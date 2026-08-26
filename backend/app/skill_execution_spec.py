from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any


class SkillExecutionSpecError(ValueError):
    pass


@dataclass(frozen=True)
class FixedExecutionSpec:
    entrypoint: tuple[str, ...]
    verifier: tuple[str, ...] | None
    timeout_seconds: int
    verifier_timeout_seconds: int


_SHELL_TOKENS = frozenset(
    {"|", "||", "&&", ";", "&", ">", ">>", "<", "<<", "2>", "2>>", "2>&1"}
)


def _fixed_argv(value: object, *, field: str, required: bool) -> tuple[str, ...] | None:
    if value is None and not required:
        return None
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and item for item in value
    ):
        raise SkillExecutionSpecError(f"{field} must be a non-empty argv string array")
    if len(value) > 64 or any(len(item) > 1000 for item in value):
        raise SkillExecutionSpecError(
            f"{field} may contain at most 64 items of 1000 characters"
        )
    program = PurePosixPath(value[0]).name.casefold()
    if program in {"sh", "bash", "powershell", "pwsh", "cmd", "cmd.exe"}:
        raise SkillExecutionSpecError(f"{field} cannot invoke a shell")
    if any(
        item in _SHELL_TOKENS
        or item.startswith((">/", ">>/", "1>/", "1>>/", "2>/", "2>>/"))
        for item in value[1:]
    ):
        raise SkillExecutionSpecError(f"{field} cannot contain shell operators")
    if any("{{" in item or "${" in item for item in value):
        raise SkillExecutionSpecError(
            f"{field} must be immutable and cannot interpolate user input"
        )
    return tuple(value)


def _timeout(value: object, *, field: str, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 300:
        raise SkillExecutionSpecError(f"{field} must be an integer between 1 and 300")
    return value


def fixed_execution_spec(manifest: dict[str, Any]) -> FixedExecutionSpec | None:
    """Parse the optional immutable fixed-entrypoint execution contract."""

    spec = manifest.get("spec") if isinstance(manifest.get("spec"), dict) else {}
    execution = spec.get("execution")
    if execution is None:
        return None
    if not isinstance(execution, dict):
        raise SkillExecutionSpecError("spec.execution must be an object")
    mode = str(execution.get("mode") or "agent").strip().casefold()
    if mode in {"agent", "adaptive"}:
        return None
    if mode != "fixed":
        raise SkillExecutionSpecError("spec.execution.mode must be agent or fixed")
    entrypoint = _fixed_argv(
        execution.get("entrypoint"),
        field="spec.execution.entrypoint",
        required=True,
    )
    verifier = _fixed_argv(
        execution.get("verifier"),
        field="spec.execution.verifier",
        required=False,
    )
    return FixedExecutionSpec(
        entrypoint=entrypoint or (),
        verifier=verifier,
        timeout_seconds=_timeout(
            execution.get("timeoutSeconds"),
            field="spec.execution.timeoutSeconds",
            default=300,
        ),
        verifier_timeout_seconds=_timeout(
            execution.get("verifierTimeoutSeconds"),
            field="spec.execution.verifierTimeoutSeconds",
            default=120,
        ),
    )
