from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _csv(name: str, default: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in os.getenv(name, default).split(",") if item.strip())


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    environment: str = os.getenv("SKILLGO_ENVIRONMENT", "development")
    database_url: str = os.getenv(
        "SKILLGO_DATABASE_URL", "sqlite:///./data/skillgo.db"
    )
    storage_root: Path = Path(os.getenv("SKILLGO_STORAGE_ROOT", "./storage"))
    jwt_secret: str = os.getenv(
        "SKILLGO_JWT_SECRET", "development-only-change-this-secret"
    )
    access_token_minutes: int = int(os.getenv("SKILLGO_ACCESS_TOKEN_MINUTES", "480"))
    cors_origins: tuple[str, ...] = _csv(
        "SKILLGO_CORS_ORIGINS", "http://localhost:5173"
    )
    bootstrap_email: str | None = os.getenv("SKILLGO_BOOTSTRAP_EMAIL")
    bootstrap_password: str | None = os.getenv("SKILLGO_BOOTSTRAP_PASSWORD")
    bootstrap_name: str = os.getenv("SKILLGO_BOOTSTRAP_NAME", "SkillGo Owner")
    max_upload_bytes: int = int(os.getenv("SKILLGO_MAX_UPLOAD_BYTES", str(20 * 1024 * 1024)))
    max_uncompressed_bytes: int = int(
        os.getenv("SKILLGO_MAX_UNCOMPRESSED_BYTES", str(100 * 1024 * 1024))
    )
    max_archive_files: int = int(os.getenv("SKILLGO_MAX_ARCHIVE_FILES", "500"))
    max_run_input_bytes: int = int(os.getenv("SKILLGO_MAX_RUN_INPUT_BYTES", str(512 * 1024)))
    context_max_messages: int = int(os.getenv("SKILLGO_CONTEXT_MAX_MESSAGES", "20"))
    context_max_chars: int = int(os.getenv("SKILLGO_CONTEXT_MAX_CHARS", "30000"))
    conversation_lock_seconds: int = int(os.getenv("SKILLGO_CONVERSATION_LOCK_SECONDS", "180"))
    workspace_max_file_bytes: int = int(
        os.getenv("SKILLGO_WORKSPACE_MAX_FILE_BYTES", str(10 * 1024 * 1024))
    )
    workspace_max_files: int = int(os.getenv("SKILLGO_WORKSPACE_MAX_FILES", "30"))
    workspace_extract_max_chars: int = int(
        os.getenv("SKILLGO_WORKSPACE_EXTRACT_MAX_CHARS", "200000")
    )
    workspace_context_max_chars: int = int(
        os.getenv("SKILLGO_WORKSPACE_CONTEXT_MAX_CHARS", "40000")
    )
    model_base_url: str | None = os.getenv("SKILLGO_MODEL_BASE_URL")
    model_api_key: str | None = os.getenv("SKILLGO_MODEL_API_KEY")
    model_name: str | None = os.getenv("SKILLGO_MODEL_NAME")
    model_timeout_seconds: float = float(os.getenv("SKILLGO_MODEL_TIMEOUT_SECONDS", "120"))
    model_temperature: float = float(os.getenv("SKILLGO_MODEL_TEMPERATURE", "0.2"))
    model_json_mode: bool = _bool("SKILLGO_MODEL_JSON_MODE", True)
    model_native_tools: bool = _bool("SKILLGO_MODEL_NATIVE_TOOLS", True)
    model_tls_verify: bool = _bool("SKILLGO_MODEL_TLS_VERIFY", True)
    sandbox_worker_enabled: bool = _bool("SKILLGO_SANDBOX_WORKER_ENABLED", False)
    sandbox_runtime: str = os.getenv("SKILLGO_SANDBOX_RUNTIME", "runsc")
    sandbox_image: str = os.getenv(
        "SKILLGO_SANDBOX_IMAGE", "skillgo/sandbox-runtime:local"
    )
    sandbox_poll_seconds: float = float(os.getenv("SKILLGO_SANDBOX_POLL_SECONDS", "1"))
    sandbox_worker_lease_seconds: int = int(
        os.getenv("SKILLGO_SANDBOX_WORKER_LEASE_SECONDS", "90")
    )
    sandbox_worker_heartbeat_seconds: int = int(
        os.getenv("SKILLGO_SANDBOX_WORKER_HEARTBEAT_SECONDS", "15")
    )
    sandbox_worker_max_attempts: int = int(
        os.getenv("SKILLGO_SANDBOX_WORKER_MAX_ATTEMPTS", "3")
    )
    sandbox_job_timeout_seconds: int = int(
        os.getenv("SKILLGO_SANDBOX_JOB_TIMEOUT_SECONDS", "1800")
    )
    sandbox_command_timeout_seconds: int = int(
        os.getenv("SKILLGO_SANDBOX_COMMAND_TIMEOUT_SECONDS", "120")
    )
    # A reasoning turn may now contain several native tool calls. Keep the
    # model-turn budget separate from the sandbox-operation budget so a useful
    # batch is not reported as several rounds of "thinking".
    sandbox_max_agent_turns: int = int(
        os.getenv(
            "SKILLGO_SANDBOX_MAX_AGENT_TURNS",
            os.getenv("SKILLGO_SANDBOX_MAX_AGENT_STEPS", "40"),
        )
    )
    sandbox_max_agent_tool_calls: int = int(
        os.getenv("SKILLGO_SANDBOX_MAX_AGENT_TOOL_CALLS", "160")
    )
    sandbox_memory: str = os.getenv("SKILLGO_SANDBOX_MEMORY", "768m")
    sandbox_nano_cpus: int = int(os.getenv("SKILLGO_SANDBOX_NANO_CPUS", "1000000000"))
    sandbox_pids_limit: int = int(os.getenv("SKILLGO_SANDBOX_PIDS_LIMIT", "128"))
    sandbox_max_artifact_bytes: int = int(
        os.getenv("SKILLGO_SANDBOX_MAX_ARTIFACT_BYTES", str(50 * 1024 * 1024))
    )
    platform_document_tools_enabled: bool = _bool(
        "SKILLGO_PLATFORM_DOCUMENT_TOOLS_ENABLED", False
    )
    agent_run_success_detail_days: int = int(
        os.getenv("SKILLGO_AGENT_RUN_SUCCESS_DETAIL_DAYS", "7")
    )
    agent_run_failure_detail_days: int = int(
        os.getenv("SKILLGO_AGENT_RUN_FAILURE_DETAIL_DAYS", "30")
    )
    agent_run_cleanup_interval_seconds: int = int(
        os.getenv("SKILLGO_AGENT_RUN_CLEANUP_INTERVAL_SECONDS", "3600")
    )
    storage_retention_days: int = int(
        os.getenv("SKILLGO_STORAGE_RETENTION_DAYS", "15")
    )
    storage_cleanup_interval_seconds: int = int(
        os.getenv("SKILLGO_STORAGE_CLEANUP_INTERVAL_SECONDS", "3600")
    )
    storage_orphan_grace_hours: int = int(
        os.getenv("SKILLGO_STORAGE_ORPHAN_GRACE_HOURS", "24")
    )


settings = Settings()

if settings.sandbox_worker_heartbeat_seconds >= settings.sandbox_worker_lease_seconds:
    raise RuntimeError(
        "SKILLGO_SANDBOX_WORKER_HEARTBEAT_SECONDS must be lower than "
        "SKILLGO_SANDBOX_WORKER_LEASE_SECONDS"
    )

if settings.storage_retention_days < 1:
    raise RuntimeError("SKILLGO_STORAGE_RETENTION_DAYS must be at least 1")

if settings.environment == "production" and settings.jwt_secret.startswith("development-"):
    raise RuntimeError("SKILLGO_JWT_SECRET must be changed in production")
