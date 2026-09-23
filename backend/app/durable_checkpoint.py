"""Bounded, authenticated task snapshots on shared persistent object storage.

No pickle and no process memory. An in-flight tool is never automatically replayed.
"""
import asyncio
import hashlib
import hmac
import json
import os
import re
import zipfile
from dataclasses import fields
from pathlib import PurePosixPath
from uuid import uuid4

from .config import settings
from .storage import storage
from .agent_policy import AgentExecutionState
from .sandbox_checkpoint import export_workspace, MAX_BYTES, MAX_ENTRIES
from .sandbox_runtime import SandboxRuntimeError

MAX_STATE = 16 * 1024 * 1024
MAX_ARCHIVE = MAX_BYTES + MAX_STATE + MAX_ENTRIES * 2048


def state_to_json(state):
    result = {f.name: getattr(state, f.name) for f in fields(state) if f.name != '_observation_cache'}
    for name in ('loaded_skills', 'completed_skill_indexes'):
        result[name] = sorted(result[name])
    return result


def state_from_json(data):
    values = dict(data)
    for name in ('loaded_skills', 'completed_skill_indexes'):
        values[name] = set(values[name])
    values['skill_evidence'] = {int(k): v for k, v in values['skill_evidence'].items()}
    return AgentExecutionState(**values)


def _snapshot_key():
    return hashlib.sha256(('skillgo-task-snapshot-v1:' + settings.jwt_secret).encode()).digest()


def signature(blob, user_id, job_id):
    return hmac.new(_snapshot_key(), (user_id + ':' + job_id + ':').encode() + blob,
                    hashlib.sha256).hexdigest()


def _file_signature(path, user_id, job_id):
    mac = hmac.new(_snapshot_key(), (user_id + ':' + job_id + ':').encode(), hashlib.sha256)
    with open(path, 'rb') as blob:
        for chunk in iter(lambda: blob.read(1024 * 1024), b''):
            mac.update(chunk)
    return mac.hexdigest()


def write_bundle(job, files, modes, directories, state, image):
    if sum(len(data) for data in files.values()) > MAX_BYTES:
        raise ValueError('Task snapshot workspace exceeds limits')
    encoded = json.dumps({'schema': 1, 'user_id': job.user_id, 'job_id': job.id,
        'image': image, 'network': bool(job.network_enabled), 'state': state,
        'modes': modes, 'directories': directories,
        'manifest': {p: hashlib.sha256(b).hexdigest() for p, b in files.items()}}, ensure_ascii=False).encode()
    if len(encoded) > MAX_STATE:
        raise ValueError('Agent snapshot state exceeds 16 MiB')
    key = f'checkpoints/{job.user_id}/{job.id}/{uuid4().hex}.zip'
    target = storage.root / key
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Unique immutable object; DB pointer is published only after fsync/rename.
    # Stream straight to disk: an in-memory zip held up to ~4x workspace bytes
    # and OOM-killed 512m workers during checkpoint turns.
    temporary = target.with_suffix('.partial')
    try:
        with temporary.open('xb') as output:
            os.chmod(temporary, 0o600)
            with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED,
                                 allowZip64=True) as archive:
                for path, data in files.items():
                    archive.writestr('files/' + path.removeprefix('/workspace/'), data)
                # state.json last: its manifest only becomes known after the
                # file entries are written; readers fetch it by name, not order.
                archive.writestr('state.json', encoded)
                if output.tell() > MAX_ARCHIVE:
                    raise ValueError('Task snapshot exceeds size limit')
            output.flush(); os.fsync(output.fileno())
            size = output.tell()
        digest = _file_signature(temporary, job.user_id, job.id)
        os.replace(temporary, target)
        if os.name != 'nt':
            fd = os.open(target.parent, os.O_DIRECTORY)
            try: os.fsync(fd)
            finally: os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)
    return {'key': key, 'signature': digest,
            'size': size, 'status': 'ready', 'next_turn': state['next_turn']}


def load_bundle(job):
    ref = (job.memory.data or {}).get('durable_checkpoint') if job.memory else None
    if not ref:
        return None
    if ref.get('status') != 'ready':
        raise SandboxRuntimeError('CHECKPOINT_TOOL_UNCERTAIN', 'Worker stopped during a tool turn; automatic replay is disabled')
    prefix = f'checkpoints/{job.user_id}/{job.id}/'
    if not re.fullmatch(re.escape(prefix) + r'[0-9a-f]{32}\.zip', ref.get('key', '')):
        raise ValueError('Snapshot owner/path mismatch')
    path = storage.root / ref['key']
    if path.stat().st_size > MAX_ARCHIVE:
        raise ValueError('Snapshot too large')
    if path.stat().st_size != ref['size'] or not hmac.compare_digest(
            _file_signature(path, job.user_id, job.id), ref['signature']):
        raise ValueError('Snapshot authentication failed')
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo('state.json')
        if info.file_size > MAX_STATE:
            raise ValueError('Snapshot state too large')
        meta = json.loads(archive.read(info))
        if (meta['schema'] != 1 or meta['user_id'] != job.user_id or meta['job_id'] != job.id
                or meta['network'] != bool(job.network_enabled)
                or not re.fullmatch(r'sha256:[0-9a-f]{64}', meta['image'])):
            raise ValueError('Snapshot identity/configuration mismatch')
        files = {}
        total = 0
        for name, digest in meta['manifest'].items():
            validate_path(name)
            member = archive.getinfo('files/' + name.removeprefix('/workspace/'))
            total += member.file_size
            if total > MAX_BYTES or len(files) >= MAX_ENTRIES:
                raise ValueError('Snapshot workspace exceeds limits')
            data = archive.read(member)
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError('Snapshot file checksum mismatch')
            files[name] = data
        for path in [*meta['directories'], *meta['modes']]: validate_path(path)
        return meta, files


def validate_path(path):
    p = PurePosixPath(path)
    if not path.startswith('/workspace') or not p.is_relative_to('/workspace') or '..' in p.parts or '\\' in path:
        raise ValueError('Invalid snapshot workspace path')


async def restore_bundle(sandbox, bundle):
    meta, files = bundle
    # Stage only trusted filesystem metadata; keep argv bounded even for 10k files.
    metadata_path = '/workspace/.skillgo-restore-' + uuid4().hex + '.json'
    small = {k: meta[k] for k in ('directories', 'modes', 'manifest')}
    await asyncio.to_thread(sandbox.put_files, {**files, metadata_path: json.dumps(small).encode()})
    # Stage every package before the first command starts the task container.
    from .sandbox_checkpoint import reprovision_skill_packages
    await reprovision_skill_packages(sandbox)
    code = ('import os,json,hashlib\ns=json.load(open(' + repr(metadata_path) + '))\n'
            'for p in s["directories"]: os.makedirs(p,exist_ok=True)\n'
            'for p,h in s["manifest"].items(): assert hashlib.sha256(open(p,"rb").read()).hexdigest()==h,p\n'
            'os.unlink(' + repr(metadata_path) + ')\n'
            'for p,m in sorted(s["modes"].items(),key=lambda x:len(x[0]),reverse=True): os.chmod(p,m)\n')
    result = await sandbox.command(['python3', '-I', '-c', code], timeout_seconds=90)
    if result.exit_code:
        raise ValueError('Restored workspace verification failed')
    sandbox.durable_resume = meta['state']


async def save_checkpoint(db, job, sandbox, state, fence):
    ref = None
    old = (job.memory.data or {}).get('durable_checkpoint')
    await asyncio.to_thread(sandbox.container.pause)
    try:
        files, modes, directories = await asyncio.to_thread(export_workspace, sandbox)
        image = sandbox.container.attrs['Image']
        ref = await asyncio.to_thread(write_bundle, job, files, modes, directories, state, image)
        fence()
        job.memory.data = {**job.memory.data, 'durable_checkpoint': ref}
        db.commit()
    except BaseException:
        db.rollback()
        # Commit acknowledgement can be lost after publication. Leave the object
        # for reference-aware garbage collection, never delete it speculatively.
        raise
    finally:
        await asyncio.to_thread(sandbox.container.unpause)
    if old:
        try: storage.delete(old['key'])
        except OSError: pass  # Orphan sweep retries; new snapshot remains authoritative.


def mark_inflight(db, job, fence):
    ref = (job.memory.data or {}).get('durable_checkpoint')
    if ref:
        fence()
        job.memory.data = {**job.memory.data, 'durable_checkpoint': {**ref, 'status': 'in_flight'}}
        db.commit()
