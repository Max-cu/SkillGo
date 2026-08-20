"""Dependency-free execution session and tool middleware primitives.

The module deliberately stays below the product layer.  ``AgentSession`` gives
the worker an append-only, replayable event vocabulary while persisting through
SkillGo's existing ``AgentRunEvent`` table.  ``ToolPipeline`` adds explicit
before/after extension points without changing the trusted tool implementations.

This is not a second agent framework: SkillGo remains the owner of tenancy,
leases, sandboxes, Skill policy, and artifact delivery.
"""

from __future__ import annotations

import copy
import inspect
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from sqlalchemy.orm import Session

from .execution_runtime import append_run_event
from .models import AgentRun


_SENSITIVE_KEYS = frozenset(
    {
        "code",
        "content",
        "stdout",
        "stderr",
        "skill_md",
        "reasoning_content",
        "arguments",
    }
)
_MAX_STRING = 800
_MAX_DEPTH = 4


def _safe_event_value(value: Any, *, depth: int = 0) -> Any:
    """Return bounded JSON-like data suitable for durable run events.

    Agent events are operational telemetry, not a transcript or artifact
    store.  Keeping this boundary here prevents a future hook from accidentally
    persisting source code, document contents, or model reasoning.
    """

    if depth > _MAX_DEPTH:
        return "[event data truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:_MAX_STRING]
    if isinstance(value, Mapping):
        return {
            str(key)[:80]: _safe_event_value(item, depth=depth + 1)
            for key, item in value.items()
            if str(key) not in _SENSITIVE_KEYS
        }
    if isinstance(value, (list, tuple, set)):
        return [_safe_event_value(item, depth=depth + 1) for item in list(value)[:32]]
    return str(value)[:_MAX_STRING]


@dataclass(frozen=True)
class AgentEvent:
    """One immutable in-process view of a persisted Agent event."""

    sequence: int
    session_sequence: int
    event_type: str
    status: str
    created_at: datetime
    data: dict[str, Any]


class AgentSession:
    """Append-only session facade over an existing AgentRun.

    The database remains the source of truth.  ``events`` is only a compact
    in-process projection useful for tests and for code that needs the current
    session boundary without querying the database again.
    """

    def __init__(self, db: Session, run: AgentRun) -> None:
        self.db = db
        self.run = run
        self.events: list[AgentEvent] = []
        self.status = "idle"
        self._session_sequence = 0

    @property
    def sequence(self) -> int:
        return self._session_sequence

    @property
    def is_idle(self) -> bool:
        return self.status == "idle"

    def append(
        self,
        event_type: str,
        *,
        status: str = "running",
        data: Mapping[str, Any] | None = None,
    ) -> AgentEvent:
        self._session_sequence += 1
        payload = _safe_event_value(dict(data or {}))
        if not isinstance(payload, dict):
            payload = {"value": payload}
        payload = {"session_sequence": self._session_sequence, **payload}
        event = append_run_event(
            self.db,
            self.run,
            f"agent.{event_type}"[:48],
            status=status,
            data=payload,
        )
        projected = AgentEvent(
            sequence=event.sequence,
            session_sequence=self._session_sequence,
            event_type=event.event_type,
            status=event.status,
            created_at=event.created_at or datetime.now(timezone.utc),
            data=copy.deepcopy(payload),
        )
        self.events.append(projected)
        return projected

    def start_turn(self, turn: int) -> None:
        self.status = "running"
        self.append("turn.started", data={"turn": turn})

    def start_step(self, turn: int, step: int = 1) -> None:
        self.append("step.started", data={"turn": turn, "step": step})

    def assistant_result(
        self,
        *,
        turn: int,
        step: int = 1,
        model_name: str | None = None,
        tool_call_count: int = 0,
        text_length: int = 0,
        token_usage: Mapping[str, Any] | None = None,
    ) -> None:
        self.append(
            "assistant.message",
            status="succeeded",
            data={
                "turn": turn,
                "step": step,
                "model_name": model_name or "",
                "tool_call_count": tool_call_count,
                "text_length": max(0, int(text_length)),
                "token_usage": dict(token_usage or {}),
            },
        )

    def tool_call(self, context: "ToolCallContext") -> None:
        self.append(
            "tool.call",
            data={
                "turn": context.turn,
                "step": context.step,
                "operation": context.operation,
                "tool": context.name,
                "tool_call_id": context.tool_call_id or "",
            },
        )

    def tool_result(self, context: "ToolCallContext", payload: Any) -> None:
        succeeded = not (
            isinstance(payload, Mapping)
            and (
                payload.get("ok") is False
                or (
                    isinstance(payload.get("exit_code"), int)
                    and payload.get("exit_code") != 0
                )
            )
        )
        data: dict[str, Any] = {
            "turn": context.turn,
            "step": context.step,
            "operation": context.operation,
            "tool": context.name,
            "tool_call_id": context.tool_call_id or "",
        }
        if isinstance(payload, Mapping):
            for key in ("error_code", "path", "bytes", "exit_code", "full_result_path"):
                if key in payload:
                    data[key] = payload[key]
        self.append("tool.result", status="succeeded" if succeeded else "failed", data=data)

    def finish_step(self, turn: int, step: int = 1) -> None:
        self.append("step.finished", status="succeeded", data={"turn": turn, "step": step})

    def finish_turn(self, turn: int, *, reason: str = "completed") -> None:
        self.append(
            "turn.finished",
            status="succeeded" if reason == "completed" else "failed",
            data={"turn": turn, "reason": reason},
        )
        self.status = "idle"

    def checkpoint(self, *, turn: int, state: Mapping[str, Any] | None = None) -> None:
        self.append("checkpoint", data={"turn": turn, "state": dict(state or {})})


@dataclass(frozen=True)
class ToolCallContext:
    """Stable identity passed through every tool middleware stage."""

    turn: int
    step: int
    operation: int
    name: str
    action: Mapping[str, Any]
    tool_call_id: str | None = None


BeforeHook = Callable[[ToolCallContext], Any]
AfterHook = Callable[[ToolCallContext, Any], Any]


class ToolPipeline:
    """Small ordered tool middleware chain.

    A before hook may return ``None``/``True`` to continue, a string to reject
    the call, or a replacement action mapping.  After hooks may observe or
    replace the model-facing result.  Sync and async hooks are both accepted so
    future policy providers can perform I/O without changing the call site.
    """

    def __init__(self) -> None:
        self._before: list[BeforeHook] = []
        self._after: list[AfterHook] = []

    def add_before(self, hook: BeforeHook) -> None:
        self._before.append(hook)

    def add_after(self, hook: AfterHook) -> None:
        self._after.append(hook)

    async def before(self, context: ToolCallContext) -> tuple[ToolCallContext, str | None]:
        current = context
        for hook in self._before:
            result = hook(current)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, str):
                return current, result
            if isinstance(result, Mapping):
                action = dict(result)
                current = ToolCallContext(
                    turn=current.turn,
                    step=current.step,
                    operation=current.operation,
                    name=str(action.get("action") or current.name),
                    action=action,
                    tool_call_id=current.tool_call_id,
                )
        return current, None

    async def after(self, context: ToolCallContext, payload: Any) -> Any:
        current = payload
        for hook in self._after:
            result = hook(context, current)
            if inspect.isawaitable(result):
                result = await result
            if result is not None:
                current = result
        return current
