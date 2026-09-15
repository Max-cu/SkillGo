"""Same-worker, same-image sandbox handover at a completed tool-turn boundary.

This is not process checkpointing or recovery after Worker death. Agent state stays
in the Worker; the frozen workspace is copied into a fresh isolated volume.
"""
import asyncio
import hashlib
import io
import json
import tarfile
from pathlib import PurePosixPath
from uuid import uuid4

from .sandbox_runtime import DockerSandbox, SandboxRuntimeError

MAX_BYTES = 256 * 1024 * 1024
MAX_ENTRIES = 10000


def unpack_snapshot(raw):
    files, modes, directories = {}, {}, []
    total = 0
    seen = set()
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:') as archive:
        for entry in archive:
            path = PurePosixPath(entry.name)
            if (path.is_absolute() or '..' in path.parts or not path.parts
                    or path.parts[0] != 'workspace' or '\\' in entry.name):
                raise ValueError('Invalid snapshot path')
            name = '/' + str(path)
            if name in seen or len(seen) >= MAX_ENTRIES:
                raise ValueError('Duplicate or oversized snapshot')
            seen.add(name)
            if not (entry.isdir() or entry.isfile()):
                raise ValueError('Snapshot contains links or special files')
            modes[name] = entry.mode & 0o777
            if entry.isdir():
                directories.append(name)
            else:
                total += entry.size
                if total > MAX_BYTES:
                    raise ValueError('Snapshot exceeds size limit')
                files[name] = archive.extractfile(entry).read()
    return files, modes, directories


def export_workspace(sandbox):
    helper = None
    try:
        # Frozen source; helper cannot modify it and never receives credentials.
        helper = sandbox.client.containers.create(
            image=sandbox.container.attrs['Image'], command=['sleep', 'infinity'],
            user='10001:10001', network_mode='none', read_only=True,
            volumes={sandbox.volume.name: {'bind': '/workspace', 'mode': 'ro'}},
            cap_drop=['ALL'], security_opt=['no-new-privileges:true'],
            mem_limit='128m', pids_limit=16,
            labels={'skillgo.stager': 'true', 'skillgo.job_id': sandbox.job_id,
                    'skillgo.execution_id': sandbox.execution_id})
        helper.start()
        chunks, _ = helper.get_archive('/workspace')
        raw = bytearray()
        for chunk in chunks:
            raw.extend(chunk)
            if len(raw) > MAX_BYTES + MAX_ENTRIES * 2048:
                raise ValueError('Snapshot archive exceeds size limit')
        return unpack_snapshot(raw)
    finally:
        if helper is not None:
            helper.remove(force=True)


async def replace_sandbox(sandbox, cancelled, *, image_id=None, validate_candidate=None):
    candidate = None
    frozen = False
    try:
        sandbox.container.reload()
        image = image_id or sandbox.container.attrs['Image']
        await asyncio.to_thread(sandbox.container.pause)
        frozen = True
        files, modes, directories = await asyncio.to_thread(export_workspace, sandbox)
        if cancelled():
            raise RuntimeError('Task cancelled before restore')
        candidate = DockerSandbox(sandbox.client, job_id=sandbox.job_id,
            execution_id=sandbox.execution_id + '-r' + uuid4().hex[:8],
            network_enabled=sandbox.network_enabled, image_id=image)
        await asyncio.to_thread(candidate.start)
        await asyncio.to_thread(candidate.put_files, files)
        manifest = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
        # Validate bytes before adopting; no user-authored script is executed.
        code = ('import hashlib,json,os\n'
                'spec=json.loads(' + repr(json.dumps([manifest, modes, directories])) + ')\n'
                'for p in spec[2]: os.makedirs(p,exist_ok=True)\n'
                'for p,h in spec[0].items():\n'
                ' assert hashlib.sha256(open(p,"rb").read()).hexdigest()==h,p\n'
                'for p,m in sorted(spec[1].items(),key=lambda x:len(x[0]),reverse=True): os.chmod(p,m)\n')
        result = await candidate.command(['python3', '-I', '-c', code],
            timeout_seconds=90, allow_large_arguments=True)
        if result.exit_code:
            raise ValueError('Restored workspace failed integrity verification')
        if validate_candidate is not None:
            await validate_candidate(candidate)
        if cancelled():
            raise RuntimeError('Task cancelled before sandbox handover')
        # Adoption preserves the caller's object and its context-manager cleanup.
        old_container, old_volume = sandbox.container, sandbox.volume
        sandbox.container, sandbox.volume = candidate.container, candidate.volume
        sandbox.image, sandbox.execution_id = image, candidate.execution_id
        candidate.container, candidate.volume = old_container, old_volume
        frozen = False
        await asyncio.to_thread(candidate.close)
        candidate = None
        return {'image_id': image, 'file_count': len(files),
                'bytes': sum(map(len, files.values())),
                'manifest_sha256': hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()}
    finally:
        if candidate is not None:
            await asyncio.to_thread(candidate.close)
        if frozen:
            await asyncio.to_thread(sandbox.container.unpause)
