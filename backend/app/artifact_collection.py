"""Collect explicitly declared Skill outputs without imposing export directories."""
from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath

from .artifact_validation import normalize_artifact_paths, _sanitize_name_segment
from .config import settings
from .sandbox_runtime import SandboxRuntimeError


def delivery_only_job(job) -> bool:
    """Deployment policy, never an Agent- or browser-controlled exemption."""
    selected = {item['skill_id'] for item in getattr(job, 'selected_skills', [])} or {getattr(job, 'skill_id', '')}
    return selected.issubset(set(settings.delivery_only_skill_ids))


# Executed inside the job sandbox. Directory descriptors and O_NOFOLLOW keep
# both source reads and destination writes safe against symlink races.
COPY_ARTIFACTS_PROGRAM = r'''
import errno, json, os, stat, sys, uuid

items = json.loads(sys.argv[1])
limit = int(sys.argv[2])
root_fd = os.open('/workspace', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

def directory(parts, create=False):
    fd = os.dup(root_fd)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise

try:
    for source, target in items:
        source_parts = source.removeprefix('/workspace/').split('/')
        target_parts = target.removeprefix('/workspace/').split('/')
        source_dir = directory(source_parts[:-1])
        target_dir = None
        temporary = None
        try:
            source_fd = os.open(source_parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source_dir)
            with os.fdopen(source_fd, 'rb') as reader:
                info = os.fstat(reader.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError('Declared artifact is not a regular file: ' + source)
                if not 0 < info.st_size <= limit:
                    raise OverflowError('Declared artifact is empty or exceeds the size limit: ' + source)
                target_dir = directory(target_parts[:-1], create=True)
                temporary = '.collect-' + uuid.uuid4().hex
                target_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                    0o600, dir_fd=target_dir)
                size = 0
                with os.fdopen(target_fd, 'wb') as writer:
                    while True:
                        chunk = reader.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > limit:
                            raise OverflowError('Artifact exceeded the size limit during collection: ' + source)
                        writer.write(chunk)
                after = os.fstat(reader.fileno())
                if size != info.st_size or (after.st_size, after.st_mtime_ns) != (info.st_size, info.st_mtime_ns):
                    raise ValueError('Artifact changed during collection: ' + source)
                os.replace(temporary, target_parts[-1], src_dir_fd=target_dir, dst_dir_fd=target_dir)
                temporary = None
        finally:
            os.close(source_dir)
            if target_dir is not None:
                if temporary is not None:
                    os.unlink(temporary, dir_fd=target_dir)
                os.close(target_dir)
    print(json.dumps({'ok': True}))
except FileNotFoundError as exc:
    print(json.dumps({'ok': False, 'error_code': 'SANDBOX_ARTIFACT_MISSING', 'message': str(exc)}))
except OverflowError as exc:
    print(json.dumps({'ok': False, 'error_code': 'SANDBOX_ARTIFACT_SIZE', 'message': str(exc)}))
except (OSError, ValueError) as exc:
    print(json.dumps({'ok': False, 'error_code': 'SANDBOX_ARTIFACT_DENIED', 'message': str(exc)}))
finally:
    os.close(root_fd)
'''


def artifact_source_paths(paths: list[str], *, cwd: str = '/workspace') -> list[str]:
    if not isinstance(paths, list) or not 1 <= len(paths) <= 50:
        raise SandboxRuntimeError('SANDBOX_ARTIFACT_DENIED', 'Declare 1-50 artifact files')
    resolved = []
    for raw in paths:
        if not isinstance(raw, str) or not raw or len(raw) > 1024 or '\x00' in raw:
            raise SandboxRuntimeError('SANDBOX_ARTIFACT_DENIED', 'Invalid artifact path')
        path = PurePosixPath(raw)
        if not path.is_absolute():
            # Existing platform-relative output paths keep their old meaning.
            base = '/workspace' if path.parts[0] in {'output', 'work', 'skills'} else cwd
            path = PurePosixPath(base) / path
        if '..' in path.parts or not path.is_relative_to('/workspace') or path == PurePosixPath('/workspace'):
            raise SandboxRuntimeError('SANDBOX_ARTIFACT_DENIED', 'Artifact sources must stay below /workspace')
        resolved.append(str(path))
    return list(dict.fromkeys(resolved))


async def collect_declared_artifacts(sandbox, paths: list[str], *, cwd: str = '/workspace') -> dict:
    """Copy declared regular workspace files to deterministic delivery paths.

    Never scan package assets or auto-select files. Existing output paths remain
    unchanged; exporting the same source again replaces its delivery copy so
    byte-bound validation notices mutations and must be repeated.
    """
    sources = artifact_source_paths(paths, cwd=cwd)
    mapping = {}
    copies = []
    for source in sources:
        path = PurePosixPath(source)
        if path.is_relative_to('/workspace/output'):
            target = normalize_artifact_paths([source])[0]
        else:
            folder = hashlib.sha256(source.encode()).hexdigest()[:16]
            target = f'/workspace/output/collected/{folder}/{_sanitize_name_segment(path.name)}'
            copies.append([source, target])
        mapping[source] = target
    if copies:
        result = await sandbox.command(
            ['python3', '-c', COPY_ARTIFACTS_PROGRAM, json.dumps(copies), str(settings.sandbox_max_artifact_bytes)],
            cwd='/workspace', timeout_seconds=120, allow_large_arguments=True,
        )
        try:
            report = json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise SandboxRuntimeError('SANDBOX_ARTIFACT_COLLECTION_FAILED', 'Could not collect declared artifact files') from exc
        if result.exit_code != 0 or report.get('ok') is not True:
            raise SandboxRuntimeError(report.get('error_code') or 'SANDBOX_ARTIFACT_COLLECTION_FAILED',
                                      report.get('message') or 'Could not collect declared artifact files')
    return {'artifacts': list(mapping.values()), 'artifact_sources': mapping}


async def receive_declared_artifacts(sandbox, paths: list[str], *, cwd: str = '/workspace') -> dict:
    """Turn correctable declaration errors into tool results, not job failures."""
    if not paths:
        return {'ok': False, 'error_code': 'SANDBOX_ARTIFACT_MISSING',
                'message': 'Workflow finish requires at least one declared output artifact.',
                'hint': 'Generate the deliverable and declare its exact workspace file path in finish.'}
    try:
        return {'ok': True, **await collect_declared_artifacts(sandbox, paths, cwd=cwd)}
    except SandboxRuntimeError as exc:
        return {'ok': False, 'error_code': exc.code, 'message': str(exc)[:1000],
                'hint': 'Declare existing regular files inside /workspace, not directories or symbolic links. '
                        'Skill exports do not need to be moved manually; the platform collects them.'}
