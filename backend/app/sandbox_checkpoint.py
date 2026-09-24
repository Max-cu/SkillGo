"""Same-worker, same-image sandbox handover at a completed tool-turn boundary.

This is not process checkpointing or recovery after Worker death. Agent state stays
in the Worker; the frozen workspace is copied into a fresh isolated volume.
"""
import asyncio
import hashlib
import io
import json
import logging
import tarfile
from pathlib import PurePosixPath
from uuid import uuid4

from .sandbox_runtime import DockerSandbox, SandboxRuntimeError

logger = logging.getLogger(__name__)

# Bounded against the 2 GiB worker container: export materializes files in
# memory (~1x) and restore builds one tar via put_files (~1.2x peak). 512 MiB
# covers legitimately heavy runs (embedded-image PPTX, batch OCR) while
# keeping restore peak comfortably under the worker memory quota.
MAX_BYTES = 512 * 1024 * 1024
MAX_ENTRIES = 10000
# Tar block + per-header overhead allowance on top of plain file bytes.
MAX_ARCHIVE_BYTES = MAX_BYTES + MAX_ENTRIES * 2048

# Docker daemon teardown of the frozen-view helper occasionally stalls under
# load; the client default (60s) killed the delete and masked the real export
# error. Give it room and retry once before leaving the helper for GC.
HELPER_REMOVE_TIMEOUT = 180


class _ChunkReader:
    """Adapt an iterator of byte chunks to the fileobj interface tarfile needs.

    Streaming the docker get_archive generator straight into tarfile avoids
    holding the whole tar (and a second parsed copy) in worker memory at once.
    """

    def __init__(self, chunks, *, limit=MAX_ARCHIVE_BYTES):
        self._chunks = iter(chunks)
        self._buffer = bytearray()
        self._exhausted = False
        self._limit = limit
        self.consumed = 0

    def read(self, size=-1):
        if size is None or size < 0:
            while not self._exhausted:
                try:
                    self._extend(next(self._chunks))
                except StopIteration:
                    self._exhausted = True
            data = bytes(self._buffer)
            self._buffer = bytearray()
            return data
        while len(self._buffer) < size and not self._exhausted:
            try:
                self._extend(next(self._chunks))
            except StopIteration:
                self._exhausted = True
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data

    def _extend(self, chunk):
        self._buffer.extend(chunk)
        self.consumed += len(chunk)
        if self.consumed > self._limit:
            raise ValueError('Snapshot archive exceeds size limit')


def _collect_entries(archive, *, excluded: frozenset[str] = frozenset()):
    """Validate and materialize one sequential tar stream as path -> bytes."""

    files, modes, directories = {}, {}, []
    total = 0
    seen_files: set[str] = set()
    seen_dirs: set[str] = set()
    for entry in archive:
        path = PurePosixPath(entry.name)
        if (path.is_absolute() or '..' in path.parts or not path.parts
                or path.parts[0] != 'workspace' or '\\' in entry.name):
            raise ValueError('Invalid snapshot path')
        name = '/' + str(path)
        # Platform-provisioned immutable Skill files are re-staged from the
        # original package on restore; skipping them keeps asset-rich skills
        # (thousands of small files) out of the bounded mutable snapshot.
        if name in excluded:
            continue
        if not (entry.isdir() or entry.isfile()):
            raise ValueError('Snapshot contains links or special files')
        modes[name] = entry.mode & 0o777
        if entry.isdir():
            # Directories carry no bytes and an asset-rich package tree may
            # contain hundreds; bound them separately from the file cap so
            # package directories cannot exhaust the mutable-file budget.
            if name in seen_dirs or len(seen_dirs) >= MAX_ENTRIES:
                raise ValueError('Duplicate or oversized snapshot')
            seen_dirs.add(name)
            directories.append(name)
        else:
            if name in seen_files or len(seen_files) >= MAX_ENTRIES:
                raise ValueError('Duplicate or oversized snapshot')
            seen_files.add(name)
            total += entry.size
            if total > MAX_BYTES:
                raise ValueError('Snapshot exceeds size limit')
            files[name] = archive.extractfile(entry).read()
    return files, modes, directories


def unpack_snapshot(raw, *, excluded: frozenset[str] = frozenset()):
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:') as archive:
        return _collect_entries(archive, excluded=excluded)


async def reprovision_input_files(sandbox) -> None:
    """Re-stage immutable user inputs (/workspace/input) after a restore.

    Snapshots deliberately omit platform-provisioned input files; the Worker
    attaches an async ``provision_inputs`` hook that re-reads the originals
    from object storage. Must run while the container is still unstarted
    (put_files rejects staging once any command exists).
    """
    provider = getattr(sandbox, 'provision_inputs', None)
    if provider is not None:
        await provider(sandbox)


async def reprovision_skill_packages(sandbox) -> None:
    """Re-stage immutable Skill zips and extract them into /workspace/skills.

    Runs on a freshly restored or replaced sandbox whose mutable snapshot
    deliberately omitted platform-provisioned package files.
    """

    packages = getattr(sandbox, 'provisioned_packages', None) or {}
    if packages:
        await asyncio.to_thread(sandbox.put_files, packages)
    for archive_path, extract_root in getattr(sandbox, 'provisioned_extractions', []):
        result = await sandbox.command(
            [
                "python3",
                "-c",
                "import sys,zipfile;zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])",
                archive_path,
                extract_root,
            ],
            timeout_seconds=180,
        )
        if result.exit_code:
            raise SandboxRuntimeError(
                "SANDBOX_PACKAGE_SETUP_FAILED",
                getattr(result, 'stderr', '') or f"Could not unpack Skill package into {extract_root}",
            )


def _remove_helper(helper) -> None:
    """Best-effort teardown of the frozen-view helper container.

    Cleanup runs in ``export_workspace``'s ``finally``; a Docker transport
    stall here must never replace the export's primary exception (it once hid
    a Snapshot-exceeds-size-limit ValueError behind a ReadTimeout). A helper
    that survives is picked up by later workspace cleanup.
    """
    for attempt in (1, 2):
        try:
            helper.remove(force=True, timeout=HELPER_REMOVE_TIMEOUT)
            return
        except Exception:
            logger.warning(
                "checkpoint helper remove failed (attempt %d/2)", attempt, exc_info=True)


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
        excluded = getattr(sandbox, 'immutable_workspace_paths', None) or frozenset()
        with tarfile.open(fileobj=_ChunkReader(chunks), mode='r|') as archive:
            return _collect_entries(archive, excluded=excluded)
    finally:
        if helper is not None:
            _remove_helper(helper)


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
        # Carry the immutable package provisioning contract so the fresh
        # candidate gets the Skill files back after adopting the mutable state.
        candidate.provisioned_packages = getattr(sandbox, 'provisioned_packages', None) or {}
        candidate.provisioned_extractions = list(getattr(sandbox, 'provisioned_extractions', None) or [])
        candidate.provision_inputs = getattr(sandbox, 'provision_inputs', None)
        await asyncio.to_thread(candidate.start)
        await asyncio.to_thread(candidate.put_files, files)
        # Inputs (and packages below) use put_files, which is rejected once the
        # container starts; package extraction runs the first command, so all
        # staging must happen before reprovision_skill_packages.
        await reprovision_input_files(candidate)
        await reprovision_skill_packages(candidate)
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
