import asyncio
import io
import tarfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from app import sandbox_checkpoint as cp


def archive(name='workspace/work/test.txt', kind=tarfile.REGTYPE):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w') as t:
        item = tarfile.TarInfo(name)
        item.type = kind
        item.mode = 0o750
        item.size = 3 if kind == tarfile.REGTYPE else 0
        t.addfile(item, io.BytesIO(b'abc') if item.size else None)
    return out.getvalue()


def test_snapshot_preserves_bytes_and_permissions():
    files, modes, dirs = cp.unpack_snapshot(archive())
    assert files == {'/workspace/work/test.txt': b'abc'}
    assert modes['/workspace/work/test.txt'] == 0o750


@pytest.mark.parametrize('name,kind', [
    ('/etc/passwd', tarfile.REGTYPE), ('workspace/../escape', tarfile.REGTYPE),
    ('workspace/link', tarfile.SYMTYPE), ('workspace/hard', tarfile.LNKTYPE),
    ('workspace/pipe', tarfile.FIFOTYPE),
])
def test_snapshot_rejects_unsafe_entries(name, kind):
    with pytest.raises(ValueError): cp.unpack_snapshot(archive(name, kind))


def test_snapshot_has_size_bound(monkeypatch):
    monkeypatch.setattr(cp, 'MAX_BYTES', 2)
    with pytest.raises(ValueError): cp.unpack_snapshot(archive())


@pytest.mark.parametrize('failure', ['none', 'copy', 'verify', 'cancel'])
def test_handover_adopts_only_verified_candidate(monkeypatch, failure):
    old = SimpleNamespace(container=Mock(attrs={'Image': 'sha256:fixed'}), volume=Mock(),
                          client=Mock(), job_id='job', execution_id='attempt', network_enabled=False)
    original = old.container
    candidate = SimpleNamespace(container=Mock(), volume=Mock(), execution_id='new',
                                start=Mock(), put_files=Mock(), close=Mock())
    replacement = candidate.container
    async def command(*args, **kwargs): return SimpleNamespace(exit_code=1 if failure == 'verify' else 0)
    candidate.command = command
    if failure == 'copy': candidate.put_files.side_effect = RuntimeError('copy failed')
    monkeypatch.setattr(cp, 'DockerSandbox', lambda *args, **kw: candidate)
    monkeypatch.setattr(cp, 'export_workspace', lambda _: ({'/workspace/a': b'a'}, {}, []))
    if failure == 'none':
        result = asyncio.run(cp.replace_sandbox(old, lambda: False))
        assert old.container is replacement and result['file_count'] == 1
        original.unpause.assert_not_called()
    else:
        with pytest.raises((ValueError, RuntimeError)):
            asyncio.run(cp.replace_sandbox(old, lambda: failure == 'cancel'))
        assert old.container is original
        original.unpause.assert_called_once()
    if failure != 'cancel': candidate.close.assert_called_once()


def test_restart_request_requires_running_owner_and_is_idempotent(client, user_headers, fake_model_gateway):
    from test_workflow_jobs import create_version, sandbox_skill_zip
    from app.database import SessionLocal
    from app.models import WorkflowJob, JobStatus, JobEvent
    from sqlalchemy import select
    _, version = create_version(client, user_headers, slug='restart-test', package=sandbox_skill_zip())
    response = client.post('/api/v1/jobs', headers=user_headers,
                           data={'version_id': version['id'], 'instruction': 'test'})
    job_id = response.json()['id']
    url = f'/api/v1/jobs/{job_id}/sandbox/restart'
    assert client.post(url, headers=user_headers).status_code == 409
    with SessionLocal() as db:
        job = db.get(WorkflowJob, job_id)
        job.status = JobStatus.RUNNING
        db.commit()
    assert client.post(url).status_code == 401
    assert client.post(url, headers=user_headers).status_code == 200
    assert client.post(url, headers=user_headers).status_code == 200
    with SessionLocal() as db:
        requests = db.scalars(select(JobEvent).where(JobEvent.job_id == job_id,
                            JobEvent.event_type == 'sandbox_restart')).all()
        assert len(requests) == 1 and requests[0].status == 'queued'


def test_agent_continues_next_turn_with_previous_result(client, user_headers, fake_model_gateway, monkeypatch):
    from test_workflow_jobs import create_version, sandbox_skill_zip
    from test_orchestration import MemorySandbox
    from app.database import SessionLocal
    from app.models import WorkflowJob, JobEvent
    from app.workflow_execution import add_job_event
    from app.model_gateway import ModelResult
    from app import sandbox_agent_loop as loop
    from sqlalchemy import select
    _, version = create_version(client, user_headers, slug='handover-loop', package=sandbox_skill_zip())
    job_id = client.post('/api/v1/jobs', headers=user_headers,
                        data={'version_id': version['id'], 'instruction': 'test'}).json()['id']
    switched = []
    async def replace(sandbox, cancelled):
        switched.append(True)
        return {'file_count': 0}
    monkeypatch.setattr(loop, 'replace_sandbox', replace)
    class Done(Exception): pass
    class Gateway:
        connection = SimpleNamespace(context_tokens=48000)
        turn = 0
        async def agent_step(self, *, messages):
            self.turn += 1
            if self.turn == 1:
                with SessionLocal() as session:
                    add_job_event(session, session.get(WorkflowJob, job_id), 'sandbox_restart',
                                  'restart', '', status='queued')
                    session.commit()
                return ModelResult({'action': 'read_skill', 'skill_index': 1}, 'test', {})
            assert switched == [True]
            assert any('read_skill' in str(m) for m in messages)
            raise Done()
    with SessionLocal() as db:
        sandbox = MemorySandbox()
        sandbox.container = object()
        with pytest.raises(Done):
            asyncio.run(loop._run_agent_loop(db, db.get(WorkflowJob, job_id), sandbox,
                skill_contexts=[{'name': 'test', 'version': '1', 'root': '/workspace/skill',
                                 'skill_md': '# test', 'runtime_requirements': {}}],
                gateway=Gateway(), job_cancelled=lambda: False))
        event = db.scalars(select(JobEvent).where(JobEvent.job_id == job_id,
                           JobEvent.event_type == 'sandbox_restart')).one()
        assert event.status == 'succeeded'
        assert event.data['next_turn'] == 2 and event.data['completed_operations'] == 1
