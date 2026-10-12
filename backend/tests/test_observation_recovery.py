from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.agent_policy import AgentExecutionState
from app.durable_checkpoint import state_from_json, state_to_json
from app.model_gateway import ModelResult
from app.sandbox_agent_loop import _observation_in_context, _read_file_payload, _run_agent_loop
from test_orchestration import MemorySandbox


def read(path='/workspace/work/spec.txt', **window):
    return {'action': 'read_file', 'path': path, **window}


def observed(action, content='rules', **metadata):
    return {'ok': True, 'path': action['path'], 'content': content, **metadata}


@pytest.mark.parametrize('action,payload', [
    ({'action':'command','argv':['python3','rewrite.py']}, {'exit_code':0,'stdout':'rewritten'}),
    ({'action':'run_python','code':'rewrite()'}, {'exit_code':0,'stdout':'rewritten'}),
    ({'action':'command','argv':['python3','rewrite.py']}, {'exit_code':1,'stderr':'partially written'}),
])
def test_scripts_invalidate_mutable_skill_assets_and_exports(action, payload):
    state=AgentExecutionState(skill_count=1)
    old_reads=[read('/workspace/skills/ppt/assets/design_spec.md'),read('/workspace/skills/ppt/exports/report.txt')]
    for item in old_reads: state.record(item,observed(item,'old'))
    state.record(action,payload)
    assert all(state.stable_cached_observation(item) is None for item in old_reads)


def test_new_file_invalidates_ancestor_listings_but_keeps_unrelated_file_read():
    state=AgentExecutionState(skill_count=1)
    for path in ['/workspace','/workspace/work','/workspace/work/sub']:
        state.record({'action':'list_files','path':path},[])
    unrelated=read('/workspace/work/spec.txt')
    state.record(unrelated,observed(unrelated))
    state.record({'action':'write_file','path':'/workspace/work/sub/new.txt'}, {'ok':True,'bytes':4})
    assert all(state.stable_cached_observation({'action':'list_files','path':path}) is None
               for path in ['/workspace','/workspace/work','/workspace/work/sub'])
    assert state.stable_cached_observation(unrelated)['content']=='rules'


def test_bounded_read_does_not_poison_auto_complete_read():
    state=AgentExecutionState(skill_count=1)
    bounded=read('/workspace/input/long.txt',offset=0,limit=30000)
    state.record(bounded,observed(bounded,'first page',complete=False))
    assert state.stable_cached_observation(read(bounded['path'])) is None
    assert state.stable_cached_observation({**bounded,'offset':30000}) is None


def test_normal_and_cached_pagination_are_not_duplicate_information():
    state=AgentExecutionState(skill_count=1)
    for offset in range(0,150000,30000):
        action=read('/workspace/input/long.txt',offset=offset,limit=30000)
        payload=observed(action,str(offset),offset=offset)
        assert state.register_observation_access(action,reused=False,payload=payload) is None
        state.record(action,payload)
        assert state.register_observation_access(action,reused=True,payload=payload) is None


def test_exact_read_only_blocked_when_identical_content_is_available():
    state=AgentExecutionState(skill_count=1)
    action=read();payload=observed(action)
    for _ in range(2): assert state.register_observation_access(action,reused=True,payload=payload) is None
    assert state.register_observation_access(action,reused=True,payload=payload)['error_code']=='OBSERVATION_LOOP_BREAK'
    assert state.register_observation_access(action,reused=True,payload=payload,content_available=False) is None
    assert state.register_observation_access(action,reused=True,payload=observed(action,'changed')) is None


def test_context_availability_requires_the_full_result_not_an_excerpt():
    payload=observed(read())
    pinned=[{'role':'system','content':'rules'},{'role':'user','content':'request'}]
    reply={'role':'tool','tool_call_id':'read-1','content':json.dumps({'tool_result':'read_files','payload':{
        'results':[{'tool':'read_file','payload':payload}]}})}
    assert _observation_in_context(payload,[*pinned,reply])
    assert not _observation_in_context(payload,pinned)
    changed=json.loads(reply['content']);changed['payload']['results'][0]['payload']['content']='ru'
    assert not _observation_in_context(payload,[*pinned,{**reply,'content':json.dumps(changed)}])
    assert not _observation_in_context(payload,[*pinned,{**reply,'content':'{"tool_result":"read_files","payload":{"results":null}}'}])


def test_stable_cache_is_bounded_and_private_on_checkpoint():
    state=AgentExecutionState(skill_count=1)
    for i in range(40):
        action=read(f'/workspace/work/{i}.txt');state.record(action,observed(action,'x'*50000))
    assert len(json.dumps(state._stable_path_cache).encode()) <= 1_048_576
    assert '_stable_path_cache' not in state_to_json(state)


def test_failure_diagnostics_survive_projection_and_clear_on_success():
    state=AgentExecutionState(skill_count=1)
    action={'action':'command','argv':['python3','check.py']}
    payload={'exit_code':1,'stdout':'root must be <g>','stderr':''}
    state.record(action,payload);state.record(action,payload)
    assert 'root must be <g>' in state.failure_recovery_hint(action)
    assert 'root must be <g>' in state.checkpoint()
    restored=state_from_json(json.loads(json.dumps(state_to_json(state))))
    assert restored.failure_recovery_hint(action)==state.failure_recovery_hint(action)
    state.record(action,{'exit_code':0,'stdout':'passed'})
    assert state.failure_recovery_hint(action) is None
    assert not json.loads(state.checkpoint())['unresolved_failures']


def test_verifier_retry_allowed_after_partial_failed_correction():
    state=AgentExecutionState(skill_count=1)
    state.register_verifier_failure({'ok':False,'checks':[{'passed':False}]})
    assert state.verifier_retry_blocked() is not None
    state.record({'action':'command','argv':['python3','fix.py']},{'exit_code':1,'stderr':'fixed first file; second failed'})
    assert state.progress_epoch==0
    assert state.verifier_retry_blocked() is None


def test_eof_diagnosis_does_not_encourage_increasing_empty_offsets():
    class Sandbox:
        async def read_text_window(self,path,*,offset,limit):
            return {'ok':True,'path':path,'content':'','offset':offset,'limit':limit,
                    'total_chars':10,'returned_chars':0,'eof':True,'complete':False,
                    'truncated':False,'next_offset':None}
    payload=asyncio.run(_read_file_payload(Sandbox(),AgentExecutionState(skill_count=1),
                      '/workspace/work/spec.txt',[],offset=90000,limit=30000))
    assert payload['eof'] and payload['next_offset'] is None
    assert 'Do not increase the offset' in payload['hint']


def test_repeated_read_batch_returns_one_error_reply_then_recovers(client,user_headers):
    from app.database import SessionLocal
    from app.models import WorkflowJob
    from test_workflow_jobs import create_version,sandbox_skill_zip
    _,version=create_version(client,user_headers,slug='observation-recovery',package=sandbox_skill_zip())
    created=client.post('/api/v1/jobs',headers=user_headers,data={'version_id':version['id'],'instruction':'answer 42'}).json()
    sandbox=MemorySandbox();sandbox.files['/workspace/work/a.txt']=b'A';sandbox.files['/workspace/work/b.txt']=b'B'
    class Gateway:
        connection=SimpleNamespace(context_tokens=48000)
        turn=0
        async def agent_step(self,*,messages):
            self.turn+=1
            # Every native call has exactly one reply before user guidance.
            pending=set()
            for message in messages:
                if message.get('role')=='assistant': pending.update(c['id'] for c in message.get('tool_calls',[]))
                elif message.get('role')=='tool': pending.remove(message['tool_call_id'])
                elif message.get('role')=='user': assert not pending
            assert not pending
            if self.turn==1:
                action={'action':'update_plan','goal':'answer 42','steps':[{'id':'work','title':'Work'},
                    {'id':'verify','title':'Verify'}],'success_criteria':['answer 42'],'validation_step_id':'verify'}
            elif self.turn<=5:
                action={'action':'read_files','files':[{'path':'/workspace/work/a.txt'},{'path':'/workspace/work/b.txt'}],'reason':'read'}
            elif self.turn==6:
                replies=[m for m in messages if m.get('tool_call_id')=='call-5']
                assert len(replies)==1
                failure=json.loads(replies[0]['content'])['payload']
                assert failure['error_code']=='OBSERVATION_LOOP_BREAK'
                assert failure['executed_count']==1 and failure['failed_index']==1 and failure['skipped_indexes']==[2]
                action={'action':'write_file','path':'/workspace/output/result.txt','content':'42','reason':'answer'}
            elif self.turn==7:
                action={'action':'run_verifier','argv':['verify'],'reason':'verify result'}
            else:
                assert self.turn==8
                action={'action':'finish','summary':'answer 42','artifacts':['/workspace/output/result.txt']}
            call_id=f'call-{self.turn}'
            native={'role':'assistant','content':None,'tool_calls':[{'id':call_id,'type':'function','function':{
                'name':action['action'],'arguments':json.dumps({k:v for k,v in action.items() if k!='action'})}}]}
            return ModelResult(action,'test',{},assistant_message=native,tool_call_id=call_id)
    with SessionLocal() as db:
        job=db.get(WorkflowJob,created['id'])
        result=asyncio.run(_run_agent_loop(db,job,sandbox,skill_contexts=[{'root':'/workspace/skills/test',
            'name':'test','version':'1','skill_md':'# Answer','runtime_requirements':{}}],gateway=Gateway(),job_cancelled=lambda:False))
        assert result[0]=='answer 42' and result[2]==8
        assert job.memory.data['verification']['ok'] is True
        assert job.memory.data['verification']['validation_recorded'] is True
