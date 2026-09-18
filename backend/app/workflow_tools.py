"""Trusted execution helpers for verification, step files and visual inspection."""
from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from uuid import uuid4
from typing import Any

from .artifact_validation import snapshot_sandbox_artifacts
from .sandbox_runtime import SandboxRuntimeError


def effective_instruction(job) -> str:
    answers = (getattr(getattr(job, 'memory', None), 'data', None) or {}).get('answers', [])
    return job.instruction + ('\n\nUser-confirmed clarifications:\n' + json.dumps(answers, ensure_ascii=False) if answers else '')


# Bound how many files a single directory ref may bind. Mirrors the 500-entry
# workspace listing cap; real batch steps (SVG sets, image assets) are far below.
MAX_DIRECTORY_MEMBERS = 500


def _validated_workspace_path(path: str) -> str:
    parsed = PurePosixPath(path)
    if not parsed.is_absolute() or not parsed.is_relative_to('/workspace') or '..' in parsed.parts:
        raise SandboxRuntimeError('PLAN_PATH_INVALID', 'Step files must be absolute paths below /workspace')
    return str(parsed)


async def snapshot_plan_refs(sandbox, paths: list[str]) -> tuple[dict[str, str], list[str]]:
    """Hash the concrete files (or non-empty directories) referenced by a plan.

    Returns ``(ref -> sha256, missing_refs)``. A regular file binds to its
    content hash; a directory binds to an aggregate hash of its regular files,
    so a step that legitimately produces a folder (SVG set, image assets) is
    tracked as one ref. Paths that do not exist are reported, not raised:
    mid-run steps may reference outputs produced by a later operation, and the
    hard existence gate is enforced only at finish against /workspace/output.
    """

    refs = [_validated_workspace_path(path) for path in dict.fromkeys(paths)]
    if not refs:
        return {}, []
    try:
        tree = await sandbox.list_files('/workspace')
    except SandboxRuntimeError as exc:
        if exc.code != 'SANDBOX_LIST_FAILED':
            raise
        tree = []
    files_present = {
        str(item.get('path'))
        for item in tree
        if item.get('type') == 'file' and isinstance(item.get('path'), str)
    }
    dirs_present = {
        str(item.get('path'))
        for item in tree
        if item.get('type') in {'directory', 'dir'} and isinstance(item.get('path'), str)
    }

    result: dict[str, str] = {}
    missing: list[str] = []
    for ref in refs:
        if ref in files_present:
            data = sandbox.read_workspace_file(ref)
            result[ref] = hashlib.sha256(data).hexdigest()
        elif ref in dirs_present:
            members = sorted(p for p in files_present if p.startswith(ref.rstrip('/') + '/'))
            members = members[:MAX_DIRECTORY_MEMBERS]
            if not members:
                missing.append(ref)
                continue
            aggregate = hashlib.sha256()
            for member in members:
                digest = hashlib.sha256(sandbox.read_workspace_file(member)).hexdigest()
                aggregate.update(f'{PurePosixPath(member).relative_to(ref)}:{digest}\n'.encode())
            result[ref] = aggregate.hexdigest()
        else:
            missing.append(ref)
    return result, missing


async def snapshot_step_files(sandbox, paths: list[str]) -> dict[str, str]:
    files, _missing = await snapshot_plan_refs(sandbox, paths)
    return files


async def run_verifier(sandbox, action: dict[str, Any], *, requirements: list[str]) -> dict[str, Any]:
    verification_id = uuid4().hex
    snapshot_error = None
    async def snapshot():
        nonlocal snapshot_error
        try:
            return await snapshot_sandbox_artifacts(sandbox)
        except SandboxRuntimeError as exc:
            if exc.code not in {'ARTIFACT_CONTENT_INVALID', 'SANDBOX_ARTIFACT_MISSING', 'SANDBOX_ARTIFACT_SIZE', 'SANDBOX_ARTIFACT_INVALID'}:
                raise
            snapshot_error = str(exc)
            return {}
    before = await snapshot()
    verifier_files = {}
    for argument in action['argv']:
        path = PurePosixPath(argument)
        if path.suffix.lower() in {'.py', '.js', '.sh'}:
            if not path.is_absolute():
                path = PurePosixPath(action.get('cwd') or '/workspace') / path
            verifier_files.update(await snapshot_step_files(sandbox, [str(path)]))
    result = await sandbox.command(action['argv'], cwd=action.get('cwd') or '/workspace',
                                   timeout_seconds=action.get('timeout_seconds', 120))
    after = await snapshot()
    checks = []
    error = None
    try:
        report = json.loads(result.stdout)
        checks = report['checks']
        if not isinstance(checks, list) or not 1 <= len(checks) <= 200:
            raise ValueError('checks must contain 1-200 items')
        ids = set()
        for item in checks:
            if not isinstance(item, dict) or not isinstance(item.get('requirement_id'), str) or type(item.get('passed')) is not bool or 'observed' not in item:
                raise ValueError('Each check requires requirement_id, boolean passed and observed')
            if len(json.dumps(item, ensure_ascii=False)) > 2000:
                raise ValueError('Each check must be compact (at most 2000 characters); put details in evidence files')
            ids.add(item['requirement_id'])
        required = {f'r{i}' for i in range(1, len(requirements) + 1)}
        if not required.issubset(ids):
            raise ValueError('Verifier did not cover every requirement_id: ' + ', '.join(sorted(required - ids)))
    except (ValueError, KeyError, TypeError) as exc:
        error = str(exc)
    error = snapshot_error or error
    ok = bool(before) and before == after and result.exit_code == 0 and error is None and all(item['passed'] for item in checks)
    payload = {'ok': ok, 'verification_id': verification_id, 'artifacts': after,
               'verifier_files': verifier_files,
               'checks': checks, 'exit_code': result.exit_code,
               'argv': action['argv'], 'stdout': result.stdout, 'stderr': result.stderr,
               'error_code': None if ok else 'VERIFIER_FAILED',
               'message': error or ('Verifier changed output bytes' if before != after else 'Verification passed' if ok else 'Verification failed')}
    report_path = f'/workspace/work/verification/{verification_id}.json'
    await sandbox.write_text(report_path, json.dumps(payload, ensure_ascii=False))
    return {**payload, 'full_result_path': report_path}
