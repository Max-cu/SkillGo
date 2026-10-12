import asyncio
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest
from jsonschema import validate

from app.agent_context import _deduplicate_reference_shelf
from app.agent_policy import AgentExecutionState
from app.durable_checkpoint import state_from_json, state_to_json
from app.model_gateway import ModelResult, SANDBOX_AGENT_TOOLS
from app.sandbox_agent_loop import _run_agent_loop, AgentJobCancelled, _read_file_payload
from app.sandbox_runtime import SandboxCommandResult
from app.sandbox_tool_registry import validate_agent_action
from app.skill_phase_contracts import stage_contracts, available_reference_paths, expand_reference_actions

ROOT='/workspace/skill'
GUIDE='''# Approved workflow
### Step 1: Design
全文加载 `references/design.md`。
| 时机 | 读什么 | 怎么读 |
| 开始前 | `references/style.md` | 全文 |
按需查阅 `references/optional.md`。
### Step 3: compile.py
加载 `references/build.md` 获取完整说明。
| 子步骤 | 做什么 | 命令 |
| **3a** | 前置检查 | `python scripts/check.py` |
| **3b** | 构建 | `python scripts/compile.py` |
| **3c** | 检查 | `python scripts/verify.py` |
### Step 4: export.py
加载 `references/export.md` 获取完整说明。
| 子步骤 | 做什么 | 命令 |
| **4a** | 导出 | `python scripts/export.py` |
| **4b** | 检查 | `python scripts/verify_export.py` |
### Step 5: Decide the next design
Needs a model decision.
'''


def context(guide=GUIDE):
    return {'name':'Test','version':'1','root':ROOT,'skill_md':guide,'runtime_requirements':{}}


def test_only_explicit_full_references_group_together():
    contract=stage_contracts(context())
    assert contract['groups'][ROOT+'/references/design.md']==[
        ROOT+'/references/design.md',ROOT+'/references/style.md']
    assert ROOT+'/references/optional.md' not in contract['required']
    workflow=contract['workflows']['steps-3-4']
    assert [x[1] for x in workflow['commands']]==[
        'scripts/check.py','scripts/compile.py','scripts/verify.py','scripts/export.py','scripts/verify_export.py']
    assert workflow['stages']==[3,4]


@pytest.mark.parametrize('replacement',[
    'python scripts/compile.py "<unknown>"',
    'python scripts/compile.py && python scripts/delete.py',
    'python scripts/../compile.py',
    'python scripts/compile.py ${secret}',
    'python scripts/compile.py …',
])
def test_unresolved_or_shell_commands_disable_entire_stage(replacement):
    guide=GUIDE.replace('python scripts/compile.py',replacement)
    assert 'steps-3-4' not in stage_contracts(context(guide))['workflows']


def test_conditional_command_and_dynamic_stage_not_automated():
    guide=GUIDE.replace('| **3b** | 构建 |','| **3b** | 若有图片则构建 |')
    assert 'steps-3-4' not in stage_contracts(context(guide))['workflows']
    assert 'steps-1-1' not in stage_contracts(context())['workflows']


def test_manual_decision_breaks_fixed_stage_and_ambiguous_numbering_disables_workflows():
    guide=GUIDE.replace('| **3b** | 构建 |', '| **3x** | 选择布局 | 无命令 |\n| **3b** | 构建 |')
    assert 'steps-3-4' not in stage_contracts(context(guide))['workflows']
    assert not stage_contracts(context(GUIDE+GUIDE))['workflows']


def test_workflow_can_end_at_the_requested_stage():
    contract=stage_contracts(context())
    assert [a[1] for a in contract['workflows']['steps-3-3']['commands']]==[
        'scripts/check.py','scripts/compile.py','scripts/verify.py']


@pytest.mark.parametrize('path',['../outside.md','/workspace/outside.md','references/../outside.md','assets/a.md'])
def test_reference_groups_cannot_escape_or_pin_mutable_inputs(path):
    guide=f'### Step 1: Design\n全文加载 `{path}`。'
    assert not stage_contracts(context(guide))['required']


def test_unknown_skill_keeps_existing_tools():
    assert stage_contracts(context('# A completely free-form Skill'))=={'groups':{},'required':[],'workflows':{}}


def test_documentation_examples_do_not_declare_executable_workflows():
    assert stage_contracts(context('```markdown\n'+GUIDE+'\n```'))=={'groups':{},'required':[],'workflows':{}}


def test_phase_group_expansion_reuses_only_currently_available_full_originals():
    contracts=[stage_contracts(context())]
    action={'action':'read_file','path':ROOT+'/references/design.md','limit':200}
    assert len(expand_reference_actions([action],contracts,set()))==2
    assert expand_reference_actions([action],contracts,{ROOT+'/references/style.md'})==[action]
    complete={'path':ROOT+'/references/style.md','reference':True,'complete':True,'content':'full'}
    message={'role':'tool','content':json.dumps({'tool_result':'read_file','payload':complete})}
    assert available_reference_paths([message])=={complete['path']}
    complete['complete']=False
    message['content']=json.dumps({'tool_result':'read_file','payload':complete})
    assert available_reference_paths([message])==set()


def test_workflow_schema_and_server_validation():
    action={'action':'run_skill_workflow','skill_index':1,'workflow_id':'steps-3-4','reason':'Build known stages'}
    function=next(x['function'] for x in SANDBOX_AGENT_TOOLS if x['function']['name']==action['action'])
    validate({k:v for k,v in action.items() if k!='action'},function['parameters'])
    assert validate_agent_action(action) is None


@pytest.mark.parametrize('extra',[{'commands':[]},{'restart':'true'},{'skill_index':True},{'workflow_id':''}])
def test_model_cannot_override_workflow_commands(extra):
    action={'action':'run_skill_workflow','skill_index':1,'workflow_id':'steps-3-4',**extra}
    assert validate_agent_action(action)


def test_workflow_cursor_survives_new_and_old_durable_snapshots():
    state=AgentExecutionState(1)
    state.workflow_runs={'workflow':{'next_index':3,'command_count':5}}
    assert state_from_json(state_to_json(state)).workflow_runs==state.workflow_runs
    old=state_to_json(state);old.pop('workflow_runs')
    assert state_from_json(old).workflow_runs=={}


def test_dedup_recognizes_nested_phase_and_workflow_originals():
    entry={'path':ROOT+'/references/style.md','content':'Full original\n'*300}
    entry['sha256']=hashlib.sha256(entry['content'].encode()).hexdigest()
    payload={**entry,'reference':True,'complete':True}
    for tool,body in [('read_file',{'phase_references':[payload]}),
                      ('run_skill_workflow',{'results':[{'tool':'read_file','payload':payload}]})]:
        original={'role':'tool','tool_call_id':'parent','content':json.dumps({'tool_result':tool,'payload':body})}
        before=copy.deepcopy(original)
        view,count,_=_deduplicate_reference_shelf([entry],[original])
        assert count==1 and view[0]['history_tool_call_id']=='parent' and original==before


class Observed(Exception):
    pass


def run_case(client,user_headers,monkeypatch,actions,inspect,*,with_plan=True,fail_once=None,cancel_after=None,guide=GUIDE):
    from app.database import SessionLocal
    from app.models import WorkflowJob
    from test_workflow_jobs import create_version,sandbox_skill_zip
    from test_orchestration import MemorySandbox
    monkeypatch.setattr('app.sandbox_agent_loop.delivery_only_job',lambda job:True)
    _,version=create_version(client,user_headers,slug='phase-regression',package=sandbox_skill_zip())
    job_id=client.post('/api/v1/jobs',headers=user_headers,data={'version_id':version['id'],'instruction':'answer 42'}).json()['id']
    cancelled=False

    class Sandbox(MemorySandbox):
        def __init__(self):
            super().__init__();self.reads=[];self.failed=False
            for name,size in [('design',5618),('style',17009),('build',5000),('export',4000)]:
                self.files[ROOT+'/references/'+name+'.md']=('R'*size).encode()
            self.files[ROOT+'/assets/sample.txt']=b'mutable generated text'*50

        async def read_text_window(self,path,*,offset=0,limit=30000):
            self.reads.append((path,offset,limit));text=self.files[path].decode();content=text[offset:offset+limit]
            end=offset+len(content);eof=end>=len(text)
            return {'ok':True,'path':path,'content':content,'offset':offset,'limit':limit,'total_chars':len(text),
                    'returned_chars':len(content),'eof':eof,'complete':offset==0 and eof,'truncated':not eof,
                    'next_offset':None if eof else end}

        async def command(self,argv,**kwargs):
            nonlocal cancelled
            self.calls.append(argv)
            fail=argv[-1]==fail_once and not self.failed
            if fail:self.failed=True
            if cancel_after and len(self.calls)==cancel_after:cancelled=True
            return SandboxCommandResult(1 if fail else 0,'ran '+argv[-1],'fixture failure' if fail else '')

    sandbox=Sandbox()
    class Gateway:
        connection=SimpleNamespace(context_tokens=300000)
        turn=0
        async def agent_step(self,*,messages):
            self.turn+=1
            if with_plan and self.turn==1:
                return ModelResult({'action':'update_plan','goal':'answer 42','steps':[
                    {'id':'work','title':'Work','status':'pending','output_refs':['/workspace/output/result.txt']},
                    {'id':'done','title':'Done','status':'pending'}],
                    'success_criteria':['answer 42'],'validation_step_id':'done'},'test',{})
            position=self.turn-(2 if with_plan else 1)
            if position<len(actions):
                action=actions[position];call_id=f'phase-{position}'
                native={'role':'assistant','content':None,'reasoning_content':'original reasoning',
                        'tool_calls':[{'id':call_id,'type':'function','function':{'name':action['action'],
                            'arguments':json.dumps({k:v for k,v in action.items() if k!='action'})}}]}
                return ModelResult(action,'test',{},assistant_message=native,tool_call_id=call_id)
            replies=[]
            for i,action in enumerate(actions):
                matching=[m for m in messages if m.get('role')=='tool' and m.get('tool_call_id')==f'phase-{i}']
                assert len(matching)==1
                parent=next(m for m in messages if m.get('tool_calls',[{}])[0].get('id')==f'phase-{i}')
                assert parent['reasoning_content']=='original reasoning'
                body=json.loads(matching[0]['content']);assert body['tool_result']==action['action']
                replies.append(body['payload'])
            inspect(replies,messages,sandbox)
            raise Observed

    with SessionLocal() as db:
        job=db.get(WorkflowJob,job_id)
        with pytest.raises(AgentJobCancelled if cancel_after else Observed):
            asyncio.run(_run_agent_loop(db,job,sandbox,skill_contexts=[context(guide)],gateway=Gateway(),job_cancelled=lambda:cancelled))
        if cancel_after: assert len(sandbox.calls)==cancel_after
        return [(e.status,e.data) for e in job.events if e.event_type=='tool']


@pytest.mark.parametrize('batch',[False,True])
def test_small_required_window_becomes_full_phase_in_one_model_reply(client,user_headers,monkeypatch,batch):
    child={'path':ROOT+'/references/design.md','offset':100,'limit':200}
    action=({'action':'read_files','files':[child]} if batch else {'action':'read_file',**child})
    def inspect(replies,messages,sandbox):
        payload=replies[0]
        refs=[x['payload'] for x in payload['results']] if batch else [payload,*payload['phase_references']]
        assert [x['path'] for x in refs]==[ROOT+'/references/design.md',ROOT+'/references/style.md']
        assert all(x['complete'] and x['eof'] and x['content']==sandbox.files[x['path']].decode() for x in refs)
        assert refs[0]['requested_window']=={'offset':100,'limit':200}
        assert len(sandbox.reads)==2
    run_case(client,user_headers,monkeypatch,[action],inspect,with_plan=False)


def test_ordinary_sampling_stays_paginated(client,user_headers,monkeypatch):
    action={'action':'read_file','path':ROOT+'/assets/sample.txt','limit':200}
    def inspect(replies,messages,sandbox):
        assert replies[0]['returned_chars']==200 and not replies[0]['complete']
        assert len(sandbox.reads)==1
    run_case(client,user_headers,monkeypatch,[action],inspect,with_plan=False)


WORKFLOW={'action':'run_skill_workflow','skill_index':1,'workflow_id':'steps-3-4','reason':'Execute declared fixed stages'}


def test_workflow_loads_full_guides_and_all_commands_without_intermediate_model_calls(client,user_headers,monkeypatch):
    def inspect(replies,messages,sandbox):
        payload=replies[0]
        assert payload['ok'] and payload['executed_count']==7 and payload['next_command_index']==5
        assert [x[-1] for x in sandbox.calls]==[x[1] for x in stage_contracts(context())['workflows']['steps-3-4']['commands']]
        assert len(sandbox.reads)==2
        assert all(x['payload']['complete'] for x in payload['results'] if x['tool']=='read_file')
    events=run_case(client,user_headers,monkeypatch,[WORKFLOW],inspect)
    assert all(status=='succeeded' for status,_ in events)


def test_failure_stops_workflow_and_retry_does_not_replay_successful_commands(client,user_headers,monkeypatch):
    def inspect(replies,messages,sandbox):
        assert not replies[0]['ok'] and replies[0]['next_command_index']==1
        assert replies[1]['ok'] and replies[1]['next_command_index']==5
        assert [x[-1] for x in sandbox.calls]==['scripts/check.py','scripts/compile.py',
            'scripts/compile.py','scripts/verify.py','scripts/export.py','scripts/verify_export.py']
        assert len(sandbox.reads)==2
    run_case(client,user_headers,monkeypatch,[WORKFLOW,{**WORKFLOW,'retry_failed':True}],inspect,fail_once='scripts/compile.py')


def test_failed_workflow_requires_explicit_recovery_before_repeating_export(client,user_headers,monkeypatch):
    def inspect(replies,messages,sandbox):
        assert not replies[0]['ok'] and not replies[1]['ok']
        assert 'recovery' in replies[1]['message']
        assert [x[-1] for x in sandbox.calls]==['scripts/check.py','scripts/compile.py',
            'scripts/verify.py','scripts/export.py']
    run_case(client,user_headers,monkeypatch,[WORKFLOW,WORKFLOW],inspect,fail_once='scripts/export.py')


def test_completed_workflow_cannot_blindly_repeat_export(client,user_headers,monkeypatch):
    def inspect(replies,messages,sandbox):
        assert replies[0]['ok'] and not replies[1]['ok']
        assert 'already completed' in replies[1]['message']
        assert len(sandbox.calls)==5
    run_case(client,user_headers,monkeypatch,[WORKFLOW,WORKFLOW],inspect)


def test_explicit_restart_after_changed_inputs_is_available(client,user_headers,monkeypatch):
    def inspect(replies,messages,sandbox):
        assert all(p['ok'] for p in replies) and len(sandbox.calls)==10
    run_case(client,user_headers,monkeypatch,[WORKFLOW,{**WORKFLOW,'restart':True}],inspect)


@pytest.mark.parametrize('action',[WORKFLOW,{**WORKFLOW,'workflow_id':'unknown'},{**WORKFLOW,'commands':[]}])
def test_missing_plan_unknown_workflow_and_injected_commands_have_no_effects(client,user_headers,monkeypatch,action):
    def inspect(replies,messages,sandbox):
        assert not replies[0]['ok'] and not sandbox.calls and not sandbox.reads
    run_case(client,user_headers,monkeypatch,[action],inspect,with_plan=action!=WORKFLOW)


def test_workflow_cancellation_checked_between_commands(client,user_headers,monkeypatch):
    run_case(client,user_headers,monkeypatch,[WORKFLOW],lambda *args:None,cancel_after=1)


def test_required_full_reader_uses_bounded_internal_pages():
    class Sandbox:
        reads=[]
        async def read_text_window(self,path,*,offset,limit):
            self.reads.append((offset,limit));content=('X'*100001)[offset:offset+limit];end=offset+len(content)
            return {'path':path,'content':content,'total_chars':100001,'returned_chars':len(content),
                    'eof':end==100001,'next_offset':None if end==100001 else end,'offset':offset,'limit':limit}
    sandbox=Sandbox()
    payload=asyncio.run(_read_file_payload(sandbox,AgentExecutionState(1),ROOT+'/references/large.md',[ROOT],
        offset=0,limit=30000,complete_reference=True,max_complete_chars=200000))
    assert payload['complete'] and len(payload['content'])==100001
    assert len(sandbox.reads)==4 and all(limit<=30000 for _,limit in sandbox.reads)
