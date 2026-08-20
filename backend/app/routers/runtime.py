from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..config import settings
from ..conversation_service import (
    can_run_version,
    claim_conversation,
    conversation_history,
    record_exchange,
    release_conversation,
)
from ..database import get_db
from ..deps import current_user, super_admin_user
from ..execution import RunValidationError, execute_instruction_run, validate_input
from ..model_config_service import DEFAULT_CONFIG_ID, ensure_model_connection_rows, model_connection_from_db, normalize_models
from ..model_gateway import ModelConnection, ModelGatewayError, OpenAICompatibleGateway, get_model_gateway
from ..models import (
    Conversation,
    Endpoint,
    InvocationType,
    ModelProviderConfig,
    ModelConnectionConfig,
    Role,
    Run,
    RunStatus,
    SkillType,
    SkillVersion,
    User,
    VersionStatus,
)
from ..runtime_profile import version_runtime_profile
from ..schemas import (
    EndpointCreate,
    EndpointCreated,
    EndpointPatch,
    EndpointRead,
    InvokeRequest,
    InvokeResponse,
    AvailableModels,
    ModelConfigRead,
    ModelConfigUpdate,
    ModelConnectionTestRequest,
    ModelConnectionTestResult,
    ModelConnectionCreate,
    ModelConnectionItem,
    ModelConnectionList,
    ModelConnectionUpdate,
    ModelStatus,
    RunCreate,
    RunRead,
)
from ..security import generate_endpoint_key, verify_endpoint_key
from ..services import add_audit, endpoint_read, run_read
from ..workspace_service import workspace_context


router = APIRouter(tags=["runtime"])


def _can_manage_version(version: SkillVersion, user: User) -> bool:
    return version.skill.owner_id == user.id or user.role in (Role.ADMIN, Role.SUPER_ADMIN)


def _validation_http_error(exc: RunValidationError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={"code": exc.code, "message": str(exc)},
    )


@router.post("/runs", response_model=RunRead, status_code=status.HTTP_201_CREATED)
async def create_run(
    payload: RunCreate,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
    gateway: OpenAICompatibleGateway = Depends(get_model_gateway),
) -> RunRead:
    version = db.get(SkillVersion, payload.version_id)
    if version is None or not can_run_version(version, user):
        raise HTTPException(status_code=404, detail="Skill version not found")
    runtime_profile = version_runtime_profile(version)
    if runtime_profile["execution_mode"] != "instruction_only":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "WORKFLOW_RUNNER_REQUIRED",
                "message": "这个 Skill 不是纯对话指令，请使用“运行工作流”入口",
                "execution_mode": runtime_profile["execution_mode"],
                "runtime_status": runtime_profile["runtime_status"],
            },
        )
    chat_mode = payload.message is not None
    input_data = {"message": payload.message} if chat_mode else (payload.input or {})
    try:
        validate_input(version, input_data, validate_schema=not chat_mode)
    except RunValidationError as exc:
        raise _validation_http_error(exc) from exc

    try:
        gateway = gateway.for_model(payload.model_name)
    except ModelGatewayError as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc)}) from exc

    conversation: Conversation | None = None
    history: list[dict] = []
    workspace_files: list[dict[str, str]] = []
    claimed = False
    if payload.conversation_id:
        conversation = db.get(Conversation, payload.conversation_id)
        if conversation is None or conversation.user_id != user.id:
            raise HTTPException(status_code=404, detail="Conversation not found")
        if conversation.skill_version_id != version.id:
            raise HTTPException(
                status_code=409,
                detail="Conversation is pinned to a different Skill version",
            )
        if not claim_conversation(db, conversation, user):
            raise HTTPException(status_code=409, detail="Conversation is already running")
        claimed = True
        history = conversation_history(db, conversation.id)
        workspace_files = workspace_context(db, conversation.id)

    try:
        run = Run(
            user_id=user.id,
            skill_id=version.skill_id,
            skill_version_id=version.id,
            status=RunStatus.QUEUED,
            invocation_type=InvocationType.CONSOLE,
            input_data=input_data,
            model_name=gateway.model_name,
        )
        db.add(run)
        db.flush()
        add_audit(
            db,
            actor=user,
            action="run.create",
            resource_type="run",
            resource_id=run.id,
            details={
                "version_id": version.id,
                "invocation_type": "console",
                "conversation_id": conversation.id if conversation else None,
                "context_messages": len(history),
                "input_mode": "chat" if chat_mode else "structured",
                "workspace_files": len(workspace_files),
                "model_name": gateway.model_name,
            },
        )
        db.commit()
        db.refresh(run)
        await execute_instruction_run(
            db,
            run=run,
            version=version,
            actor=user,
            gateway=gateway,
            history=history,
            chat_mode=chat_mode,
            workspace_files=workspace_files,
        )
        if conversation and run.status == RunStatus.SUCCEEDED and run.output_data is not None:
            record_exchange(
                db,
                conversation_id=conversation.id,
                run_id=run.id,
                input_data=run.input_data,
                output_data=run.output_data,
            )
        if conversation:
            release_conversation(db, conversation.id)
        db.commit()
        return run_read(run, context_message_count=len(history))
    except Exception:
        db.rollback()
        if claimed and conversation:
            release_conversation(db, conversation.id)
            db.commit()
        raise


@router.get("/runs", response_model=list[RunRead])
def list_runs(
    limit: int = 50,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[RunRead]:
    limit = max(1, min(limit, 200))
    owned_endpoints = select(Endpoint.id).where(Endpoint.owner_id == user.id)
    statement = select(Run).where(
        or_(Run.user_id == user.id, Run.endpoint_id.in_(owned_endpoints))
    )
    if user.role in (Role.ADMIN, Role.SUPER_ADMIN):
        statement = select(Run)
    runs = db.scalars(statement.order_by(Run.created_at.desc()).limit(limit)).all()
    return [run_read(item) for item in runs]


@router.get("/runs/{run_id}", response_model=RunRead)
def get_run(
    run_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> RunRead:
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    endpoint_owner = run.endpoint.owner_id if run.endpoint else None
    allowed = (
        run.user_id == user.id
        or endpoint_owner == user.id
        or user.role in (Role.ADMIN, Role.SUPER_ADMIN)
    )
    if not allowed:
        raise HTTPException(status_code=404, detail="Run not found")
    return run_read(run)


@router.post("/endpoints", response_model=EndpointCreated, status_code=status.HTTP_201_CREATED)
def create_endpoint(
    payload: EndpointCreate,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> EndpointCreated:
    version = db.get(SkillVersion, payload.version_id)
    if version is None or not _can_manage_version(version, user):
        raise HTTPException(status_code=404, detail="Skill version not found")
    if version.status != VersionStatus.PUBLISHED:
        raise HTTPException(status_code=409, detail="Only published versions can be deployed")
    runtime_profile = version_runtime_profile(version)
    execution_mode = str(runtime_profile["execution_mode"])
    if execution_mode not in {"instruction_only", "sandbox_required"}:
        raise HTTPException(
            status_code=422,
            detail="Only instruction Skills and runnable sandbox workflows can be deployed",
        )
    if not runtime_profile.get("runnable"):
        raise HTTPException(
            status_code=409,
            detail={
                "code": str(runtime_profile.get("runtime_status", "RUNTIME_UNAVAILABLE")).upper(),
                "message": str(runtime_profile.get("block_reason") or "Runtime is unavailable"),
            },
        )
    if db.scalar(select(Endpoint).where(Endpoint.slug == payload.slug)):
        raise HTTPException(status_code=409, detail="Endpoint slug is already in use")

    api_key, prefix, key_hash = generate_endpoint_key()
    endpoint = Endpoint(
        owner_id=user.id,
        skill_id=version.skill_id,
        skill_version_id=version.id,
        slug=payload.slug,
        name=payload.name,
        api_key_prefix=prefix,
        api_key_hash=key_hash,
    )
    db.add(endpoint)
    db.flush()
    add_audit(
        db,
        actor=user,
        action="endpoint.create",
        resource_type="endpoint",
        resource_id=endpoint.id,
        details={"slug": endpoint.slug, "version_id": version.id},
    )
    db.commit()
    db.refresh(endpoint)
    return EndpointCreated(**endpoint_read(endpoint).model_dump(), api_key=api_key)


@router.get("/endpoints", response_model=list[EndpointRead])
def list_endpoints(
    user: User = Depends(current_user), db: Session = Depends(get_db)
) -> list[EndpointRead]:
    statement = select(Endpoint).where(Endpoint.owner_id == user.id)
    if user.role in (Role.ADMIN, Role.SUPER_ADMIN):
        statement = select(Endpoint)
    endpoints = db.scalars(statement.order_by(Endpoint.created_at.desc())).all()
    return [endpoint_read(item) for item in endpoints]


@router.patch("/endpoints/{endpoint_id}", response_model=EndpointRead)
def update_endpoint(
    endpoint_id: str,
    payload: EndpointPatch,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> EndpointRead:
    endpoint = db.get(Endpoint, endpoint_id)
    if endpoint is None or (
        endpoint.owner_id != user.id and user.role not in (Role.ADMIN, Role.SUPER_ADMIN)
    ):
        raise HTTPException(status_code=404, detail="Endpoint not found")
    endpoint.is_active = payload.is_active
    add_audit(
        db,
        actor=user,
        action="endpoint.status",
        resource_type="endpoint",
        resource_id=endpoint.id,
        details={"is_active": endpoint.is_active},
    )
    db.commit()
    db.refresh(endpoint)
    return endpoint_read(endpoint)


@router.post("/endpoints/{endpoint_id}/rotate-key", response_model=EndpointCreated)
def rotate_endpoint_key(
    endpoint_id: str,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> EndpointCreated:
    endpoint = db.get(Endpoint, endpoint_id)
    if endpoint is None or (
        endpoint.owner_id != user.id and user.role not in (Role.ADMIN, Role.SUPER_ADMIN)
    ):
        raise HTTPException(status_code=404, detail="Endpoint not found")
    api_key, prefix, key_hash = generate_endpoint_key()
    endpoint.api_key_prefix = prefix
    endpoint.api_key_hash = key_hash
    add_audit(
        db,
        actor=user,
        action="endpoint.key.rotate",
        resource_type="endpoint",
        resource_id=endpoint.id,
    )
    db.commit()
    db.refresh(endpoint)
    return EndpointCreated(**endpoint_read(endpoint).model_dump(), api_key=api_key)


@router.post("/invoke/{endpoint_slug}", response_model=InvokeResponse)
async def invoke_endpoint(
    endpoint_slug: str,
    payload: InvokeRequest,
    endpoint_key: str | None = Header(default=None, alias="X-SkillGo-Key"),
    db: Session = Depends(get_db),
    gateway: OpenAICompatibleGateway = Depends(get_model_gateway),
) -> InvokeResponse:
    endpoint = db.scalar(
        select(Endpoint).where(Endpoint.slug == endpoint_slug, Endpoint.is_active.is_(True))
    )
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    if not endpoint_key or not verify_endpoint_key(endpoint_key, endpoint.api_key_hash):
        raise HTTPException(status_code=401, detail="Invalid endpoint key")
    version = endpoint.skill_version
    runtime_profile = version_runtime_profile(version)
    if runtime_profile["execution_mode"] != "instruction_only":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ASYNC_WORKFLOW_REQUIRED",
                "message": "This Skill must be invoked through the workflow jobs API",
            },
        )
    try:
        validate_input(version, payload.input)
    except RunValidationError as exc:
        raise _validation_http_error(exc) from exc

    run = Run(
        user_id=None,
        endpoint_id=endpoint.id,
        skill_id=endpoint.skill_id,
        skill_version_id=version.id,
        status=RunStatus.QUEUED,
        invocation_type=InvocationType.API,
        input_data=payload.input,
    )
    db.add(run)
    db.flush()
    add_audit(
        db,
        actor=None,
        action="run.create",
        resource_type="run",
        resource_id=run.id,
        details={"endpoint_id": endpoint.id, "invocation_type": "api"},
    )
    db.commit()
    db.refresh(run)
    await execute_instruction_run(db, run=run, version=version, actor=None, gateway=gateway)
    if run.status != RunStatus.SUCCEEDED or run.output_data is None:
        raise HTTPException(
            status_code=502,
            detail={
                "run_id": run.id,
                "code": run.error_code,
                "message": run.error_message,
            },
        )
    return InvokeResponse(
        run_id=run.id,
        status=run.status,
        output=run.output_data,
        model_name=run.model_name,
        latency_ms=run.latency_ms,
    )


def _model_config_read(db: Session) -> ModelConfigRead:
    connection, source, api_key_configured = model_connection_from_db(db)
    return ModelConfigRead(
        configured=bool(connection.base_url and connection.model_name),
        base_url=connection.base_url,
        models=list(connection.models),
        default_model=connection.model_name,
        api_key_configured=api_key_configured,
        timeout_seconds=round(connection.timeout_seconds),
        temperature=connection.temperature,
        json_mode=connection.json_mode,
        native_tools=connection.native_tools,
        tls_verify=connection.tls_verify,
        source=source,
    )


def _model_connection_item(row: ModelConnectionConfig) -> ModelConnectionItem:
    return ModelConnectionItem(
        id=row.id,
        model_name=row.model_name,
        base_url=row.base_url,
        api_key_configured=bool(row.api_key or settings.model_api_key),
        timeout_seconds=row.timeout_seconds,
        temperature=max(0, min(row.temperature_milli, 2000)) / 1000,
        json_mode=row.json_mode,
        native_tools=row.native_tools,
        tls_verify=row.tls_verify,
        is_default=row.is_default,
        enabled=row.enabled,
    )


def _saved_model_rows(db: Session) -> list[ModelConnectionConfig]:
    return list(
        db.scalars(
            select(ModelConnectionConfig).order_by(
                ModelConnectionConfig.is_default.desc(),
                ModelConnectionConfig.created_at,
            )
        )
    )


def _ensure_default_model(db: Session, preferred: ModelConnectionConfig | None = None) -> None:
    enabled = [row for row in _saved_model_rows(db) if row.enabled]
    if not enabled:
        return
    selected = preferred if preferred is not None and preferred.enabled else next((row for row in enabled if row.is_default), enabled[0])
    for row in enabled:
        row.is_default = row.id == selected.id


@router.get("/super-admin/models", response_model=ModelConnectionList)
def list_model_connections(
    _: User = Depends(super_admin_user),
    db: Session = Depends(get_db),
) -> ModelConnectionList:
    rows = ensure_model_connection_rows(db)
    if rows:
        db.commit()
    default = next((row.model_name for row in rows if row.is_default and row.enabled), None)
    return ModelConnectionList(configured=bool(default), default_model=default, items=[_model_connection_item(row) for row in rows])


def _validated_connection_values(payload: ModelConnectionCreate | ModelConnectionUpdate) -> tuple[str, str]:
    base_url = payload.base_url.strip().rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=422, detail="模型地址必须是有效的 HTTP 或 HTTPS URL")
    return payload.model_name.strip(), base_url


@router.post("/super-admin/models", response_model=ModelConnectionItem, status_code=status.HTTP_201_CREATED)
def create_model_connection(
    payload: ModelConnectionCreate,
    user: User = Depends(super_admin_user),
    db: Session = Depends(get_db),
) -> ModelConnectionItem:
    model_name, base_url = _validated_connection_values(payload)
    if db.scalar(select(ModelConnectionConfig).where(ModelConnectionConfig.model_name == model_name)):
        raise HTTPException(status_code=409, detail="同名模型已经存在")
    row = ModelConnectionConfig(
        model_name=model_name,
        base_url=base_url,
        api_key=(payload.api_key or "").strip() or None,
        timeout_seconds=payload.timeout_seconds,
        temperature_milli=round(payload.temperature * 1000),
        json_mode=payload.json_mode,
        native_tools=payload.native_tools,
        tls_verify=payload.tls_verify,
        is_default=payload.is_default,
        enabled=payload.enabled,
    )
    db.add(row)
    db.flush()
    _ensure_default_model(db, row if payload.is_default else None)
    add_audit(db, actor=user, action="system.model.create", resource_type="model_connection", resource_id=row.id, details={"model_name": row.model_name, "base_url": row.base_url})
    db.commit()
    db.refresh(row)
    return _model_connection_item(row)


@router.put("/super-admin/models/{model_id}", response_model=ModelConnectionItem)
def update_model_connection(
    model_id: str,
    payload: ModelConnectionUpdate,
    user: User = Depends(super_admin_user),
    db: Session = Depends(get_db),
) -> ModelConnectionItem:
    row = db.get(ModelConnectionConfig, model_id)
    if row is None:
        raise HTTPException(status_code=404, detail="模型不存在")
    model_name, base_url = _validated_connection_values(payload)
    duplicate = db.scalar(select(ModelConnectionConfig).where(ModelConnectionConfig.model_name == model_name, ModelConnectionConfig.id != model_id))
    if duplicate:
        raise HTTPException(status_code=409, detail="同名模型已经存在")
    row.model_name = model_name
    row.base_url = base_url
    row.timeout_seconds = payload.timeout_seconds
    row.temperature_milli = round(payload.temperature * 1000)
    row.json_mode = payload.json_mode
    row.native_tools = payload.native_tools
    row.tls_verify = payload.tls_verify
    row.enabled = payload.enabled
    row.is_default = payload.is_default and payload.enabled
    if payload.clear_api_key:
        row.api_key = None
    elif payload.api_key and payload.api_key.strip():
        row.api_key = payload.api_key.strip()
    _ensure_default_model(db, row if payload.is_default else None)
    add_audit(db, actor=user, action="system.model.update", resource_type="model_connection", resource_id=row.id, details={"model_name": row.model_name, "base_url": row.base_url})
    db.commit()
    db.refresh(row)
    return _model_connection_item(row)


@router.post("/super-admin/models/{model_id}/default", response_model=ModelConnectionItem)
def set_default_model_connection(
    model_id: str,
    user: User = Depends(super_admin_user),
    db: Session = Depends(get_db),
) -> ModelConnectionItem:
    row = db.get(ModelConnectionConfig, model_id)
    if row is None or not row.enabled:
        raise HTTPException(status_code=404, detail="可用模型不存在")
    _ensure_default_model(db, row)
    add_audit(db, actor=user, action="system.model.set_default", resource_type="model_connection", resource_id=row.id, details={"model_name": row.model_name})
    db.commit()
    db.refresh(row)
    return _model_connection_item(row)


@router.delete("/super-admin/models/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_model_connection(
    model_id: str,
    user: User = Depends(super_admin_user),
    db: Session = Depends(get_db),
) -> None:
    row = db.get(ModelConnectionConfig, model_id)
    if row is None:
        raise HTTPException(status_code=404, detail="模型不存在")
    was_default = row.is_default
    add_audit(db, actor=user, action="system.model.delete", resource_type="model_connection", resource_id=row.id, details={"model_name": row.model_name})
    db.delete(row)
    db.flush()
    if was_default:
        remaining = list(db.scalars(select(ModelConnectionConfig).where(ModelConnectionConfig.enabled.is_(True)).order_by(ModelConnectionConfig.created_at)))
        if remaining:
            remaining[0].is_default = True
    db.commit()


@router.get("/models/available", response_model=AvailableModels)
def available_models(
    _: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> AvailableModels:
    config = _model_config_read(db)
    return AvailableModels(
        configured=config.configured,
        models=config.models,
        default_model=config.default_model,
    )


@router.get("/super-admin/model/config", response_model=ModelConfigRead)
def read_model_config(
    _: User = Depends(super_admin_user),
    db: Session = Depends(get_db),
) -> ModelConfigRead:
    return _model_config_read(db)


def _validated_model_values(payload: ModelConfigUpdate) -> tuple[str, tuple[str, ...]]:
    base_url = payload.base_url.strip().rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=422, detail="模型地址必须是有效的 HTTP 或 HTTPS URL")
    models = normalize_models(payload.models)
    if not models:
        raise HTTPException(status_code=422, detail="至少配置一个模型")
    if payload.default_model.strip() not in models:
        raise HTTPException(status_code=422, detail="默认模型必须在可用模型列表中")
    return base_url, models


@router.put("/super-admin/model/config", response_model=ModelConfigRead)
def update_model_config(
    payload: ModelConfigUpdate,
    user: User = Depends(super_admin_user),
    db: Session = Depends(get_db),
) -> ModelConfigRead:
    base_url, models = _validated_model_values(payload)
    row = db.get(ModelProviderConfig, DEFAULT_CONFIG_ID)
    if row is None:
        row = ModelProviderConfig(
            id=DEFAULT_CONFIG_ID,
            base_url=base_url,
            models=list(models),
            default_model=payload.default_model.strip(),
        )
        db.add(row)
    row.base_url = base_url
    row.models = list(models)
    row.default_model = payload.default_model.strip()
    row.timeout_seconds = payload.timeout_seconds
    row.temperature_milli = round(payload.temperature * 1000)
    row.json_mode = payload.json_mode
    row.native_tools = payload.native_tools
    row.tls_verify = payload.tls_verify
    if payload.clear_api_key:
        row.api_key = None
    elif payload.api_key and payload.api_key.strip():
        row.api_key = payload.api_key.strip()
    add_audit(
        db,
        actor=user,
        action="system.model_config.update",
        resource_type="model_provider_config",
        resource_id=row.id,
        details={"base_url": base_url, "models": list(models), "default_model": row.default_model},
    )
    db.commit()
    return _model_config_read(db)


@router.post("/super-admin/model/test", response_model=ModelConnectionTestResult)
async def test_model_connection(
    payload: ModelConnectionTestRequest,
    _: User = Depends(super_admin_user),
    db: Session = Depends(get_db),
) -> ModelConnectionTestResult:
    base_url, models = _validated_model_values(payload)
    current, _, _ = model_connection_from_db(db)
    saved = db.get(ModelConnectionConfig, payload.model_id) if payload.model_id else None
    api_key = None if payload.clear_api_key else (payload.api_key or "").strip() or (saved.api_key if saved else None) or current.api_key
    gateway = OpenAICompatibleGateway(
        ModelConnection(
            base_url=base_url,
            api_key=api_key,
            model_name=payload.default_model.strip(),
            models=models,
            timeout_seconds=payload.timeout_seconds,
            temperature=payload.temperature,
            json_mode=payload.json_mode,
            native_tools=payload.native_tools,
            tls_verify=payload.tls_verify,
        )
    )
    try:
        result = await gateway.test_connection()
    except ModelGatewayError as exc:
        raise HTTPException(status_code=502, detail={"code": exc.code, "message": str(exc)}) from exc
    return ModelConnectionTestResult(
        ok=True,
        model_name=result["model_name"],
        latency_ms=result["latency_ms"],
        message="模型连接正常",
    )


@router.get("/super-admin/model/status", response_model=ModelStatus)
def model_status(
    _: User = Depends(super_admin_user),
    db: Session = Depends(get_db),
) -> ModelStatus:
    connection, _, _ = model_connection_from_db(db)
    return ModelStatus(
        configured=bool(connection.base_url and connection.model_name),
        base_url=connection.base_url,
        model_name=connection.model_name,
        json_mode=connection.json_mode,
        tls_verify=connection.tls_verify,
    )
