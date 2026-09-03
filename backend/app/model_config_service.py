from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .database import SessionLocal
from .model_gateway import ModelConnection, environment_model_connection
from .models import ModelConnectionConfig, ModelProviderConfig


DEFAULT_CONFIG_ID = "default"
MODEL_CAPABILITIES = frozenset({"chat", "vision", "ocr"})


def normalize_models(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        model = str(value).strip()
        if model and model not in normalized:
            normalized.append(model)
    return tuple(normalized)


def normalize_capabilities(values: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values or ("chat",):
        capability = str(value).strip().casefold()
        if capability in MODEL_CAPABILITIES and capability not in normalized:
            normalized.append(capability)
    return tuple(normalized or ["chat"])


def _row_connection(
    row: ModelConnectionConfig,
    *,
    available_chat_models: tuple[str, ...],
) -> ModelConnection:
    defaults: list[str] = []
    if row.is_default:
        defaults.append("chat")
    if row.is_default_vision:
        defaults.append("vision")
    if row.is_default_ocr:
        defaults.append("ocr")
    return ModelConnection(
        base_url=row.base_url,
        api_key=row.api_key or settings.model_api_key,
        model_name=row.model_name,
        models=available_chat_models,
        timeout_seconds=float(row.timeout_seconds),
        temperature=max(0, min(row.temperature_milli, 2000)) / 1000,
        json_mode=row.json_mode,
        native_tools=row.native_tools,
        tls_verify=row.tls_verify,
        capabilities=normalize_capabilities(row.capabilities),
        default_capabilities=tuple(defaults),
    )


def ensure_model_connection_rows(db: Session) -> list[ModelConnectionConfig]:
    rows = list(db.scalars(select(ModelConnectionConfig).order_by(ModelConnectionConfig.created_at)))
    if rows:
        return rows
    legacy = db.get(ModelProviderConfig, DEFAULT_CONFIG_ID)
    connection = (
        ModelConnection(
            base_url=legacy.base_url,
            api_key=legacy.api_key or settings.model_api_key,
            model_name=legacy.default_model,
            models=normalize_models(legacy.models or []),
            timeout_seconds=float(legacy.timeout_seconds),
            temperature=max(0, min(legacy.temperature_milli, 2000)) / 1000,
            json_mode=legacy.json_mode,
            native_tools=legacy.native_tools,
            tls_verify=legacy.tls_verify,
        )
        if legacy is not None
        else environment_model_connection()
    )
    if not connection.base_url or not connection.models:
        return []
    for name in connection.models:
        db.add(
            ModelConnectionConfig(
                model_name=name,
                base_url=connection.base_url,
                api_key=connection.api_key,
                timeout_seconds=round(connection.timeout_seconds),
                temperature_milli=round(connection.temperature * 1000),
                json_mode=connection.json_mode,
                native_tools=connection.native_tools,
                tls_verify=connection.tls_verify,
                capabilities=["chat"],
                is_default=name == connection.model_name,
                enabled=True,
            )
        )
    db.flush()
    return list(db.scalars(select(ModelConnectionConfig).order_by(ModelConnectionConfig.created_at)))


def model_connection_from_db(db: Session) -> tuple[ModelConnection, str, bool]:
    ensure_model_connection_rows(db)
    rows = list(db.scalars(select(ModelConnectionConfig).where(ModelConnectionConfig.enabled.is_(True)).order_by(ModelConnectionConfig.is_default.desc(), ModelConnectionConfig.created_at)))
    chat_rows = [item for item in rows if "chat" in normalize_capabilities(item.capabilities)]
    if chat_rows:
        selected = next((item for item in chat_rows if item.is_default), chat_rows[0])
        available = tuple(item.model_name for item in chat_rows)
        return (
            _row_connection(selected, available_chat_models=available),
            "database",
            bool(selected.api_key or settings.model_api_key),
        )
    row = db.get(ModelProviderConfig, DEFAULT_CONFIG_ID)
    if row is None:
        connection = environment_model_connection()
        return connection, "environment", bool(connection.api_key)

    models = normalize_models(row.models or [])
    if row.default_model and row.default_model not in models:
        models = (*models, row.default_model)
    api_key = row.api_key or settings.model_api_key
    return (
        ModelConnection(
            base_url=row.base_url,
            api_key=api_key,
            model_name=row.default_model,
            models=models,
            timeout_seconds=float(row.timeout_seconds),
            temperature=max(0, min(row.temperature_milli, 2000)) / 1000,
            json_mode=row.json_mode,
            native_tools=row.native_tools,
            tls_verify=row.tls_verify,
        ),
        "database",
        bool(api_key),
    )


def active_model_connection() -> ModelConnection:
    with SessionLocal() as db:
        connection, _, _ = model_connection_from_db(db)
        return connection


def active_model_connections() -> dict[str, ModelConnection]:
    with SessionLocal() as db:
        ensure_model_connection_rows(db)
        rows = list(
            db.scalars(
                select(ModelConnectionConfig)
                .where(ModelConnectionConfig.enabled.is_(True))
                .order_by(ModelConnectionConfig.is_default.desc(), ModelConnectionConfig.created_at)
            )
        )
        if not rows:
            connection, _, _ = model_connection_from_db(db)
            return {name: connection for name in connection.models}
        available = tuple(
            item.model_name
            for item in rows
            if "chat" in normalize_capabilities(item.capabilities)
        )
        return {
            item.model_name: _row_connection(item, available_chat_models=available)
            for item in rows
        }


def connection_for_model(db: Session, model_name: str | None) -> ModelConnection:
    requested = (model_name or "").strip()
    if requested:
        row = db.scalar(
            select(ModelConnectionConfig).where(
                ModelConnectionConfig.model_name == requested,
                ModelConnectionConfig.enabled.is_(True),
            )
        )
        if row is not None:
            enabled_rows = list(
                db.scalars(
                    select(ModelConnectionConfig)
                    .where(ModelConnectionConfig.enabled.is_(True))
                    .order_by(ModelConnectionConfig.is_default.desc(), ModelConnectionConfig.created_at)
                )
            )
            available = tuple(
                item.model_name
                for item in enabled_rows
                if "chat" in normalize_capabilities(item.capabilities)
            )
            return _row_connection(row, available_chat_models=available)
    connection, _, _ = model_connection_from_db(db)
    return connection
