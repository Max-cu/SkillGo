"""Conservative framework adapter for reviewed final-export workflows.

No Skill edits, filename discovery or model-generated completion markers.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import PurePosixPath

EXPORT_COMMAND = ['python3', 'scripts/export_flat.py']
FINAL_COMMANDS = [['python3', 'scripts/' + name] for name in [
    'validators/check_templates.py', 'cleaners/clear_downstream.py',
    'build_svgs.py', 'validators/verify_build_output.py', 'finalize_svg.py',
    'validators/verify_finalize_output.py', 'export_flat.py',
    'validators/verify_export_output.py', 'cleaners/clear_downstream.py']]
SUCCESS_PREFIX = '✅ PPTX exported: '
BASELINE_PROGRAM = """# skillgo-export-baseline
import json,os,sys
root=sys.argv[1]
try:
 fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
except FileNotFoundError:
 print(json.dumps({'files':[]}))
else:
 try:
  names=os.listdir(fd)
  if len(names)>5000: raise ValueError('Export directory too large')
  print(json.dumps({'files':names}))
 finally: os.close(fd)
"""


def configured_export_command(version_id: str, allowed_versions: tuple, recipe: dict):
    """Explicit version opt-in and exact reviewed recipe, never workflow name alone."""
    if (version_id in allowed_versions and recipe.get('stages') == [3, 4, 5]
            and recipe.get('commands') == FINAL_COMMANDS):
        return EXPORT_COMMAND
    return None


async def export_baseline(sandbox, *, cwd: str) -> set[str] | None:
    from .sandbox_runtime import SandboxRuntimeError
    try:
        result = await sandbox.command(['python3', '-c', BASELINE_PROGRAM, cwd + '/exports'],
                                       cwd='/workspace', timeout_seconds=30, allow_large_arguments=True)
        if result.exit_code != 0 or getattr(result, 'timed_out', False):
            return None
        body = json.loads(result.stdout)
        names = body['files']
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            return None
        return set(names)
    except (ValueError, TypeError, KeyError, SandboxRuntimeError):
        return None


async def capture_export(sandbox, payload: dict, baseline: set[str] | None, *, cwd: str) -> list[dict] | None:
    """Bind a fresh path explicitly reported by this successful exporter invocation."""
    from .artifact_collection import receive_declared_artifacts
    from .sandbox_runtime import SandboxRuntimeError
    if (baseline is None or payload.get('ok') is False or payload.get('exit_code') != 0
            or payload.get('timed_out') or not isinstance(payload.get('stdout'), str)):
        return None
    paths = [line.strip()[len(SUCCESS_PREFIX):] for line in payload['stdout'].splitlines()
             if line.strip().startswith(SUCCESS_PREFIX)]
    if len(paths) != 1:
        return None
    path = PurePosixPath(paths[0])
    if (not path.is_absolute() or path.parent != PurePosixPath(cwd)/'exports'
            or path.suffix != '.pptx' or path.name in baseline or '..' in path.parts):
        return None
    try:
        collection = await receive_declared_artifacts(sandbox, [str(path)], cwd=cwd)
        if collection.get('ok') is False:
            return None
        data = sandbox.download_file(collection['artifacts'][0])
        if not data:
            return None
        return [{'path': str(path), 'size_bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}]
    except SandboxRuntimeError:
        return None


async def receive_workflow_delivery(sandbox, declared: list[dict], *, cwd: str) -> dict:
    """Use ordinary safe collection, binding delivery to the signalled bytes."""
    from .artifact_collection import artifact_source_paths, receive_declared_artifacts
    from .sandbox_runtime import SandboxRuntimeError

    try:
        sources = artifact_source_paths([item['path'] for item in declared], cwd=cwd)
        if len(sources) != len(declared):
            raise SandboxRuntimeError('SANDBOX_ARTIFACT_DENIED', 'Duplicate resolved artifact paths')
        collection = await receive_declared_artifacts(sandbox, sources, cwd=cwd)
        if collection.get('ok') is False:
            return collection
        snapshot = {}
        for source, item in zip(sources, declared):
            target = collection['artifact_sources'][source]
            data = sandbox.download_file(target)
            digest = hashlib.sha256(data).hexdigest()
            if len(data) != item['size_bytes'] or digest != item['sha256']:
                return {'ok': False, 'error_code': 'WORKFLOW_DELIVERY_CHANGED',
                        'message': 'Final artifacts differ from this execution exporter signal.'}
            snapshot[target] = digest
        return {**collection, 'ok': True, 'artifact_sha256': snapshot, 'sources': sources}
    except SandboxRuntimeError as exc:
        return {'ok': False, 'error_code': exc.code, 'message': str(exc)[:1000]}
