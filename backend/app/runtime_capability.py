"""Catalog-only environment upgrades. Builders receive no user files or reasons."""
import asyncio
import json
import time
from docker.errors import DockerException
from requests.exceptions import RequestException

from .config import settings
from .environment_capabilities import CAPABILITIES, preflight_environment
from .environment_preparation import environment_spec, enqueue_environment
from .models import PreparedEnvironment
from .sandbox_checkpoint import replace_sandbox
from .sandbox_runtime import SandboxRuntimeError


async def request_capability(db, job, sandbox, capability, cancelled):
    def failure(code, message):
        return {'ok': False, 'error_code': code, 'message': message}
    memory = job.memory.data or {}
    current = memory.get('environment') or {}
    if capability in current.get('capabilities', []):
        return {'ok': True, 'already_available': True, 'capability': capability}
    if not settings.environment_preparation_enabled:
        return failure('ENVIRONMENT_UPGRADE_DISABLED', 'Platform environment preparation is disabled')
    previous = db.get(PreparedEnvironment, memory.get('prepared_environment_digest')) if memory.get('prepared_environment_digest') else None
    try:
        caps = set(previous.spec.get('capabilities', [])) if previous else set()
        caps.add(capability)
        spec = environment_spec(caps, previous.spec['base_image'] if previous else None)
        if previous:
            if previous.status != 'ready':
                raise ValueError('Current environment is no longer ready')
            expected = environment_spec(previous.spec['capabilities'], previous.spec['base_image'])
            # Catalog/probe additions are compatible if the pinned dependencies,
            # base, platform and policy are unchanged. Candidate probes still
            # verify every capability before adopting the new environment.
            compatible_keys = set(expected) - {'probe_digest', 'extension_digest'}
            if any(expected[key] != previous.spec.get(key) for key in compatible_keys):
                raise ValueError('Pinned environment policy changed; upload a new Skill version')
        elif current.get('image_id') != spec['base_image']:
            raise ValueError('Current image has no compatible pinned environment binding')
    except ValueError as exc:
        return {**failure('ENVIRONMENT_CAPABILITY_UNSUPPORTED', str(exc)),
                'supported_capabilities': sorted(CAPABILITIES)}
    attempts = int(memory.get('environment_upgrade_attempts', 0))
    if attempts >= 2:
        return failure('ENVIRONMENT_UPGRADE_LIMIT', 'At most two upgrade attempts are allowed per task')
    env = enqueue_environment(db, spec)
    job.memory.data = {**memory, 'environment_upgrade_attempts': attempts + 1}
    db.commit()
    deadline = time.monotonic() + settings.environment_queue_wait_seconds
    while True:
        if cancelled():
            return failure('WORKFLOW_CANCELLED', 'Task cancelled while preparing environment')
        db.refresh(env)
        status, image = env.status, env.image_id
        db.commit()  # Never hold a DB transaction while waiting for a builder.
        if status == 'ready':
            break
        if status in {'failed', 'revoked'}:
            return failure('ENVIRONMENT_BUILD_FAILED', env.error_message or status)
        if time.monotonic() >= deadline:
            return failure('ENVIRONMENT_BUILD_WAIT_TIMEOUT', 'Environment preparation wait expired; original sandbox retained')
        await asyncio.sleep(1)
    inventory = None
    async def validate(candidate):
        nonlocal inventory
        inventory = await preflight_environment(candidate, [])
        needed = set(current.get('capabilities', [])) | {capability}
        if not needed.issubset(inventory['capabilities']) or inventory['image_id'] != image:
            raise ValueError('Candidate environment failed capability or image verification')
        db.refresh(env)
        if env.status != 'ready' or env.image_id != image:
            raise ValueError('Candidate environment revoked or changed before adoption')
        db.commit()
        inventory['environment_upgrade_supported'] = True
        await candidate.write_text('/workspace/work/environment.json', json.dumps(inventory, ensure_ascii=False))
    try:
        if not image or not image.startswith('sha256:'):
            raise ValueError('Prepared image is not immutable')
        evidence = await replace_sandbox(sandbox, cancelled, image_id=image, validate_candidate=validate)
    except (SandboxRuntimeError, ValueError, RuntimeError, DockerException, RequestException) as exc:
        return failure('ENVIRONMENT_UPGRADE_FAILED', str(exc))
    job.memory.data = {**job.memory.data, 'environment': inventory, 'prepared_environment_digest': env.digest}
    db.commit()
    return {'ok': True, 'upgraded': True, 'capability': capability, 'environment_digest': env.digest,
            'capabilities': inventory['capabilities'], 'snapshot': evidence,
            'message': 'Workspace restored in the upgraded sandbox. Continue; do not replay completed work. Processes and /tmp were not restored.'}
