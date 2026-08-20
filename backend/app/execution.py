from __future__ import annotations

import json
import logging
from time import perf_counter

from jsonschema import SchemaError, ValidationError, validate
from sqlalchemy.orm import Session

from .config import settings
from .model_gateway import ModelGatewayError, OpenAICompatibleGateway
from .models import Run, RunStatus, SkillType, SkillVersion, User, utcnow
from .services import add_audit


logger = logging.getLogger(__name__)


class RunValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_input(
    version: SkillVersion,
    input_data: dict,
    *,
    validate_schema: bool = True,
) -> None:
    try:
        encoded = json.dumps(input_data, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RunValidationError("INPUT_NOT_JSON", "Run input must be valid JSON") from exc
    if len(encoded) > settings.max_run_input_bytes:
        raise RunValidationError("INPUT_TOO_LARGE", "Run input exceeds the configured limit")
    if not validate_schema:
        return
    try:
        validate(instance=input_data, schema=version.input_schema or {})
    except ValidationError as exc:
        location = ".".join(str(part) for part in exc.absolute_path)
        detail = f" at {location}" if location else ""
        raise RunValidationError(
            "INPUT_SCHEMA_MISMATCH", f"Run input does not match the Skill schema{detail}: {exc.message}"
        ) from exc
    except SchemaError as exc:
        raise RunValidationError(
            "SKILL_SCHEMA_INVALID", "The Skill input schema is invalid"
        ) from exc


def _validate_output(version: SkillVersion, output_data: dict) -> None:
    try:
        validate(instance=output_data, schema=version.output_schema or {})
    except ValidationError as exc:
        raise ModelGatewayError(
            "OUTPUT_SCHEMA_MISMATCH",
            f"Private model output does not match the Skill schema: {exc.message}",
        ) from exc
    except SchemaError as exc:
        raise ModelGatewayError(
            "SKILL_SCHEMA_INVALID", "The Skill output schema is invalid"
        ) from exc


async def execute_instruction_run(
    db: Session,
    *,
    run: Run,
    version: SkillVersion,
    actor: User | None,
    gateway: OpenAICompatibleGateway,
    history: list[dict] | None = None,
    chat_mode: bool = False,
    workspace_files: list[dict[str, str]] | None = None,
) -> Run:
    run.status = RunStatus.RUNNING
    run.started_at = utcnow()
    db.commit()
    started = perf_counter()

    try:
        if version.skill_type != SkillType.INSTRUCTION:
            raise ModelGatewayError(
                "RUNTIME_UNSUPPORTED",
                "Code Skills must run through the isolated sandbox runtime",
            )
        result = await gateway.execute(
            skill_md=version.skill_md,
            input_schema=version.input_schema,
            output_schema=version.output_schema,
            input_data=run.input_data,
            history=history,
            chat_mode=chat_mode,
            workspace_files=workspace_files,
        )
        _validate_output(version, result.output)
        run.output_data = result.output
        run.model_name = result.model_name
        run.token_usage = result.token_usage
        run.status = RunStatus.SUCCEEDED
        run.error_code = None
        run.error_message = None
    except ModelGatewayError as exc:
        run.status = RunStatus.FAILED
        run.error_code = exc.code
        run.error_message = str(exc)[:4000]
    except Exception:
        logger.exception("Unexpected instruction runtime failure", extra={"run_id": run.id})
        run.status = RunStatus.FAILED
        run.error_code = "RUNTIME_INTERNAL_ERROR"
        run.error_message = "The Skill runtime failed unexpectedly"

    run.latency_ms = max(0, round((perf_counter() - started) * 1000))
    run.finished_at = utcnow()
    add_audit(
        db,
        actor=actor,
        action="run.succeeded" if run.status == RunStatus.SUCCEEDED else "run.failed",
        resource_type="run",
        resource_id=run.id,
        details={
            "status": run.status.value,
            "error_code": run.error_code,
            "latency_ms": run.latency_ms,
        },
    )
    db.commit()
    db.refresh(run)
    return run
