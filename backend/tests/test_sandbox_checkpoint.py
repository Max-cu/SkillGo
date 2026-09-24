import asyncio
import io
import tarfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
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


def _stream_parse(raw, chunk_size=7):
    """Feed tar bytes like docker get_archive: fragmented, single-pass."""
    chunks = (raw[i:i + chunk_size] for i in range(0, len(raw), chunk_size))
    with tarfile.open(fileobj=cp._ChunkReader(chunks), mode='r|') as stream:
        return cp._collect_entries(stream)


def test_streaming_parse_matches_random_access():
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode='w') as t:
        for i in range(5):
            info = tarfile.TarInfo(f'workspace/d/sub{i}.txt')
            payload = (f'f{i}-' * 100).encode()
            info.size = len(payload)
            t.addfile(info, io.BytesIO(payload))
        d = tarfile.TarInfo('workspace/d'); d.type = tarfile.DIRTYPE; d.mode = 0o750
        t.addfile(d)
    blob = raw.getvalue()
    files, modes, dirs = _stream_parse(blob)
    assert len(files) == 5
    assert files['/workspace/d/sub4.txt'] == (b'f4-' * 100)
    assert dirs == ['/workspace/d'] and modes['/workspace/d'] == 0o750
    # Same validation rules as the random-access parser.
    with pytest.raises(ValueError): _stream_parse(archive('workspace/../x'))


def test_streaming_reader_enforces_archive_limit():
    blob = archive()
    chunks = (blob[i:i + 4] for i in range(4, len(blob), 4))
    reader = cp._ChunkReader(chunks, limit=10)
    with pytest.raises(ValueError, match='size limit'):
        with tarfile.open(fileobj=reader, mode='r|') as stream:
            cp._collect_entries(stream)


def _tar_of(entries):
    """entries: list of (arcname, bytes) regular files plus dir names."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w') as t:
        for name, payload in entries:
            info = tarfile.TarInfo(name)
            info.mode = 0o640
            if payload is None:
                info.type = tarfile.DIRTYPE
                t.addfile(info)
            else:
                info.size = len(payload)
                t.addfile(info, io.BytesIO(payload))
    return out.getvalue()


def test_snapshot_excludes_immutable_package_files_but_keeps_generated_ones():
    blob = _tar_of([
        ('workspace/', None),
        ('workspace/skills/', None),
        ('workspace/skills/01-x/', None),
        ('workspace/skills/01-x/SKILL.md', b'package'),
        ('workspace/skills/01-x/scripts/a.py', b'print(1)'),
        ('workspace/skills/01-x/exports/', None),
        ('workspace/skills/01-x/exports/deck.pptx', b'PK-agent-work'),
        ('workspace/work/', None),
        ('workspace/work/notes.txt', b'notes'),
    ])
    excluded = frozenset({
        '/workspace/skills/01-x/SKILL.md',
        '/workspace/skills/01-x/scripts/a.py',
    })
    files, _modes, dirs = cp.unpack_snapshot(blob, excluded=excluded)
    assert files == {
        '/workspace/skills/01-x/exports/deck.pptx': b'PK-agent-work',
        '/workspace/work/notes.txt': b'notes',
    }
    assert '/workspace/skills/01-x/exports' in dirs


def test_snapshot_entry_bound_applies_only_to_non_immutable_files():
    entries: list[tuple[str, bytes | None]] = [('workspace/', None), ('workspace/skills/', None)]
    for i in range(cp.MAX_ENTRIES + 5):
        entries.append((f'workspace/skills/s{i}.md', b'x'))
    blob = _tar_of(entries)
    excluded = frozenset(f'/workspace/skills/s{i}.md' for i in range(cp.MAX_ENTRIES + 5))
    files, _modes, _dirs = cp.unpack_snapshot(blob, excluded=excluded)
    assert files == {}


def test_snapshot_directory_entries_do_not_count_against_file_cap():
    # Package trees contain many directories; they carry no bytes and are
    # bounded independently of the mutable-file budget (246 dirs vs 12.7k
    # files in ppt-master). Use a small monkeypatched bound to prove the
    # separation without building ten thousand tar entries.
    entries: list[tuple[str, bytes | None]] = [('workspace/', None)]
    for i in range(3):
        entries.append((f'workspace/skills/pkg/d{i}/', None))
    blob = _tar_of(entries)
    files, _modes, dirs = cp.unpack_snapshot(blob)
    assert files == {}
    assert len(dirs) == 4  # workspace + skills/pkg parents + 3 leaf dirs


def test_snapshot_directory_cap_is_independent_and_enforced(monkeypatch):
    monkeypatch.setattr(cp, 'MAX_ENTRIES', 2)
    entries = [('workspace/', None)] + [
        (f'workspace/d{i}/', None) for i in range(3)
    ]
    with pytest.raises(ValueError):
        cp.unpack_snapshot(_tar_of(entries))


def test_snapshot_excludes_immutable_input_files():
    # /workspace/input is platform-provisioned and re-staged on restore, so
    # the Worker registers exact input paths in the immutable exclusion set.
    blob = _tar_of([
        ('workspace/input/', None),
        ('workspace/input/source.pdf', b'PDFBYTES'),
        ('workspace/work/notes.txt', b'notes'),
    ])
    files, _modes, _dirs = cp.unpack_snapshot(
        blob, excluded=frozenset({'/workspace/input/source.pdf'}))
    assert files == {'/workspace/work/notes.txt': b'notes'}


def test_handover_restages_immutable_inputs_before_first_command(monkeypatch):
    old = SimpleNamespace(container=Mock(attrs={'Image': 'sha256:fixed'}), volume=Mock(),
                          client=Mock(), job_id='job', execution_id='attempt', network_enabled=False)
    candidate = SimpleNamespace(container=Mock(), volume=Mock(), execution_id='new',
                                start=Mock(), close=Mock())
    order = []
    candidate.put_files = Mock(side_effect=lambda files: order.append(('put', sorted(files))))
    async def command(*args, **kwargs):
        order.append(('cmd',))
        return SimpleNamespace(exit_code=0)
    candidate.command = command

    async def provision(target):
        await asyncio.to_thread(target.put_files, {'input/source.pdf': b'PDFBYTES'})

    old.provision_inputs = provision
    monkeypatch.setattr(cp, 'DockerSandbox', lambda *args, **kw: candidate)
    monkeypatch.setattr(cp, 'export_workspace',
                        lambda _: ({'/workspace/output/a': b'a'}, {}, []))
    asyncio.run(cp.replace_sandbox(old, lambda: False))
    # Hook is carried onto the candidate and input staging happens before the
    # first command (put_files is rejected once the container has started).
    assert candidate.provision_inputs is provision
    first_cmd = next(i for i, ev in enumerate(order) if ev[0] == 'cmd')
    input_put = next(i for i, ev in enumerate(order) if ev[0] == 'put' and 'input/source.pdf' in ev[1])
    assert input_put < first_cmd
    staged = {k for keys in (ev[1] for ev in order if ev[0] == 'put') for k in keys}
    assert {'input/source.pdf', '/workspace/output/a'} <= staged


def _oversized_export_sandbox(helper, monkeypatch):
    """Fake sandbox whose archive parse always trips the size cap."""
    helper.get_archive.return_value = (iter([archive()]), None)
    sandbox = SimpleNamespace(
        client=SimpleNamespace(containers=SimpleNamespace(create=lambda **kw: helper)),
        container=Mock(attrs={'Image': 'sha256:abc'}),
        volume=SimpleNamespace(name='vol'),
        job_id='job', execution_id='attempt',
    )

    def raise_size_limit(archive, excluded=frozenset()):
        raise ValueError('Snapshot exceeds size limit')

    monkeypatch.setattr(cp, '_collect_entries', raise_size_limit)
    return sandbox


def test_helper_cleanup_retries_and_keeps_primary_error(monkeypatch):
    # First delete stalls (the 60s ReadTimeout from the live incident); the
    # retry succeeds. The export's size-cap ValueError must be what propagates.
    helper = Mock()
    attempts = []

    def remove(**kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise requests.exceptions.ReadTimeout('docker stalled')

    helper.remove.side_effect = remove
    sandbox = _oversized_export_sandbox(helper, monkeypatch)
    with pytest.raises(ValueError, match='Snapshot exceeds size limit'):
        cp.export_workspace(sandbox)
    assert len(attempts) == 2
    assert attempts[0]['timeout'] == cp.HELPER_REMOVE_TIMEOUT
    helper.start.assert_called_once()


def test_helper_cleanup_failure_never_masks_primary_error(monkeypatch):
    helper = Mock()
    helper.remove.side_effect = requests.exceptions.ReadTimeout('docker stalled')
    sandbox = _oversized_export_sandbox(helper, monkeypatch)
    with pytest.raises(ValueError, match='Snapshot exceeds size limit'):
        cp.export_workspace(sandbox)
    assert helper.remove.call_count == 2


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
