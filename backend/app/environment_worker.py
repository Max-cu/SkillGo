"""Dedicated trusted controller. Build containers never inherit controller credentials."""
import logging
import os
import secrets
import time
from datetime import timedelta
from uuid import uuid4

# The controller has no authentication endpoint and must not receive the API's
# signing key. Satisfy shared production config validation with an ephemeral,
# process-local key before importing settings; it is never passed to builders.
if __name__ == '__main__':
    os.environ['SKILLGO_JWT_SECRET'] = secrets.token_urlsafe(48)

from sqlalchemy import select, update
from .config import settings
from .database import SessionLocal, initialize_schema
from .models import PreparedEnvironment, utcnow
from .environment_builder import build_environment

logger = logging.getLogger(__name__)


def claim_environment():
    with SessionLocal() as db:
        # A crashed attempt is terminal, never allowed to overwrite a later retry.
        db.execute(update(PreparedEnvironment).where(
            PreparedEnvironment.status.in_(['building','probing']), PreparedEnvironment.lease_until < utcnow()
        ).values(status='failed', error_message='Environment builder lease expired; retry preparation', attempt=None))
        row = db.scalar(select(PreparedEnvironment).where(PreparedEnvironment.status=='queued').order_by(PreparedEnvironment.created_at).with_for_update(skip_locked=True))
        if row is None:
            db.commit()
            return None
        token = str(uuid4())
        changed = db.execute(update(PreparedEnvironment).where(PreparedEnvironment.digest==row.digest, PreparedEnvironment.status=='queued').values(
            status='building', attempt=token, lease_until=utcnow()+timedelta(seconds=settings.environment_build_seconds+60)))
        db.commit()
        return (row.digest, row.spec, token) if changed.rowcount else None


def finish_attempt(digest, attempt, **values):
    with SessionLocal() as db:
        result = db.execute(update(PreparedEnvironment).where(PreparedEnvironment.digest==digest,
            PreparedEnvironment.attempt==attempt, PreparedEnvironment.status.in_(['building','probing']),
            PreparedEnvironment.lease_until > utcnow()).values(**values))
        db.commit()
        if not result.rowcount:
            raise RuntimeError('Environment build lease lost')


def process_one(client):
    claimed = claim_environment()
    if claimed is None:
        return False
    digest, spec, attempt = claimed
    try:
        image, inventory = build_environment(client, spec, attempt,
            on_probing=lambda: finish_attempt(digest, attempt, status='probing'))
        finish_attempt(digest, attempt, status='ready', image_id=image, inventory=inventory, error_message=None)
    except Exception as exc:
        logger.exception('Environment preparation failed digest=%s', digest)
        try: finish_attempt(digest, attempt, status='failed', error_message=str(exc)[:4000])
        except RuntimeError: logger.warning('Discarded stale build completion digest=%s', digest)
    return True


def cleanup_expired_builds(client):
    with SessionLocal() as db:
        active = set(db.scalars(select(PreparedEnvironment.attempt).where(
            PreparedEnvironment.status.in_(['building', 'probing']), PreparedEnvironment.lease_until > utcnow())))
    for container in client.containers.list(all=True, filters={'label': 'skillgo.environment_build=true'}):
        attempt = (container.labels or {}).get('skillgo.build_attempt')
        if attempt and attempt not in active:
            try:
                container.remove(force=True)
            except Exception:
                logger.exception('Could not reclaim expired builder attempt=%s', attempt)


def main():
    import docker
    if not settings.environment_preparation_enabled:
        raise RuntimeError('Enable environment preparation before starting the controller')
    initialize_schema()
    client = docker.from_env(timeout=30)
    client.ping()
    last_cleanup = 0.0
    while True:
        if time.monotonic() - last_cleanup > 60:
            cleanup_expired_builds(client)
            last_cleanup = time.monotonic()
        if not process_one(client):
            time.sleep(2)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    main()
