import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from app import durable_checkpoint as cp
from app.agent_policy import AgentExecutionState
from app.storage import LocalObjectStorage


@pytest.fixture
def job(tmp_path, monkeypatch):
    monkeypatch.setattr(cp, 'storage', LocalObjectStorage(tmp_path))
    return SimpleNamespace(id='task', user_id='owner', network_enabled=False,
                           memory=SimpleNamespace(data={}))


def publish(job):
    ref = cp.write_bundle(job, {'/workspace/work/a': b'hello'},
        {'/workspace/work/a': 0o640}, ['/workspace/work'], {'next_turn': 4}, 'sha256:' + 'a'*64)
    job.memory.data = {'durable_checkpoint': ref}
    return ref


def test_roundtrip_and_fresh_reader(job):
    ref = publish(job)
    fresh = SimpleNamespace(**vars(job))
    fresh.memory = SimpleNamespace(data=json.loads(json.dumps(job.memory.data)))
    meta, files = cp.load_bundle(fresh)
    assert files == {'/workspace/work/a': b'hello'}
    assert meta['state']['next_turn'] == 4
    assert meta['modes']['/workspace/work/a'] == 0o640
    assert (cp.storage.root / ref['key']).exists()


def test_write_bundle_fsyncs_directory_on_posix(job, monkeypatch):
    # Guards the os.open/os.fsync directory branch (previously a typo
    # os.path.open that Windows tests skipped but Linux containers hit).
    monkeypatch.setattr(cp.os, "name", "posix")
    monkeypatch.setattr(cp.os, "O_DIRECTORY", 0, raising=False)
    calls = []
    monkeypatch.setattr(cp.os, "open", lambda path, flags: calls.append("open") or 7)
    monkeypatch.setattr(cp.os, "fsync", lambda fd: calls.append("fsync"))
    monkeypatch.setattr(cp.os, "close", lambda fd: calls.append("close"))
    publish(job)
    # File fsync, then directory open/fsync/close after the rename.
    assert calls == ["fsync", "open", "fsync", "close"]


def test_state_roundtrip():
    state = AgentExecutionState(skill_count=2, loaded_skills={0,1}, skill_evidence={0:'proof'}, mutation_epoch=8)
    state._observation_cache[(8, 'x')] = object()
    restored = cp.state_from_json(json.loads(json.dumps(cp.state_to_json(state))))
    assert restored.loaded_skills == {0,1}
    assert restored.skill_evidence == {0:'proof'}
    assert restored.mutation_epoch == 8
    assert restored._observation_cache == {}


@pytest.mark.parametrize('change', ['bytes', 'owner', 'network', 'size', 'path'])
def test_rejects_corruption_and_wrong_identity(job, change):
    ref = publish(job)
    if change == 'bytes':
        path = cp.storage.root / ref['key']
        path.write_bytes(path.read_bytes()[:-1] + b'x')
    elif change == 'owner': job.user_id = 'someone-else'
    elif change == 'network': job.network_enabled = True
    elif change == 'size': ref['size'] += 1
    else: ref['key'] = '../secret'
    with pytest.raises(ValueError): cp.load_bundle(job)


def test_inflight_never_replayed(job):
    publish(job)
    db, fence = Mock(), Mock()
    cp.mark_inflight(db, job, fence)
    fence.assert_called_once()
    with pytest.raises(cp.SandboxRuntimeError, match='automatic replay'):
        cp.load_bundle(job)


@pytest.mark.parametrize('path', ['/etc/passwd', '/workspace/../secret', '/workspace2/a', '/workspace/a\\b'])
def test_paths_confined(path):
    with pytest.raises(ValueError): cp.validate_path(path)


def test_state_size_bound(job, monkeypatch):
    monkeypatch.setattr(cp, 'MAX_STATE', 5)
    with pytest.raises(ValueError, match='16 MiB'): publish(job)


def test_ambiguous_commit_keeps_object(job, monkeypatch):
    sandbox = SimpleNamespace(container=Mock(attrs={'Image':'sha256:'+'a'*64}))
    monkeypatch.setattr(cp, 'export_workspace', lambda _: ({'/workspace/a':b'a'}, {}, []))
    db = Mock()
    db.commit.side_effect = RuntimeError('lost acknowledgement')
    with pytest.raises(RuntimeError):
        asyncio.run(cp.save_checkpoint(db, job, sandbox, {'next_turn':1}, lambda:None))
    assert len(list(cp.storage.root.rglob('*.zip'))) == 1
    sandbox.container.unpause.assert_called_once()


def test_restore_only_passes_filesystem_metadata(job):
    publish(job)
    bundle = cp.load_bundle(job)
    bundle[0]['state']['messages'] = ['PRIVATE_CONTEXT']
    async def command(argv, **kwargs):
        assert 'PRIVATE_CONTEXT' not in argv[-1]
        return SimpleNamespace(exit_code=0)
    sandbox = SimpleNamespace(put_files=Mock(), command=command)
    asyncio.run(cp.restore_bundle(sandbox, bundle))
    assert sandbox.durable_resume['next_turn'] == 4

def test_agent_continues_from_disk_after_losing_all_in_memory_state(client, user_headers, fake_model_gateway, monkeypatch, job):
    from dataclasses import replace
    from app import sandbox_agent_loop as loop
    from app.database import SessionLocal
    from app.models import WorkflowJob
    from app.model_gateway import ModelResult
    from test_orchestration import MemorySandbox
    from test_workflow_jobs import create_version, sandbox_skill_zip

    _, version = create_version(client, user_headers, slug='durable-loop', package=sandbox_skill_zip())
    created = client.post('/api/v1/jobs', headers=user_headers,
        data={'version_id':version['id'], 'instruction':'answer 42'}).json()
    class FakeSandbox(MemorySandbox):
        def __init__(self):
            super().__init__()
            self.container = Mock(attrs={'Image':'sha256:'+'a'*64})
    monkeypatch.setattr(loop, 'DockerSandbox', FakeSandbox)
    monkeypatch.setattr(loop, 'settings', replace(loop.settings, durable_checkpoints_enabled=True))
    monkeypatch.setattr(cp, 'export_workspace', lambda sandbox: (dict(sandbox.files), {}, []))
    class Crash(BaseException): pass
    class Gateway:
        connection = SimpleNamespace(context_tokens=48000)
        def __init__(self, turn=0): self.turn = turn
        async def agent_step(self, *, messages):
            self.turn += 1
            steps = [{'id':'make','title':'Make','status':'in_progress','evidence':'','output_refs':['/workspace/output/result.txt']},
                {'id':'verify','title':'Verify','status':'pending','evidence':'','depends_on':['make']}]
            base = {'action':'update_plan','goal':'answer 42','steps':steps,'success_criteria':['answer is 42'],'validation_step_id':'verify'}
            if self.turn == 1: action = base
            elif self.turn == 2: action = {'action':'run_verifier','argv':['verify']}
            elif self.turn == 3: raise Crash()
            elif self.turn == 4:
                proof = next(json.loads(x['content'])['payload'] for x in reversed(messages)
                    if isinstance(x.get('content'),str) and x['content'].startswith('{"tool_result": "run_verifier"'))
                action = {'action':'record_validation','verification_id':proof['verification_id'],'status':'passed','summary':'verified','evidence':'observed 42','checks':['answer 42']}
            elif self.turn == 5:
                for step in steps: step.update(status='completed', evidence='verified output')
                action = base
            elif self.turn == 6: action = {'action':'complete_skill','skill_index':1,'evidence':'verified output'}
            else:
                assert self.turn == 7
                action = {'action':'finish','summary':'answer 42','artifacts':['/workspace/output/result.txt']}
            return ModelResult(action, 'scripted', {})
    contexts = [{'name':'Test','version':'1','root':'/workspace/skill','skill_md':'# Answer','runtime_requirements':{}}]
    with SessionLocal() as db:
        task = db.get(WorkflowJob, created['id'])
        original = FakeSandbox()
        with pytest.raises(Crash):
            asyncio.run(loop._run_agent_loop(db,task,original,skill_contexts=contexts,gateway=Gateway(),job_cancelled=lambda:False))
        assert original.calls == [['verify']]
    del original
    with SessionLocal() as db:
        task = db.get(WorkflowJob, created['id'])
        meta, files = cp.load_bundle(task)
        restored = FakeSandbox()
        restored.files = files
        restored.durable_resume = meta['state']
        result = asyncio.run(loop._run_agent_loop(db,task,restored,skill_contexts=contexts,gateway=Gateway(3),job_cancelled=lambda:False))
        assert result[1] == ['/workspace/output/result.txt']
        assert result[2] == 6
        assert restored.calls == []  # Previously completed verification was not replayed.

@pytest.mark.parametrize('snapshot_status', ['ready', 'in_flight'])
def test_reaper_resumes_only_complete_snapshots(client, user_headers, fake_model_gateway, monkeypatch, snapshot_status):
    from dataclasses import replace
    from datetime import timedelta
    from app import config, sandbox_worker as worker
    from app.database import SessionLocal
    from app.models import WorkflowJob, WorkflowJobMemory, AgentRun, JobStatus, utcnow
    from test_workflow_jobs import create_version, sandbox_skill_zip
    monkeypatch.setattr(config, 'settings', replace(config.settings, sandbox_worker_enabled=True))
    monkeypatch.setattr(worker, 'settings', replace(worker.settings, sandbox_worker_enabled=True, sandbox_worker_max_attempts=3))
    _, version = create_version(client,user_headers,slug='snapshot-reaper',package=sandbox_skill_zip())
    created = client.post('/api/v1/jobs',headers=user_headers,data={'version_id':version['id'],'instruction':'recover'}).json()
    first = worker._claim_job('old-worker')
    assert first.job_id == created['id']
    with SessionLocal() as db:
        task = db.get(WorkflowJob, first.job_id)
        if task.memory is None: task.memory = WorkflowJobMemory(data={})
        task.memory.data = {**task.memory.data,'durable_checkpoint':{'status':snapshot_status,'key':'kept'}}
        db.get(AgentRun,first.run_id).lease_expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    worker._recover_interrupted_jobs()
    with SessionLocal() as db:
        task = db.get(WorkflowJob, first.job_id)
        assert task.memory.data['durable_checkpoint']['key'] == 'kept'
        if snapshot_status == 'ready':
            assert task.status == JobStatus.QUEUED
            assert any(e.data.get('restart_policy') == 'checkpoint' for e in task.events)
        else:
            assert task.status == JobStatus.FAILED
            assert task.error_code == 'CHECKPOINT_TOOL_UNCERTAIN'

def test_active_snapshot_survives_orphan_sweep_then_expires(client, user_headers, fake_model_gateway):
    import os
    from datetime import timedelta
    from app.database import SessionLocal
    from app.models import WorkflowJob, WorkflowJobMemory, JobStatus, utcnow
    from app.storage import storage
    from app.storage_lifecycle import cleanup_expired_storage
    from test_workflow_jobs import create_version, sandbox_skill_zip
    _, version = create_version(client,user_headers,slug='snapshot-retention',package=sandbox_skill_zip())
    created = client.post('/api/v1/jobs',headers=user_headers,data={'version_id':version['id'],'instruction':'retain'}).json()
    key = 'checkpoints/test-retention/snapshot.zip'
    storage.put(key,b'snapshot')
    old = (utcnow() - timedelta(days=20)).timestamp()
    os.utime(storage.root/key,(old,old))
    with SessionLocal() as db:
        task = db.get(WorkflowJob,created['id'])
        task.status = JobStatus.RUNNING
        if task.memory is None: task.memory = WorkflowJobMemory(data={})
        task.memory.data = {**task.memory.data,'durable_checkpoint':{'key':key,'size':8,'status':'ready'}}
        db.commit()
    cleanup_expired_storage()
    assert (storage.root/key).exists()
    with SessionLocal() as db:
        task = db.get(WorkflowJob,created['id'])
        task.status = JobStatus.FAILED
        task.finished_at = utcnow() - timedelta(days=20)
        db.commit()
    cleanup_expired_storage()
    assert not (storage.root/key).exists()
    with SessionLocal() as db:
        assert 'durable_checkpoint' not in db.get(WorkflowJob,created['id']).memory.data
