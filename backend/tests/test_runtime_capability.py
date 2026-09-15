import asyncio
from dataclasses import replace
from types import SimpleNamespace
import pytest
from test_environment_preparation import db, BASE
from app import runtime_capability as runtime
from app import environment_preparation as prep


@pytest.fixture
def setup(db, monkeypatch):
    monkeypatch.setattr(runtime, 'settings', replace(runtime.settings,
        environment_preparation_enabled=True, environment_queue_wait_seconds=0))
    job=SimpleNamespace(memory=SimpleNamespace(data={'environment': {'image_id': BASE, 'capabilities': ['pdf.read']}}))
    return db,job,object()


def call(setup, cap='image.qr', cancelled=lambda:False):
    db,job,sandbox=setup
    return asyncio.run(runtime.request_capability(db,job,sandbox,cap,cancelled))


def test_available_and_unsupported_do_not_spend_upgrade_budget(setup):
    assert call(setup,'pdf.read')['already_available']
    assert call(setup,'pip:evil')['error_code']=='ENVIRONMENT_CAPABILITY_UNSUPPORTED'
    assert 'environment_upgrade_attempts' not in setup[1].memory.data


def test_pending_and_failed_keep_old_binding(setup):
    assert call(setup)['error_code']=='ENVIRONMENT_BUILD_WAIT_TIMEOUT'
    env=prep.enqueue_environment(setup[0],prep.environment_spec(['image.qr']))
    env.status='failed';env.error_message='probe failed';setup[0].commit()
    assert call(setup)['error_code']=='ENVIRONMENT_BUILD_FAILED'
    assert call(setup)['error_code']=='ENVIRONMENT_UPGRADE_LIMIT'
    assert 'prepared_environment_digest' not in setup[1].memory.data


@pytest.mark.parametrize('failure', [False, True])
def test_cached_upgrade_validates_before_updating_binding(setup, monkeypatch, failure):
    db,job,sandbox=setup
    env=prep.enqueue_environment(db,prep.environment_spec(['image.qr']))
    env.status='ready';env.image_id='sha256:'+'b'*64;db.commit()
    class Candidate:
        async def write_text(self,*args): pass
    async def probe(*args):
        return {'image_id':env.image_id,'capabilities':['pdf.read','image.qr']}
    async def handover(old,cancelled,*,image_id,validate_candidate):
        assert image_id==env.image_id
        await validate_candidate(Candidate())
        if failure: raise ValueError('copy failed')
        return {'file_count':2}
    monkeypatch.setattr(runtime,'preflight_environment',probe)
    monkeypatch.setattr(runtime,'replace_sandbox',handover)
    result=call(setup)
    assert result['ok'] is not failure
    if failure: assert 'prepared_environment_digest' not in job.memory.data
    else:
        assert job.memory.data['prepared_environment_digest']==env.digest
        assert call(setup)['already_available']
        assert job.memory.data['environment_upgrade_attempts']==1


def test_cancel_during_build_does_not_switch(setup):
    assert call(setup,cancelled=lambda:True)['error_code']=='WORKFLOW_CANCELLED'
    assert 'prepared_environment_digest' not in setup[1].memory.data


def test_agent_dispatches_capability_request_and_keeps_result(client,user_headers,fake_model_gateway,monkeypatch):
    from test_workflow_jobs import create_version,sandbox_skill_zip
    from test_orchestration import MemorySandbox
    from app.database import SessionLocal
    from app.models import WorkflowJob
    from app.model_gateway import ModelResult
    from app import sandbox_agent_loop as loop
    _,v=create_version(client,user_headers,slug='upgrade-agent',package=sandbox_skill_zip())
    job_id=client.post('/api/v1/jobs',headers=user_headers,data={'version_id':v['id'],'instruction':'test'}).json()['id']
    calls=[]
    async def upgrade(db,job,sandbox,cap,cancelled):
        calls.append(cap)
        return {'ok':True,'upgraded':True,'capabilities':['image.qr']}
    monkeypatch.setattr(loop,'request_capability',upgrade)
    class Done(Exception):pass
    class Gateway:
        connection=SimpleNamespace(context_tokens=48000)
        turn=0
        async def agent_step(self,*,messages):
            self.turn+=1
            if self.turn==1:return ModelResult({'action':'request_capability','capability':'image.qr','reason':'Generate QR output'},'test',{})
            assert calls==['image.qr']
            assert any('"upgraded": true' in str(m) for m in messages)
            raise Done()
    with SessionLocal() as db:
        with pytest.raises(Done):
            asyncio.run(loop._run_agent_loop(db,db.get(WorkflowJob,job_id),MemorySandbox(),
                skill_contexts=[{'name':'test','version':'1','root':'/workspace/skill','skill_md':'# test','runtime_requirements':{}}],
                gateway=Gateway(),job_cancelled=lambda:False))
