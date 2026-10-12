import asyncio
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest
from jsonschema import validate

from app.agent_context import project_context, _deduplicate_reference_shelf
from app.model_gateway import ModelResult, SANDBOX_AGENT_TOOLS
from app.sandbox_agent_loop import _run_agent_loop, AgentJobCancelled
from app.sandbox_runtime import SandboxCommandResult
from app.sandbox_tool_registry import validate_agent_action

ROOT = '/workspace/skill'


@pytest.mark.parametrize('action', [
    {'action':'read_files','files':[{'path':ROOT+'/references/a.md'},{'path':ROOT+'/references/b.md'}],'reason':'Read phase references'},
    {'action':'run_commands','commands':[{'argv':['python3','scripts/check.py'],'cwd':ROOT,'timeout_seconds':60}],'reason':'Run declared scripts'},
])
def test_batch_schema_and_server_validation_agree(action):
    function=next(t['function'] for t in SANDBOX_AGENT_TOOLS if t['function']['name']==action['action'])
    validate({k:v for k,v in action.items() if k!='action'},function['parameters'])
    assert validate_agent_action(action) is None


@pytest.mark.parametrize('action', [
    {'action':'read_files','files':[]},
    {'action':'read_files','files':[{'path':'/workspace/a'}]*9},
    {'action':'read_files','files':[{'path':'/workspace/a','action':'finish'}]},
    {'action':'read_files','files':[{'path':'/workspace/a','limit':True}]},
    {'action':'run_commands','commands':[{'argv':['python3','first.py']},{'argv':['pip','install','evil']}]},
    {'action':'run_commands','commands':[{'argv':['echo','hello','&&','export']}]},
    {'action':'run_commands','commands':[{'argv':['python3','check.py'],'timeout_seconds':True}]},
])
def test_entire_batch_is_prevalidated_with_ordinary_restrictions(action):
    assert validate_agent_action(action)


def shelf_entry(text, path=ROOT+'/references/a.md'):
    return {'path':path,'content':text,'sha256':hashlib.sha256(text.encode()).hexdigest(),
            'bytes':len(text.encode()),'chars':len(text)}


def reference_message(entry, *, complete=True, role='tool', batch=False):
    payload={**entry,'ok':True,'reference':True,'complete':complete}
    body={'tool_result':'read_file','payload':payload}
    if batch:
        body={'tool_result':'read_files','payload':{'ok':True,'results':[{'index':1,'tool':'read_file','payload':payload}]}}
    return {'role':role,'tool_call_id':'ref-call','content':json.dumps(body,ensure_ascii=False)}


@pytest.mark.parametrize('batch', [False, True])
def test_exact_duplicate_keeps_full_native_result_and_original_checkpoint(batch):
    entry=shelf_entry('必须保留的完整规范。\n'*300)
    native={'role':'assistant','reasoning_content':'unchanged native reasoning',
            'tool_calls':[{'id':'ref-call','function':{'name':'read_files' if batch else 'read_file','arguments':'{}'}}]}
    messages=[{'role':'system','content':'complete system rules'},{'role':'user','content':'完整用户要求'},
              native,reference_message(entry,batch=batch)]
    checkpoint={'requirements':['all user requirements'], 'reference_shelf':[entry]}
    before=copy.deepcopy((messages,checkpoint))
    diagnostics={}
    projected=project_context(messages,checkpoint=json.dumps(checkpoint),skill_contexts=[],loaded=set(),
                              completed=set(),max_tokens=50000,diagnostics=diagnostics)
    assert projected[:-1]==messages
    memory=json.loads(projected[-1]['content'].split('\n',1)[1])
    view=memory['checkpoint']['reference_shelf'][0]
    assert 'content' not in view and view['content_in_retained_tool_history'] is True
    assert view['history_tool_call_id']=='ref-call'
    assert entry['content'] in messages[-1]['content'].replace('\\n','\n')
    assert diagnostics['reference_duplicate_copies_removed']==1
    assert (messages,checkpoint)==before


@pytest.mark.parametrize('variant', ['changed','partial','mutable','legacy','wrong_hash','malformed_batch'])
def test_dedup_falls_back_to_full_copy_when_identity_is_not_proven(variant):
    entry=shelf_entry('完整规则\n'*300)
    message=reference_message(entry)
    body=json.loads(message['content'])
    if variant=='changed': body['payload']['content']+='additional required detail'
    if variant=='partial': body['payload']['content']=entry['content'][-20:];body['payload']['complete']=False
    if variant=='mutable': body['payload']['reference']=False
    if variant=='legacy': message['role']='user'
    if variant=='wrong_hash': body['payload']['sha256']='0'*64
    if variant=='malformed_batch': body={'tool_result':'read_files','payload':{'results':None}}
    message['content']=json.dumps(body,ensure_ascii=False)
    view,count,_=_deduplicate_reference_shelf([entry],[message])
    assert view==[entry] and count==0


def test_reference_is_restored_in_full_when_matching_history_does_not_fit():
    entry=shelf_entry('Full required instruction. '*50)
    old=[{'role':'assistant','tool_calls':[{'id':'ref-call'}]},reference_message(entry)]
    # Make the old exchange expensive without altering the exact reference.
    old[0]['reasoning_content']='old native context '*2000
    latest=[{'role':'assistant','tool_calls':[{'id':'latest'}]},
            {'role':'tool','tool_call_id':'latest','content':'recent result'}]
    messages=[{'role':'system','content':'rules'},{'role':'user','content':'user requirement'},*old,*latest]
    diagnostics={}
    projected=project_context(messages,checkpoint=json.dumps({'reference_shelf':[entry]}),skill_contexts=[],
                              loaded=set(),completed=set(),max_tokens=2200,diagnostics=diagnostics)
    assert diagnostics['dropped_exchange_count']==1
    view=json.loads(projected[-1]['content'].split('\n',1)[1])['checkpoint']['reference_shelf'][0]
    assert view['content']==entry['content']
    assert diagnostics['reference_duplicate_copies_removed']==0
    assert projected[-3:-1]==latest


def test_dedup_can_be_disabled_for_identical_input_control():
    entry=shelf_entry('Full original instruction. '*100)
    messages=[{'role':'system','content':'rules'},{'role':'user','content':'request'},
              {'role':'assistant','tool_calls':[{'id':'ref-call'}]},reference_message(entry)]
    kwargs=dict(checkpoint=json.dumps({'reference_shelf':[entry]}),skill_contexts=[],loaded=set(),
                completed=set(),max_tokens=20000)
    control=project_context(messages,**kwargs,deduplicate_references=False)
    changed=project_context(messages,**kwargs)
    assert control[:-1]==changed[:-1]==messages
    assert json.loads(control[-1]['content'].split('\n',1)[1])['checkpoint']['reference_shelf'][0]['content']==entry['content']
    assert len(changed[-1]['content'])<len(control[-1]['content'])


@pytest.mark.parametrize('name,args', [
    ('read_files',{'files':[{'path':ROOT+'/references/a.md'}],'reason':'read'}),
    ('run_commands',{'commands':[{'argv':['python3','check.py'],'cwd':ROOT,'timeout_seconds':60}],'reason':'check'}),
])
def test_native_parser_preserves_parent_batch_call(name,args):
    from app.model_gateway import _parse_agent_tool_response
    message={'content':None,'reasoning_content':'native context preserved',
             'tool_calls':[{'id':'native-batch','type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}
    calls,native=_parse_agent_tool_response({'choices':[{'message':message}]})
    assert len(calls)==1 and calls[0].action=={**args,'action':name}
    assert native['tool_calls']==message['tool_calls'] and native['reasoning_content']==message['reasoning_content']


class ObservedResult(Exception):
    pass


def run_batch_case(client,user_headers,monkeypatch,action,inspect_result,*,with_plan=True,fail_at=None,cancel_after_first=False):
    from app.database import SessionLocal
    from app.models import WorkflowJob
    from test_workflow_jobs import create_version,sandbox_skill_zip
    from test_orchestration import MemorySandbox
    monkeypatch.setattr('app.sandbox_agent_loop.delivery_only_job',lambda job:True)
    _,version=create_version(client,user_headers,slug='batch-regression',package=sandbox_skill_zip())
    created=client.post('/api/v1/jobs',headers=user_headers,data={'version_id':version['id'],'instruction':'answer 42'}).json()
    cancelled=False

    class Sandbox(MemorySandbox):
        def __init__(self):
            super().__init__();self.reads=[]
            self.files[ROOT+'/references/a.md']=('规则 A\n'*600).encode()
            self.files[ROOT+'/references/b.md']=('规则 B\n'*7000).encode()

        async def read_text_window(self,path,*,offset=0,limit=30000):
            self.reads.append((path,offset,limit));text=self.files[path].decode();content=text[offset:offset+limit]
            end=offset+len(content);eof=end>=len(text)
            return {'ok':True,'path':path,'content':content,'offset':offset,'limit':limit,'total_chars':len(text),
                    'returned_chars':len(content),'eof':eof,'complete':offset==0 and eof,'truncated':not eof,
                    'next_offset':None if eof else end}

        async def command(self,argv,**kwargs):
            nonlocal cancelled
            self.calls.append(argv)
            if cancel_after_first: cancelled=True
            fail=fail_at==len(self.calls)
            return SandboxCommandResult(1 if fail else 0,'ran '+argv[-1],'fixture command failed' if fail else '')

    class Gateway:
        connection=SimpleNamespace(context_tokens=200000)
        turn=0

        async def agent_step(self,*,messages):
            self.turn+=1
            if with_plan and self.turn==1:
                return ModelResult({'action':'update_plan','goal':'answer 42','steps':[
                    {'id':'work','title':'Work','status':'pending','output_refs':['/workspace/output/result.txt']},
                    {'id':'done','title':'Done','status':'pending'}],'success_criteria':['answer 42'],
                    'validation_step_id':'done'},'test',{})
            if self.turn==(2 if with_plan else 1):
                native={'role':'assistant','content':None,'reasoning_content':'preserve parent reasoning',
                        'tool_calls':[{'id':'batch-1','type':'function','function':{
                            'name':action['action'],'arguments':json.dumps({k:v for k,v in action.items() if k!='action'})}}]}
                return ModelResult(action,'test',{},assistant_message=native,tool_call_id='batch-1')
            replies=[m for m in messages if m.get('role')=='tool' and m.get('tool_call_id')=='batch-1']
            assert len(replies)==1
            parent=next(m for m in messages if m.get('tool_calls',[{}])[0].get('id')=='batch-1')
            assert parent['reasoning_content']=='preserve parent reasoning'
            assert parent['tool_calls'][0]['function']['name']==action['action']
            payload=json.loads(replies[0]['content'])['payload']
            inspect_result(payload,messages,sandbox)
            raise ObservedResult

    sandbox=Sandbox()
    with SessionLocal() as db:
        job=db.get(WorkflowJob,created['id'])
        context={'name':'Test','version':'1','root':ROOT,'skill_md':'# Answer','runtime_requirements':{}}
        with pytest.raises(AgentJobCancelled if cancel_after_first else ObservedResult):
            asyncio.run(_run_agent_loop(db,job,sandbox,skill_contexts=[context],gateway=Gateway(),job_cancelled=lambda:cancelled))
        if cancel_after_first: assert len(sandbox.calls)==1
        return [(e.status,e.data) for e in job.events if e.event_type=='tool']


def test_read_batch_keeps_both_complete_original_texts(client,user_headers,monkeypatch):
    action={'action':'read_files','files':[{'path':ROOT+'/references/a.md'},{'path':ROOT+'/references/b.md'}],'reason':'Load references'}
    def inspect(payload,messages,sandbox):
        assert payload['ok'] and payload['all_complete'] and payload['executed_count']==2
        for item in payload['results']:
            actual=item['payload'];assert actual['content']==sandbox.files[actual['path']].decode()
            assert actual['complete'] and actual['eof']
        assert len(sandbox.reads)==3  # second file needs an internal continuation, not another model turn.
    events=run_batch_case(client,user_headers,monkeypatch,action,inspect,with_plan=False)
    assert [d['batch_index'] for _,d in events]==[1,2]


@pytest.mark.parametrize('fail_at',[None,2])
def test_command_batch_runs_in_order_and_stops_before_export_on_failure(client,user_headers,monkeypatch,fail_at):
    action={'action':'run_commands','commands':[{'argv':['python3',name],'cwd':ROOT,'timeout_seconds':60} for name in ('first.py','check.py','export.py')], 'reason':'Run known phase'}
    def inspect(payload,messages,sandbox):
        count=2 if fail_at else 3
        assert 'executed_count' in payload, payload
        assert payload['executed_count']==count and len(sandbox.calls)==count
        assert payload['succeeded_count']==(1 if fail_at else 3)
        assert [a[-1] for a in sandbox.calls]==['first.py','check.py','export.py'][:count]
        assert payload['ok'] is (fail_at is None)
        assert payload['skipped_indexes']==([3] if fail_at else [])
        if fail_at:
            assert payload['failed_index']==2 and payload['results'][0]['payload']['exit_code']==0
    events=run_batch_case(client,user_headers,monkeypatch,action,inspect,fail_at=fail_at)
    assert events[-1][0]==('failed' if fail_at else 'succeeded')


def test_invalid_later_command_prevents_all_batch_side_effects(client,user_headers,monkeypatch):
    action={'action':'run_commands','commands':[{'argv':['python3','first.py']},{'argv':['pip','install','bad']}], 'reason':'Must reject whole batch'}
    def inspect(payload,messages,sandbox):
        assert not payload['ok'] and payload['error_code']=='SANDBOX_ACTION_INVALID'
        assert 'item 2' in payload['message'] and sandbox.calls==[]
    run_batch_case(client,user_headers,monkeypatch,action,inspect)


def test_command_batch_requires_plan_and_does_not_use_inspection_allowance(client,user_headers,monkeypatch):
    action={'action':'run_commands','commands':[{'argv':['python3','first.py']}], 'reason':'Need plan'}
    def inspect(payload,messages,sandbox):
        assert payload['error_code']=='PLAN_REQUIRED' and sandbox.calls==[]
    run_batch_case(client,user_headers,monkeypatch,action,inspect,with_plan=False)


def test_command_batch_checks_cancellation_between_items(client,user_headers,monkeypatch):
    action={'action':'run_commands','commands':[{'argv':['python3','first.py']},{'argv':['python3','export.py']}], 'reason':'Cancel safely'}
    run_batch_case(client,user_headers,monkeypatch,action,lambda *args:None,cancel_after_first=True)
