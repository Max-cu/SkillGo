import asyncio
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app import artifact_collection as collection
from app.agent_policy import AgentExecutionState
from app.artifact_validation import snapshot_sandbox_artifacts
from app.sandbox_runtime import SandboxRuntimeError
from test_agent_policy import _completed_plan


class Sandbox:
    def __init__(self, files):
        self.files = dict(files)

    async def command(self, argv, **kwargs):
        for source, target in json.loads(argv[-2]):
            if source not in self.files:
                return SimpleNamespace(exit_code=0, stdout=json.dumps({'ok': False, 'error_code': 'SANDBOX_ARTIFACT_MISSING', 'message': source}))
            self.files[target] = self.files[source]
        return SimpleNamespace(exit_code=0, stdout='{"ok":true}')

    def download_file(self, path):
        return self.files[path]

    async def list_files(self,path):
        return [{'path':p,'type':'file','size':len(data)} for p,data in self.files.items() if p.startswith(path.rstrip('/')+'/')]

    def read_workspace_file(self,path):
        return self.files[path]

    async def write_text(self,path,content):
        self.files[path]=content.encode()


def test_collect_skill_export_relative_paths_and_duplicate_names():
    root='/workspace/skills/01-ppt/ppt'
    a=root+'/exports/中文报告.pptx'
    b='/workspace/work/other/中文报告.pptx'
    sandbox=Sandbox({a:b'first',b:b'second'})
    result=asyncio.run(collection.collect_declared_artifacts(sandbox, ['exports/中文报告.pptx',b,a],cwd=root))
    assert len(result['artifacts'])==2
    assert len(set(result['artifacts']))==2
    assert set(result['artifact_sources'])=={a,b}
    assert all(p.startswith('/workspace/output/') for p in result['artifacts'])
    assert [sandbox.files[p] for p in result['artifacts']]==[b'first',b'second']
    sandbox.files[a]=b'changed'
    again=asyncio.run(collection.collect_declared_artifacts(sandbox,[a],cwd=root))
    assert again['artifacts'][0]==result['artifacts'][0]
    assert sandbox.files[again['artifacts'][0]]==b'changed'


@pytest.mark.parametrize('path',['/etc/passwd','/workspace/../etc/passwd','../../file','/workspace/output',''])
def test_invalid_declaration_is_correctable_not_job_failure(path):
    result=asyncio.run(collection.receive_declared_artifacts(Sandbox({}),[path]))
    assert result['ok'] is False
    assert result['error_code']=='SANDBOX_ARTIFACT_DENIED'


def test_missing_artifact_is_correctable_and_existing_output_unchanged():
    result=asyncio.run(collection.receive_declared_artifacts(Sandbox({}),['/workspace/work/missing.pptx']))
    assert result['error_code']=='SANDBOX_ARTIFACT_MISSING'
    existing='/workspace/output/report.pptx'
    assert asyncio.run(collection.collect_declared_artifacts(Sandbox({existing:b'file'}),[existing]))['artifacts']==[existing]


def test_delivery_mode_is_scoped_to_all_selected_skills(monkeypatch):
    monkeypatch.setattr(collection,'settings',replace(collection.settings,artifact_delivery_only=False,delivery_only_skill_ids=('ppt',)))
    assert collection.delivery_only_job(SimpleNamespace(skill_id='ppt',selected_skills=[]))
    assert not collection.delivery_only_job(SimpleNamespace(skill_id='other',selected_skills=[]))
    assert not collection.delivery_only_job(SimpleNamespace(skill_id='ppt',selected_skills=[{'skill_id':'ppt'},{'skill_id':'other'}]))


def test_global_delivery_mode_covers_skills_outside_allowlist(monkeypatch):
    monkeypatch.setattr(collection,'settings',replace(collection.settings,artifact_delivery_only=True,delivery_only_skill_ids=()))
    assert collection.delivery_only_job(SimpleNamespace(skill_id='other',selected_skills=[]))
    assert collection.delivery_only_job(SimpleNamespace(skill_id='ppt',selected_skills=[{'skill_id':'ppt'},{'skill_id':'other'}]))


def test_delivery_finish_does_not_require_verification_but_keeps_business_steps():
    state=AgentExecutionState(skill_count=1,loaded_skills={1},completed_skill_indexes={1},delivery_only=True)
    assert state.update_plan(_completed_plan())['ok']
    assert state.finish_blocker(current_artifacts={'/workspace/output/a.pptx':'hash'}) is None
    state.plan['steps'][0]['status']='in_progress'
    assert 'incomplete step' in state.finish_blocker(current_artifacts={})
    state.delivery_only=False
    state.plan['steps'][0]['status']='completed'
    assert state.finish_blocker(current_artifacts={}) is not None


def test_delivery_snapshot_checks_bytes_without_inspecting_ppt_content():
    path='/workspace/output/a.pptx'
    sandbox=Sandbox({path:b'bytes are not a PPTX archive'})
    result=asyncio.run(snapshot_sandbox_artifacts(sandbox,[path],validate_content=False))
    assert result[path]==hashlib.sha256(sandbox.files[path]).hexdigest()
    with pytest.raises(SandboxRuntimeError):
        asyncio.run(snapshot_sandbox_artifacts(sandbox,[path]))


@pytest.mark.parametrize('validate_content', [True, False])
def test_snapshot_rejects_empty_artifact_in_both_modes(validate_content):
    path='/workspace/output/empty.pptx'
    with pytest.raises(SandboxRuntimeError, match='Artifact is empty'):
        asyncio.run(snapshot_sandbox_artifacts(Sandbox({path:b''}),[path],validate_content=validate_content))


@pytest.mark.parametrize("tracking", [True, False])
@pytest.mark.parametrize("global_delivery", [True, False])
@pytest.mark.parametrize("observability", [True, False])
def test_agent_finish_collects_exports_without_verifier_and_recovers_bad_path(client,user_headers,owner_headers,monkeypatch,tracking,global_delivery,observability):
    from app import config
    from app import sandbox_agent_loop as loop
    from app.database import SessionLocal
    from app.models import WorkflowJob
    from app.model_gateway import ModelResult
    from app.durable_checkpoint import state_to_json
    from app.sandbox_agent_loop import _run_agent_loop, _agent_messages
    from test_workflow_endpoints import publish_sandbox_skill,deploy_endpoint

    monkeypatch.setattr(config,'settings',replace(config.settings,sandbox_worker_enabled=True))
    monkeypatch.setattr(loop,'settings',replace(loop.settings,context_observability_enabled=observability))
    _,version=publish_sandbox_skill(client,user_headers,owner_headers,slug='delivery-only-ppt')
    endpoint=deploy_endpoint(client,user_headers,version,slug='delivery-only-ppt-v1')
    response=client.post(f"/api/v1/workflow-endpoints/{endpoint['slug']}/jobs",
                         headers={'X-SkillGo-Key':endpoint['api_key']},data={'instruction':'Create PPT'})
    assert response.status_code==202,response.text
    monkeypatch.setattr(collection,'settings',replace(collection.settings,
        artifact_delivery_only=global_delivery,
        delivery_only_skill_ids=() if global_delivery else (version['skill_id'],)))
    root='/workspace/skills/01-ppt/ppt'
    exported=root+'/exports/报告.pptx'
    sandbox=Sandbox({exported:b'PPT file bytes'})
    state=AgentExecutionState(skill_count=1,loaded_skills={1},plan_status_tracking=tracking)
    assert state.update_plan(_completed_plan())['ok']

    class Gateway:
        connection=SimpleNamespace(context_tokens=48000,agent_options={})
        model_name='test-private-model'
        count=0
        async def agent_step(self,**kwargs):
            self.count+=1
            path='/etc/passwd' if self.count==1 else exported
            return ModelResult(output={'action':'finish','summary':'PPT completed','artifacts':[path]},
                               model_name=self.model_name,token_usage={})

    with SessionLocal() as db:
        job=db.get(WorkflowJob,response.json()['id'])
        context={'root':root,'name':'PPT','version':'1','skill_md':'Create PPT',
                 'runtime_requirements':{},'fixed_execution':None}
        system=_agent_messages(job,[context],[])[0]['content']
        assert 'Platform delivery-only policy' in system
        assert '24. Before finish' not in system
        assert '- Final verification uses' not in system
        assert 'Put all final deliverables under /workspace/output' not in system
        sandbox.durable_resume={'versions':[version['id']],'model_name':job.model_name,
            'messages':[{'role':'system','content':'Test delivery workflow'},{'role':'user','content':'Create PPT'}],
            'execution_state':state_to_json(state),'next_turn':1,'repeated_action':'','repeat_count':0,
            'recoverable_errors':0,'tool_operation_count':0,'singleton_tool_turns':0,
            'fixed_executed':[False],'memory':{}}
        gateway=Gateway()
        result=asyncio.run(_run_agent_loop(db,job,sandbox,skill_contexts=[context],
                         gateway=gateway,job_cancelled=lambda:False))
        assert gateway.count==2
        if not tracking:
            assert job.memory.data['plan']['tracking_mode']=='outline'
            assert all(step['status']=='pending' for step in job.memory.data['plan']['steps'])
        assert result[0]=='PPT completed'
        assert len(result[1])==1
        assert sandbox.files[result[1][0]]==b'PPT file bytes'
        reasoning=[event for event in job.events if event.event_type=='reasoning']
        assert len(reasoning)==2
        for event in reasoning:
            assert ('context_observation' in event.data) is observability
            if observability:
                assert event.data['context_observation']['scope']=='agent_projection_before_gateway'
                assert event.data['turn_timing']['snapshot_status']=='disabled'
                assert event.data['turn_timing']['model_call_ms']>=0
                assert event.data['provider_usage']['available'] is False
                assert event.data['provider_usage']['input_tokens'] is None
        from app.models import AgentRunEvent
        checkpoints=db.query(AgentRunEvent).filter(AgentRunEvent.run_id==job.agent_run.id,
                                                   AgentRunEvent.event_type=='agent.checkpoint').all()
        assert any(e.data.get('state',{}).get('completed_skill_count')==1 for e in checkpoints)
