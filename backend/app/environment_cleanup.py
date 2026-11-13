"""Reclaim explicitly released environments without touching shared or active images."""
from __future__ import annotations

import logging
from sqlalchemy import select
from .config import settings
from .models import PreparedEnvironment, SkillEnvironmentBinding, WorkflowJob, WorkflowJobMemory

logger = logging.getLogger(__name__)
ACTIVE_STATUSES = ['CREATED', 'QUEUED', 'RUNNING', 'WAITING_USER', 'PREPARING',
                   'PRODUCING_ARTIFACTS', 'VERIFYING']


def request_environment_cleanup(db, digests):
    for digest in sorted(set(digests)):
        env = db.scalar(select(PreparedEnvironment).where(
            PreparedEnvironment.digest == digest).with_for_update())
        if env is not None:
            env.inventory = {**(env.inventory or {}), 'cleanup_requested': True}


def cleanup_released_environments(db, client):
    candidates = list(db.scalars(select(PreparedEnvironment.digest).where(
        PreparedEnvironment.inventory['cleanup_requested'].as_boolean() == True)))
    for digest in candidates:
        env = db.scalar(select(PreparedEnvironment).where(
            PreparedEnvironment.digest == digest).with_for_update(skip_locked=True))
        if env is None:
            continue
        if env.status in {'building', 'probing'}:
            db.rollback()
            continue
        bound = db.scalar(select(SkillEnvironmentBinding.version_id).where(
            SkillEnvironmentBinding.environment_digest == digest).limit(1))
        active = db.scalar(select(WorkflowJobMemory.job_id).join(
            WorkflowJob, WorkflowJob.id == WorkflowJobMemory.job_id).where(
                WorkflowJob.status.in_(ACTIVE_STATUSES),
                (WorkflowJobMemory.data['prepared_environment_digest'].as_string() == digest)
                | (WorkflowJobMemory.data['pending_environment_digest'].as_string() == digest)).limit(1))
        if bound:
            env.inventory = {k: v for k, v in env.inventory.items() if k != 'cleanup_requested'}
            db.commit()
            continue
        if active:
            db.rollback()
            continue
        try:
            image = env.image_id
            if image:
                # Containers protect even in-flight deletions whose job rows are gone.
                if client.containers.list(all=True, filters={'ancestor': image}):
                    db.rollback()
                    continue
                others = list(db.scalars(select(PreparedEnvironment).where(
                    PreparedEnvironment.digest != digest)))
                protected = {settings.environment_base_image, settings.environment_minimal_base_image,
                             env.spec.get('base_image')}
                protected.update(e.image_id for e in others)
                protected.update(e.spec.get('base_image') for e in others)
                if image not in protected:
                    from docker.errors import ImageNotFound
                    try:
                        obj = client.images.get(image)
                        if obj.labels.get('skillgo.prepared_environment') == 'true':
                            client.images.remove(image, force=False, noprune=True)
                    except ImageNotFound:
                        pass
            db.delete(env)
            db.commit()
        except Exception:
            db.rollback()
            logger.exception('Could not reclaim released environment digest=%s', digest)
