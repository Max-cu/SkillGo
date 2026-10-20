"""Deployment-wide cap on in-flight model requests.

Worker processes do not share memory. The cap is a row count in this
deployment's database, claimed inside one transaction so two containers cannot
both take the last slot. Another deployment has a different database and does
not see these rows. Deployments that share one model server must keep the sum
of their caps at or below 20 until a long-input load test says otherwise.
Every worker process in a deployment must be configured with the same cap.
"""

from __future__ import annotations

import os
import socket
import time
from uuid import uuid4

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .models import ModelRequestGate, ModelRequestLease


PARALLEL_SUBTASK_POOL = "parallel_subtask_model"


class ModelLeaseError(RuntimeError):
    pass


def process_holder() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"[:160]


def default_engine():
    from .database import engine

    return engine


def _lock_gate(conn, pool: str) -> None:
    insert_fn = pg_insert if conn.dialect.name == "postgresql" else sqlite_insert
    statement = insert_fn(ModelRequestGate).values(pool=pool)
    statement = statement.on_conflict_do_update(
        index_elements=[ModelRequestGate.pool],
        set_={"pool": statement.excluded.pool},
    )
    conn.execute(statement)
    if conn.dialect.name == "postgresql":
        conn.execute(
            select(ModelRequestGate.pool).where(ModelRequestGate.pool == pool).with_for_update()
        )


def try_acquire(
    engine,
    *,
    limit: int,
    ttl_seconds: int,
    job_id: str,
    holder: str,
    pool: str = PARALLEL_SUBTASK_POOL,
) -> str | None:
    """Claim one slot, or return None when the pool is already at `limit`.

    Expired rows are removed in the same transaction before the count, so a
    dead worker frees its slot on the next claim. `ttl_seconds` may be negative
    in tests; a non-positive ttl expires on the following claim.
    """

    if limit < 1:
        raise ModelLeaseError("model request limit must be at least 1")
    now = int(time.time())
    token = str(uuid4())
    with engine.begin() as conn:
        _lock_gate(conn, pool)
        conn.execute(
            delete(ModelRequestLease).where(
                ModelRequestLease.pool == pool,
                ModelRequestLease.expires_at <= now,
            )
        )
        count = conn.execute(
            select(func.count()).select_from(ModelRequestLease).where(ModelRequestLease.pool == pool)
        ).scalar() or 0
        if int(count) >= limit:
            return None
        conn.execute(
            insert(ModelRequestLease).values(
                token=token,
                pool=pool,
                job_id=str(job_id)[:64],
                holder=holder[:160],
                expires_at=now + int(ttl_seconds),
                created_at=now,
            )
        )
    return token


def refresh_lease(engine, token: str, *, ttl_seconds: int, pool: str = PARALLEL_SUBTASK_POOL) -> bool:
    expires = int(time.time()) + int(ttl_seconds)
    with engine.begin() as conn:
        result = conn.execute(
            update(ModelRequestLease)
            .where(ModelRequestLease.token == token, ModelRequestLease.pool == pool)
            .values(expires_at=expires)
        )
        return bool(result.rowcount)


def release_lease(engine, token: str, *, pool: str = PARALLEL_SUBTASK_POOL) -> None:
    with engine.begin() as conn:
        conn.execute(
            delete(ModelRequestLease).where(
                ModelRequestLease.token == token,
                ModelRequestLease.pool == pool,
            )
        )


def active_lease_count(engine, *, pool: str = PARALLEL_SUBTASK_POOL) -> int:
    now = int(time.time())
    with engine.connect() as conn:
        count = conn.execute(
            select(func.count()).select_from(ModelRequestLease).where(
                ModelRequestLease.pool == pool,
                ModelRequestLease.expires_at > now,
            )
        ).scalar()
    return int(count or 0)
