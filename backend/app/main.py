from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select, text

from .config import settings
from .database import SessionLocal, initialize_schema
from .execution_runtime import cleanup_execution_history, fail_stale_conversation_runs
from .models import Role, User
from .routers import admin, agent, auth, conversations, jobs, runtime, skills, workspace
from .security import hash_password
from .services import add_audit
from .storage_lifecycle import cleanup_expired_storage


logger = logging.getLogger(__name__)


def bootstrap() -> None:
    initialize_schema()
    if not settings.bootstrap_email or not settings.bootstrap_password:
        return
    with SessionLocal() as db:
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(73455002)"))
        email = settings.bootstrap_email.lower()
        super_admins = list(
            db.scalars(
                select(User)
                .where(User.role == Role.SUPER_ADMIN)
                .order_by(User.created_at.asc())
            ).all()
        )
        if super_admins:
            keeper = next(
                (user for user in super_admins if user.email.lower() == email),
                super_admins[0],
            )
            duplicates = [user for user in super_admins if user.id != keeper.id]
            for duplicate in duplicates:
                duplicate.role = Role.ADMIN
                add_audit(
                    db,
                    actor=keeper,
                    action="system.super_admin_uniqueness_repair",
                    resource_type="user",
                    resource_id=duplicate.id,
                    details={"kept_super_admin_id": keeper.id},
                )
            if duplicates:
                db.commit()
            return
        existing = db.scalar(select(User).where(func.lower(User.email) == email))
        if existing:
            existing.role = Role.SUPER_ADMIN
            existing.is_active = True
            add_audit(
                db,
                actor=existing,
                action="system.bootstrap_super_admin.promote",
                resource_type="user",
                resource_id=existing.id,
            )
            db.commit()
            return
        user = User(
            email=email,
            display_name=settings.bootstrap_name,
            password_hash=hash_password(settings.bootstrap_password),
            role=Role.SUPER_ADMIN,
        )
        db.add(user)
        db.flush()
        add_audit(
            db,
            actor=user,
            action="system.bootstrap_super_admin",
            resource_type="user",
            resource_id=user.id,
        )
        db.commit()


async def _execution_cleanup_loop(stopping: asyncio.Event) -> None:
    last_storage_cleanup = 0.0
    while not stopping.is_set():
        try:
            await asyncio.to_thread(fail_stale_conversation_runs)
            await asyncio.to_thread(cleanup_execution_history)
            loop_time = asyncio.get_running_loop().time()
            if loop_time - last_storage_cleanup >= max(60, settings.storage_cleanup_interval_seconds):
                await asyncio.to_thread(cleanup_expired_storage)
                last_storage_cleanup = loop_time
        except Exception:
            logger.exception("Lifecycle cleanup failed; it will retry on the next interval")
        try:
            await asyncio.wait_for(
                stopping.wait(),
                timeout=max(60, settings.agent_run_cleanup_interval_seconds),
            )
        except TimeoutError:
            continue


@asynccontextmanager
async def lifespan(_: FastAPI):
    bootstrap()
    stopping = asyncio.Event()
    cleanup_task = asyncio.create_task(_execution_cleanup_loop(stopping))
    try:
        yield
    finally:
        stopping.set()
        await cleanup_task


app = FastAPI(
    title="SkillGo API",
    version="0.2.0",
    description="Skill workflow registry, community and execution control plane.",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth.router, prefix="/api/v1")
app.include_router(agent.router, prefix="/api/v1")
app.include_router(skills.router, prefix="/api/v1")
app.include_router(conversations.router, prefix="/api/v1")
app.include_router(workspace.router, prefix="/api/v1")
app.include_router(jobs.router, prefix="/api/v1")
app.include_router(admin.router, prefix="/api/v1")
app.include_router(runtime.router, prefix="/api/v1")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "healthy"}


@app.get("/api/v1")
def api_root() -> dict[str, str]:
    return {"name": "SkillGo API", "version": "0.2.0"}
