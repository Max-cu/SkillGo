"""Spawn model-authored subtasks inside the current job.

The main model decides the split at runtime and calls spawn_subtasks. Each
child is an in-process model loop with its own instruction, read paths, and
write allowlist. Children do not see the main conversation, cannot spawn
further subtasks, and do not claim another job-worker slot. A failed child
does not cancel the others. Each child has its own model-retry budget.

Sandbox reads and writes are serialized on the shared container; model
requests run concurrently, bounded per job and across every worker process
in this deployment. When the batch returns, the main model is told to run
its own cross-subtask checks before continuing.

Worker processes do not share memory. The in-flight model count is a lease
table in this deployment's database. Another deployment has its own database.
Deployments that share one model server must keep the sum of their caps at or
below 20 until a long-input load test says otherwise. Every worker process in
a deployment must use the same cap. Ordinary main-agent turns are not counted;
the calling job is blocked inside this tool and is not also calling the model.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Callable

from .config import settings
from .model_gateway import ModelGatewayError
from .model_request_gate import (
    ModelLeaseError,
    default_engine,
    process_holder,
    refresh_lease,
    release_lease,
    try_acquire,
)
from .sandbox_runtime import SandboxRuntimeError


logger = logging.getLogger(__name__)

TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_TASKS = 40
MAX_INSTRUCTION_CHARS = 80_000
MAX_OUTPUT_PATHS = 8
MAX_TASK_READABLE_PATHS = 16
MAX_SHARED_READABLE_PATHS = 32
MAX_WRITE_BYTES = 9 * 1024 * 1024
VALIDATION_TIMEOUT_SECONDS = 180
READ_NOT_FOUND = frozenset({"SANDBOX_READ_FAILED", "SANDBOX_PATH_DENIED"})
RETRYABLE_CODES = frozenset({
    "MODEL_RESPONSE_TIMEOUT",
    "MODEL_FIRST_RESPONSE_TIMEOUT",
    "MODEL_STREAM_STALLED",
    "MODEL_TRANSPORT_ERROR",
    "MODEL_CONNECTION_FAILED",
})

SUBTASK_WORKER_SYSTEM_PROMPT = """You complete exactly one subtask.
Follow the instruction in this message. Read only the listed files. Shared paths are read-only context and must not be written.
Write only the declared output paths. Any other write is rejected.
If a validation command is given, run it with that exact argv after every output exists. Validation has a read-only workspace and private /tmp; it cannot modify outputs or shared files. If it fails, fix your outputs with write_file and run that same command again. Call finish_task only after it succeeds on the current bytes.
If no validation command is given, call finish_task after every declared output has been written.
You cannot start further subtasks.
You do not have the parent conversation. Do not assume files or decisions that are not in this message or in the files you are allowed to read."""

MAIN_MODEL_NEXT_STEP = (
    "Control has returned to the main model. "
    "Run your own cross-subtask checks before continuing. "
    "Retry only the tasks in tasks_failed and leave tasks_ok in place."
)

SUBTASK_WORKER_TOOLS: list[dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a text file from this subtask's allowlist, including its own output paths.",
        "parameters": {"type": "object", "additionalProperties": False, "required": ["path"], "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 120000},
        }},
    }},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Write one declared output path. Other paths are rejected.",
        "parameters": {"type": "object", "additionalProperties": False, "required": ["path", "content"], "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        }},
    }},
    {"type": "function", "function": {
        "name": "command",
        "description": "Run this subtask's validation command. argv must match the declared command exactly.",
        "parameters": {"type": "object", "additionalProperties": False, "required": ["argv"], "properties": {
            "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 64},
        }},
    }},
    {"type": "function", "function": {
        "name": "finish_task",
        "description": "Finish this subtask after every current output has passed the validation command.",
        "parameters": {"type": "object", "additionalProperties": False, "properties": {
            "summary": {"type": "string", "maxLength": 500},
        }},
    }},
]


class SubtaskRunCancelled(Exception):
    """The parent job was cancelled while subtask workers were running."""


class SubtaskTimeout(Exception):
    pass


class _OversizedFile(Exception):
    pass


@dataclass
class Subtask:
    task_id: str
    instruction: str
    output_paths: list[str]
    read_paths: list[str]
    validation_argv: list[str] | None


@dataclass
class SubtaskOutcome:
    task_id: str
    ok: bool
    error_code: str | None = None
    reason: str | None = None
    summary: str = ""
    wrote: bool = False
    metrics: dict[str, Any] = field(default_factory=dict)


def merge_job_metrics(previous: dict | None, metrics: dict) -> dict:
    previous = previous or {}

    def total(key: str) -> int:
        return int(previous.get(key) or 0) + int(metrics.get(key) or 0)

    return {
        "model_wait_ms": total("model_wait_ms"),
        "tool_exec_ms": total("tool_exec_ms"),
        "rounds": total("rounds"),
        "capacity_wait_ms": total("capacity_wait_ms"),
        "calls": int(previous.get("calls") or 0) + 1,
    }


def retryable_model_error(exc: ModelGatewayError) -> bool:
    status = (exc.details or {}).get("http_status")
    if isinstance(status, int) and not isinstance(status, bool):
        if status == 429 or 500 <= status <= 599:
            return True
    return exc.code in RETRYABLE_CODES


def backoff_seconds(attempt: int, exc: ModelGatewayError) -> float:
    retry_after = (exc.details or {}).get("retry_after_seconds")
    if isinstance(retry_after, int) and not isinstance(retry_after, bool) and retry_after >= 0:
        return min(30.0, float(retry_after))
    base = min(20.0, 0.5 * (2 ** attempt))
    return base * (0.5 + random.random() * 0.5)


def normalize_workspace_path(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("path must be a non-empty string")
    path = PurePosixPath(value.strip())
    if not path.is_absolute():
        path = PurePosixPath("/workspace") / path
    parts: list[str] = []
    for part in path.parts:
        if part == "..":
            raise ValueError("path must stay inside /workspace")
        if part in ("", "."):
            continue
        parts.append(part)
    if len(parts) < 3 or parts[0] != "/" or parts[1] != "workspace":
        raise ValueError("path must be a file inside /workspace")
    return str(PurePosixPath(*parts))


def normalize_cwd(value: str) -> str:
    if value == "/workspace":
        return value
    return normalize_workspace_path(value)


def _command_error(argv: Any, cwd: str | None, timeout: Any = None) -> str | None:
    from .sandbox_tool_registry import validate_agent_action

    action: dict[str, Any] = {"action": "command", "argv": argv}
    if cwd is not None:
        action["cwd"] = cwd
    if timeout is not None:
        action["timeout_seconds"] = timeout
    return validate_agent_action(action)


def _path_list(value: Any, *, label: str, minimum: int, maximum: int) -> tuple[list[str], str | None]:
    if value is None:
        if minimum == 0:
            return [], None
        return [], f"{label} is required"
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        return [], f"{label} must contain {minimum}-{maximum} paths"
    paths: list[str] = []
    for index, item in enumerate(value, 1):
        if not isinstance(item, str):
            return [], f"{label} item {index} must be text"
        try:
            paths.append(normalize_workspace_path(item))
        except ValueError as exc:
            return [], f"{label} item {index} {exc}"
    if len(set(paths)) != len(paths):
        return [], f"{label} contains a duplicate path"
    return paths, None


def validate_spawn_subtasks_action(action: dict[str, Any]) -> str | None:
    """Reject a malformed spawn before any child loop starts."""

    allowed = {"action", "reason", "tasks", "shared_paths", "cwd"}
    extra = sorted(set(action) - allowed)
    if extra:
        return f"spawn_subtasks has unsupported fields: {', '.join(extra[:8])}"
    reason = action.get("reason")
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
        return "reason must contain 1-500 characters"
    cwd = action.get("cwd")
    if cwd is not None:
        if not isinstance(cwd, str):
            return "cwd must be text"
        try:
            normalize_cwd(cwd)
        except ValueError as exc:
            return f"cwd {exc}"
    command_cwd = cwd if isinstance(cwd, str) else None
    shared_paths, path_error = _path_list(
        action.get("shared_paths"),
        label="shared_paths",
        minimum=0,
        maximum=MAX_SHARED_READABLE_PATHS,
    )
    if path_error:
        return path_error
    tasks = action.get("tasks")
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= MAX_TASKS:
        return f"tasks must contain 1-{MAX_TASKS} items"
    seen_ids: set[str] = set()
    seen_outputs: set[str] = set()
    seen_reads = set(shared_paths)
    for index, task in enumerate(tasks, 1):
        if not isinstance(task, dict):
            return f"tasks item {index} must be an object"
        if set(task) - {"task_id", "instruction", "read_paths", "output_paths", "validation_argv"}:
            return f"tasks item {index} has invalid fields"
        task_id = task.get("task_id")
        if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
            return f"tasks item {index} task_id must be a short token"
        if task_id in seen_ids:
            return f"duplicate task_id {task_id}"
        seen_ids.add(task_id)
        instruction = task.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            return f"tasks item {index} instruction must be text"
        if len(instruction) > MAX_INSTRUCTION_CHARS:
            return f"tasks item {index} instruction exceeds {MAX_INSTRUCTION_CHARS} characters"
        outputs, output_error = _path_list(
            task.get("output_paths"),
            label=f"tasks item {index} output_paths",
            minimum=1,
            maximum=MAX_OUTPUT_PATHS,
        )
        if output_error:
            return output_error
        for output in outputs:
            if output in seen_outputs:
                return f"output allowlists overlap at {output}"
            seen_outputs.add(output)
        reads, read_error = _path_list(
            task.get("read_paths"),
            label=f"tasks item {index} read_paths",
            minimum=0,
            maximum=MAX_TASK_READABLE_PATHS,
        )
        if read_error:
            return read_error
        seen_reads.update(reads)
        if "validation_argv" in task and (error := _command_error(task.get("validation_argv"), command_cwd)):
            return f"tasks item {index} validation_argv {error}"
    overlap = seen_reads & seen_outputs
    if overlap:
        sample = sorted(overlap)[0]
        return f"shared or read paths cannot include output path {sample}"
    return None


def _empty_metrics(task_id: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "status": "failed",
        "duration_ms": 0,
        "rounds": 0,
        "model_wait_ms": 0,
        "tool_exec_ms": 0,
        "capacity_wait_ms": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }


def _batch_error(code: str, message: str, *, tasks: list[Subtask] | None = None) -> dict[str, Any]:
    failed = [
        {"task_id": task.task_id, "error_code": code, "reason": message}
        for task in (tasks or [])
    ]
    return {
        "ok": False,
        "error_code": code,
        "message": message,
        "tasks_ok": [],
        "tasks_failed": failed,
        "results": [],
        "metrics": {
            "attempted": False,
            "model_wait_ms": 0,
            "tool_exec_ms": 0,
            "rounds": 0,
            "capacity_wait_ms": 0,
            "tasks": [],
        },
        "execution_started": False,
        "hint": "Correct the arguments and call spawn_subtasks again.",
    }


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _usage_tokens(usage: Any) -> tuple[int, int, int]:
    if not isinstance(usage, dict):
        return 0, 0, 0

    def num(key: str) -> int:
        value = usage.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0
        return int(value)

    prompt = num("prompt_tokens")
    completion = num("completion_tokens")
    total = num("total_tokens") or prompt + completion
    return prompt, completion, total


async def _read_full_text(sandbox, path: str, *, max_chars: int) -> str | None:
    try:
        if hasattr(sandbox, "read_text_window"):
            parts: list[str] = []
            offset = 0
            eof = False
            while offset <= max_chars:
                window = await sandbox.read_text_window(path, offset=offset, limit=60_000)
                content = window.get("content") or ""
                parts.append(content)
                eof = bool(window.get("eof"))
                if eof or not content:
                    break
                offset = int(window.get("next_offset") or (offset + len(content)))
            text = "".join(parts)
            if not eof and len(text) > max_chars:
                raise _OversizedFile(path)
            return text
        text = await sandbox.read_text(path, offset=0, limit=max_chars + 1)
    except SandboxRuntimeError as exc:
        if exc.code in READ_NOT_FOUND:
            return None
        raise
    except (KeyError, FileNotFoundError):
        return None
    if len(text) > max_chars:
        raise _OversizedFile(path)
    return text


async def _path_exists(sandbox, path: str) -> bool:
    try:
        if hasattr(sandbox, "read_text_window"):
            await sandbox.read_text_window(path, offset=0, limit=1)
        else:
            await sandbox.read_text(path, offset=0, limit=1)
    except SandboxRuntimeError as exc:
        if exc.code in READ_NOT_FOUND:
            return False
        raise
    except (KeyError, FileNotFoundError):
        return False
    return True


def _build_tasks(action: dict[str, Any]) -> list[Subtask]:
    tasks: list[Subtask] = []
    for raw in action["tasks"]:
        argv = raw.get("validation_argv")
        if argv is not None:
            argv = [str(item) for item in argv]
        outputs, _ = _path_list(
            raw.get("output_paths"),
            label="output_paths",
            minimum=1,
            maximum=MAX_OUTPUT_PATHS,
        )
        reads, _ = _path_list(
            raw.get("read_paths"),
            label="read_paths",
            minimum=0,
            maximum=MAX_TASK_READABLE_PATHS,
        )
        tasks.append(Subtask(
            task_id=raw["task_id"],
            instruction=raw["instruction"],
            output_paths=outputs,
            read_paths=reads,
            validation_argv=argv,
        ))
    return tasks


def _subtask_context(task: Subtask, shared: dict[str, Any]) -> dict[str, Any]:
    readable = list(dict.fromkeys([*shared["shared_paths"], *task.read_paths, *task.output_paths]))
    return {
        "task_id": task.task_id,
        "instruction": task.instruction,
        "shared_paths": list(shared["shared_paths"]),
        "read_paths": list(task.read_paths),
        "output_paths": list(task.output_paths),
        "readable_paths": readable,
        "validation_argv": task.validation_argv,
        "cwd": shared["cwd"],
    }


class _WorkerState:
    def __init__(self, task: Subtask) -> None:
        self.task = task
        self.written: set[str] = set()
        self.validated: dict[str, str] = {}
        self.metrics = _empty_metrics(task.task_id)
        self.finished = False
        self.summary = ""

    @property
    def wrote(self) -> bool:
        return bool(self.written)


def _readable_paths(task: Subtask, shared: dict[str, Any]) -> set[str]:
    return set(_subtask_context(task, shared)["readable_paths"])


async def _read_allowlisted(sandbox, path: str, *, offset: int, limit: int) -> dict[str, Any]:
    limit = min(max(int(limit), 1), 60_000)
    offset = max(int(offset), 0)
    if hasattr(sandbox, "read_text_window"):
        window = await sandbox.read_text_window(path, offset=offset, limit=limit)
        return {"ok": True, "path": path, **{key: window.get(key) for key in (
            "content", "offset", "limit", "eof", "truncated", "next_offset", "total_chars",
        ) if key in window or key == "content"}}
    content = await sandbox.read_text(path, offset=offset, limit=limit)
    return {"ok": True, "path": path, "content": content, "offset": offset, "eof": len(content) < limit}


async def _bind_validation(state: _WorkerState, sandbox, task: Subtask) -> str | None:
    if set(task.output_paths) - state.written:
        missing = next(path for path in task.output_paths if path not in state.written)
        state.validated = {}
        return missing
    digests: dict[str, str] = {}
    for path in task.output_paths:
        text = await _read_full_text(sandbox, path, max_chars=MAX_WRITE_BYTES)
        if text is None:
            state.validated = {}
            return path
        digests[path] = _sha256(text)
    state.validated = digests
    return None


async def _execute_worker_tool(
    state: _WorkerState,
    action: dict[str, Any],
    *,
    sandbox,
    sandbox_lock: asyncio.Lock,
    shared: dict[str, Any],
    binary_suffixes: frozenset[str],
) -> dict[str, Any]:
    name = action.get("action")
    task = state.task
    if name == "read_file":
        try:
            path = normalize_workspace_path(str(action.get("path") or ""))
        except ValueError as exc:
            return {"ok": False, "error_code": "PATH_NOT_ALLOWED", "message": str(exc)}
        if path not in _readable_paths(task, shared):
            return {"ok": False, "error_code": "PATH_NOT_ALLOWED", "message": f"read is not allowed for {path}", "path": path}
        if PurePosixPath(path).suffix.lower() in binary_suffixes:
            return {
                "ok": False,
                "error_code": "SANDBOX_READ_BINARY",
                "message": "Binary files are not returned as text. Reference the path in your instruction.",
                "path": path,
            }
        offset = action.get("offset") or 0
        limit = action.get("limit") or 60_000
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            return {"ok": False, "error_code": "SANDBOX_ACTION_INVALID", "message": "offset must be an integer >= 0"}
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 120_000:
            return {"ok": False, "error_code": "SANDBOX_ACTION_INVALID", "message": "limit must be 1-120000"}
        started = time.perf_counter()
        async with sandbox_lock:
            try:
                payload = await _read_allowlisted(sandbox, path, offset=offset, limit=limit)
            except SandboxRuntimeError as exc:
                payload = {"ok": False, "error_code": exc.code, "message": str(exc)[:1000], "path": path}
            except (KeyError, FileNotFoundError):
                payload = {"ok": False, "error_code": "SANDBOX_READ_FAILED", "message": f"file does not exist: {path}", "path": path}
        state.metrics["tool_exec_ms"] += _elapsed_ms(started)
        return payload
    if name == "write_file":
        try:
            path = normalize_workspace_path(str(action.get("path") or ""))
        except ValueError as exc:
            return {"ok": False, "error_code": "PATH_NOT_ALLOWED", "message": str(exc)}
        if path not in task.output_paths:
            return {
                "ok": False,
                "error_code": "PATH_NOT_ALLOWED",
                "message": f"this subtask may write only {', '.join(task.output_paths)}",
                "path": path,
            }
        content = action.get("content")
        if not isinstance(content, str):
            return {"ok": False, "error_code": "SANDBOX_ACTION_INVALID", "message": "content must be text"}
        if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
            return {"ok": False, "error_code": "SANDBOX_WRITE_TOO_LARGE", "message": "output exceeds the write limit"}
        started = time.perf_counter()
        async with sandbox_lock:
            try:
                writer = getattr(sandbox, "write_subtask_text", None)
                if writer is None:
                    raise SandboxRuntimeError("SUBTASK_ISOLATION_UNAVAILABLE", "Guarded subtask writes are unavailable")
                await writer(path, content)
            except SandboxRuntimeError as exc:
                state.metrics["tool_exec_ms"] += _elapsed_ms(started)
                return {"ok": False, "error_code": exc.code, "message": str(exc)[:1000], "path": path, "execution_started": True}
        state.metrics["tool_exec_ms"] += _elapsed_ms(started)
        state.written.add(path)
        state.validated = {}
        return {"ok": True, "path": path, "bytes": len(content.encode("utf-8"))}
    if name == "spawn_subtasks":
        return {
            "ok": False,
            "error_code": "NESTED_SPAWN_FORBIDDEN",
            "message": "Subtasks cannot spawn further subtasks.",
        }
    if name == "command":
        argv = action.get("argv")
        if not task.validation_argv or argv != task.validation_argv:
            return {
                "ok": False,
                "error_code": "COMMAND_NOT_ALLOWED",
                "message": "only the declared validation command may run",
            }
        timeout = min(VALIDATION_TIMEOUT_SECONDS, settings.sandbox_command_timeout_seconds)
        started = time.perf_counter()
        async with sandbox_lock:
            try:
                validator = getattr(sandbox, "validation_command", None)
                if validator is None:
                    raise SandboxRuntimeError("SUBTASK_ISOLATION_UNAVAILABLE", "Read-only validation is unavailable")
                result = await validator(argv, cwd=shared["cwd"], timeout_seconds=timeout)
            except SandboxRuntimeError as exc:
                state.metrics["tool_exec_ms"] += _elapsed_ms(started)
                return {"ok": False, "error_code": exc.code, "message": str(exc)[:1000], "execution_started": True}
            missing = None
            if result.exit_code == 0 and not result.timed_out:
                try:
                    missing = await _bind_validation(state, sandbox, task)
                except (_OversizedFile, SandboxRuntimeError, KeyError, FileNotFoundError):
                    state.validated = {}
                    missing = task.output_paths[0]
        state.metrics["tool_exec_ms"] += _elapsed_ms(started)
        payload = {
            "ok": result.exit_code == 0 and not result.timed_out and missing is None and bool(state.validated),
            "exit_code": result.exit_code,
            "stdout": (result.stdout or "")[:4000],
            "stderr": (result.stderr or "")[:4000],
            "timed_out": bool(result.timed_out),
            "execution_started": True,
        }
        if result.exit_code != 0 or result.timed_out:
            payload["error_code"] = "TASK_VALIDATION_FAILED"
        elif missing is not None or not state.validated:
            payload["ok"] = False
            payload["error_code"] = "OUTPUT_MISSING"
            payload["message"] = f"write every declared output before validation counts; missing {missing}"
        return payload
    if name == "finish_task":
        summary = action.get("summary") or ""
        if summary and not isinstance(summary, str):
            return {"ok": False, "error_code": "SANDBOX_ACTION_INVALID", "message": "summary must be text"}
        started = time.perf_counter()
        current: dict[str, str] = {}
        missing = None
        async with sandbox_lock:
            try:
                for path in task.output_paths:
                    text = await _read_full_text(sandbox, path, max_chars=MAX_WRITE_BYTES)
                    if text is None:
                        missing = path
                        break
                    current[path] = _sha256(text)
            except (_OversizedFile, SandboxRuntimeError, KeyError, FileNotFoundError):
                missing = missing or task.output_paths[0]
        state.metrics["tool_exec_ms"] += _elapsed_ms(started)
        if missing is not None or len(current) != len(task.output_paths):
            return {
                "ok": False,
                "error_code": "OUTPUT_MISSING",
                "message": f"write every declared output before finish_task; missing {missing or task.output_paths[0]}",
            }
        if task.validation_argv:
            if not state.validated or current != state.validated:
                return {
                    "ok": False,
                    "error_code": "VALIDATION_REQUIRED",
                    "message": "Run the validation command against the current outputs before finish_task.",
                }
        elif set(task.output_paths) - state.written:
            missing_write = next(path for path in task.output_paths if path not in state.written)
            return {
                "ok": False,
                "error_code": "OUTPUT_MISSING",
                "message": f"write {missing_write} before finish_task",
            }
        state.finished = True
        state.summary = str(summary)[:500]
        return {"ok": True, "output_paths": list(task.output_paths), "summary": state.summary}
    return {"ok": False, "error_code": "SANDBOX_ACTION_INVALID", "message": f"subtask workers cannot call {name}"}


def _append_tool_message(messages: list[dict[str, Any]], tool_call_id: str | None, payload: dict[str, Any]) -> None:
    messages.append({
        "role": "tool",
        "tool_call_id": tool_call_id or "call",
        "content": json.dumps(payload, ensure_ascii=False),
    })


async def _sleep_backoff(seconds: float, deadline: float, job_cancelled: Callable[[], bool]) -> None:
    end = time.monotonic() + seconds
    while True:
        if job_cancelled():
            raise SubtaskRunCancelled()
        if time.monotonic() >= deadline:
            raise SubtaskTimeout()
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(0.5, remaining))


@contextlib.asynccontextmanager
async def _model_slot(
    *,
    engine,
    job_id: str,
    deadline: float,
    job_cancelled: Callable[[], bool],
    metrics: dict[str, Any],
):
    started = time.perf_counter()
    limit = settings.parallel_subtasks_global_model_concurrency
    ttl = settings.parallel_subtasks_lease_ttl_seconds
    holder = process_holder()
    token = None
    while token is None:
        if job_cancelled():
            raise SubtaskRunCancelled()
        if time.monotonic() >= deadline:
            metrics["capacity_wait_ms"] += _elapsed_ms(started)
            raise SubtaskTimeout()
        try:
            token = await asyncio.to_thread(
                try_acquire,
                engine,
                limit=limit,
                ttl_seconds=ttl,
                job_id=job_id,
                holder=holder,
            )
        except ModelLeaseError:
            raise
        except Exception as exc:
            raise ModelLeaseError("model request lease is unavailable") from exc
        if token is None:
            await _sleep_backoff(0.2 + random.random() * 0.15, deadline, job_cancelled)
    metrics["capacity_wait_ms"] += _elapsed_ms(started)
    stop = asyncio.Event()
    owner = asyncio.current_task()
    lost = False

    def lose_lease() -> None:
        nonlocal lost
        lost = True
        if owner is not None:
            owner.cancel()

    async def heartbeat() -> None:
        interval = max(5, ttl // 3)
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                try:
                    ok = await asyncio.wait_for(
                        asyncio.to_thread(refresh_lease, engine, token, ttl_seconds=ttl),
                        timeout=max(1, ttl // 3),
                    )
                except Exception:
                    logger.warning("parallel subtask model lease refresh failed job_id=%s", job_id)
                    lose_lease()
                    return
                if not ok:
                    logger.warning("parallel subtask model lease lost job_id=%s", job_id)
                    lose_lease()
                    return

    beat = asyncio.create_task(heartbeat())
    try:
        try:
            yield
        finally:
            stop.set()
            if not beat.done():
                beat.cancel()
            try:
                await asyncio.gather(beat, return_exceptions=True)
            finally:
                try:
                    await asyncio.to_thread(release_lease, engine, token)
                except Exception:
                    logger.warning("parallel subtask model lease release failed job_id=%s", job_id)
    except asyncio.CancelledError as exc:
        if lost and owner is not None:
            # Consume only our cancellation; preserve simultaneous parent cancellation.
            owner.uncancel()
            if not owner.cancelling() and not job_cancelled():
                raise ModelLeaseError("model request lease was lost") from exc
        raise


async def _run_worker(
    task: Subtask,
    *,
    sandbox,
    gateway,
    sandbox_lock: asyncio.Lock,
    shared: dict[str, Any],
    engine,
    job_id: str,
    job_cancelled: Callable[[], bool],
    deadline: float,
    binary_suffixes: frozenset[str],
) -> SubtaskOutcome:
    state = _WorkerState(task)
    for path in task.read_paths:
        async with sandbox_lock:
            exists = await _path_exists(sandbox, path)
        if not exists:
            state.metrics["status"] = "failed"
            state.metrics["duration_ms"] = 0
            return SubtaskOutcome(
                task.task_id,
                False,
                "INPUT_NOT_FOUND",
                f"read path was not found at {path}",
                metrics=state.metrics,
            )
    context = _subtask_context(task, shared)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SUBTASK_WORKER_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
    ]
    max_rounds = settings.parallel_subtasks_max_rounds
    retries = settings.parallel_subtasks_model_retries
    started = time.perf_counter()
    try:
        for _round in range(1, max_rounds + 1):
            if job_cancelled():
                raise SubtaskRunCancelled()
            if time.monotonic() >= deadline:
                raise SubtaskTimeout()
            result = None
            last_error: ModelGatewayError | None = None
            for attempt in range(retries + 1):
                if job_cancelled():
                    raise SubtaskRunCancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SubtaskTimeout()
                call_started = time.perf_counter()
                try:
                    async with _model_slot(
                        engine=engine,
                        job_id=job_id,
                        deadline=deadline,
                        job_cancelled=job_cancelled,
                        metrics=state.metrics,
                    ):
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise SubtaskTimeout()
                        result = await asyncio.wait_for(
                            gateway.agent_step(messages=messages, tools=SUBTASK_WORKER_TOOLS),
                            timeout=remaining,
                        )
                    state.metrics["model_wait_ms"] += _elapsed_ms(call_started)
                    break
                except TimeoutError as exc:
                    state.metrics["model_wait_ms"] += _elapsed_ms(call_started)
                    raise SubtaskTimeout() from exc
                except ModelGatewayError as exc:
                    state.metrics["model_wait_ms"] += _elapsed_ms(call_started)
                    last_error = exc
                    if attempt >= retries or not retryable_model_error(exc):
                        break
                    await _sleep_backoff(backoff_seconds(attempt, exc), deadline, job_cancelled)
                except ModelLeaseError as exc:
                    state.metrics["model_wait_ms"] += _elapsed_ms(call_started)
                    state.metrics["rounds"] += 1
                    state.metrics["status"] = "failed"
                    state.metrics["duration_ms"] = _elapsed_ms(started)
                    return SubtaskOutcome(
                        task.task_id, False, "MODEL_LEASE_LOST", str(exc),
                        wrote=state.wrote, metrics=state.metrics,
                    )
            state.metrics["rounds"] += 1
            if result is None:
                reason = str(last_error)[:500] if last_error else "model call failed"
                code = last_error.code if last_error else "TASK_MODEL_FAILED"
                state.metrics["status"] = "failed"
                state.metrics["duration_ms"] = _elapsed_ms(started)
                return SubtaskOutcome(task.task_id, False, code, reason, wrote=state.wrote, metrics=state.metrics)
            prompt, completion, total = _usage_tokens(getattr(result, "token_usage", None))
            state.metrics["prompt_tokens"] += prompt
            state.metrics["completion_tokens"] += completion
            state.metrics["total_tokens"] += total
            assistant = getattr(result, "assistant_message", None)
            messages.append(assistant if isinstance(assistant, dict) else {"role": "assistant", "content": ""})
            calls = list(getattr(result, "tool_calls", ()) or ())
            if not calls:
                calls = [type("Call", (), {"id": getattr(result, "tool_call_id", None), "action": result.output})()]
            for call in calls:
                action = dict(getattr(call, "action", {}) or {})
                payload = await _execute_worker_tool(
                    state,
                    action,
                    sandbox=sandbox,
                    sandbox_lock=sandbox_lock,
                    shared=shared,
                    binary_suffixes=binary_suffixes,
                )
                _append_tool_message(messages, getattr(call, "id", None), payload)
                if state.finished:
                    state.metrics["status"] = "ok"
                    state.metrics["duration_ms"] = _elapsed_ms(started)
                    return SubtaskOutcome(
                        task.task_id,
                        True,
                        summary=state.summary,
                        wrote=True,
                        metrics=state.metrics,
                    )
        state.metrics["status"] = "failed"
        state.metrics["duration_ms"] = _elapsed_ms(started)
        return SubtaskOutcome(
            task.task_id,
            False,
            "TASK_ROUND_LIMIT",
            f"subtask stopped after {max_rounds} model rounds",
            wrote=state.wrote,
            metrics=state.metrics,
        )
    except SubtaskTimeout:
        state.metrics["status"] = "failed"
        state.metrics["duration_ms"] = _elapsed_ms(started)
        return SubtaskOutcome(
            task.task_id,
            False,
            "TASK_TIMEOUT",
            f"subtask exceeded {settings.parallel_subtasks_timeout_seconds} seconds",
            wrote=state.wrote,
            metrics=state.metrics,
        )


def _log_subtask(job_id: str, outcome: SubtaskOutcome) -> None:
    metrics = outcome.metrics
    logger.info(
        "parallel subtask worker job_id=%s task_id=%s status=%s duration_ms=%s rounds=%s model_wait_ms=%s tool_exec_ms=%s prompt_tokens=%s completion_tokens=%s total_tokens=%s error_code=%s",
        job_id,
        outcome.task_id,
        "ok" if outcome.ok else "failed",
        metrics.get("duration_ms", 0),
        metrics.get("rounds", 0),
        metrics.get("model_wait_ms", 0),
        metrics.get("tool_exec_ms", 0),
        metrics.get("prompt_tokens", 0),
        metrics.get("completion_tokens", 0),
        metrics.get("total_tokens", 0),
        outcome.error_code or "",
    )


async def spawn_subtasks(
    sandbox,
    action: dict[str, Any],
    *,
    gateway,
    job_id: str,
    job_cancelled: Callable[[], bool] | None = None,
    cwd: str | None = None,
    engine=None,
) -> dict[str, Any]:
    """Run one fresh model loop per subtask, then return control to the caller."""

    cancelled = job_cancelled or (lambda: False)
    if not settings.parallel_subtasks_enabled:
        return _batch_error("SPAWN_SUBTASKS_DISABLED", "Spawn subtasks is disabled")
    if error := validate_spawn_subtasks_action(action):
        return _batch_error("SANDBOX_ACTION_INVALID", error)
    if cancelled():
        raise SubtaskRunCancelled()
    try:
        work_cwd = normalize_cwd(action.get("cwd") or cwd or "/workspace")
    except ValueError as exc:
        return _batch_error("SANDBOX_ACTION_INVALID", f"cwd {exc}")
    shared_paths, path_error = _path_list(
        action.get("shared_paths"),
        label="shared_paths",
        minimum=0,
        maximum=MAX_SHARED_READABLE_PATHS,
    )
    if path_error:
        return _batch_error("SANDBOX_ACTION_INVALID", path_error)
    for path in shared_paths:
        if not await _path_exists(sandbox, path):
            return _batch_error("INPUT_NOT_FOUND", f"shared path was not found at {path}")
    tasks = _build_tasks(action)
    shared = {
        "shared_paths": shared_paths,
        "cwd": work_cwd,
    }
    from .sandbox_tool_registry import BINARY_DOCUMENT_SUFFIXES

    engine = engine or default_engine()
    sandbox_lock = asyncio.Lock()
    job_slots = asyncio.Semaphore(max(1, settings.parallel_subtasks_job_concurrency))
    timeout = settings.parallel_subtasks_timeout_seconds
    logger.info(
        "spawn subtasks start job_id=%s tasks=%d job_concurrency=%d global_model_concurrency=%d",
        job_id,
        len(tasks),
        settings.parallel_subtasks_job_concurrency,
        settings.parallel_subtasks_global_model_concurrency,
    )

    async def run_one(task: Subtask) -> SubtaskOutcome:
        try:
            await job_slots.acquire()
        except asyncio.CancelledError:
            raise
        try:
            if cancelled():
                raise SubtaskRunCancelled()
            return await _run_worker(
                task,
                sandbox=sandbox,
                gateway=gateway,
                sandbox_lock=sandbox_lock,
                shared=shared,
                engine=engine,
                job_id=job_id,
                job_cancelled=cancelled,
                deadline=time.monotonic() + timeout,
                binary_suffixes=BINARY_DOCUMENT_SUFFIXES,
            )
        except SubtaskRunCancelled:
            raise
        except Exception as exc:
            logger.warning(
                "parallel subtask worker crashed job_id=%s task_id=%s error=%s",
                job_id,
                task.task_id,
                type(exc).__name__,
            )
            metrics = _empty_metrics(task.task_id)
            metrics["status"] = "failed"
            return SubtaskOutcome(task.task_id, False, "TASK_WORKER_FAILED", str(exc)[:500], metrics=metrics)
        finally:
            job_slots.release()

    pending = {asyncio.create_task(run_one(task), name=task.task_id) for task in tasks}
    outcomes: list[SubtaskOutcome] = []
    try:
        while pending:
            if cancelled():
                raise SubtaskRunCancelled()
            done, pending = await asyncio.wait(pending, timeout=0.5, return_when=asyncio.FIRST_COMPLETED)
            for item in done:
                outcomes.append(item.result())
    finally:
        for item in pending:
            item.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    by_id = {item.task_id: item for item in outcomes}
    ordered = [by_id[task.task_id] for task in tasks if task.task_id in by_id]
    for outcome in ordered:
        _log_subtask(job_id, outcome)
    tasks_ok = [item.task_id for item in ordered if item.ok]
    tasks_failed = [
        {"task_id": item.task_id, "error_code": item.error_code, "reason": item.reason}
        for item in ordered
        if not item.ok
    ]
    wrote_any = any(item.wrote for item in ordered)
    by_task = {task.task_id: task for task in tasks}
    results = [
        {
            "task_id": item.task_id,
            "ok": item.ok,
            "summary": item.summary,
            "output_paths": list(by_task[item.task_id].output_paths) if item.task_id in by_task else [],
            "error_code": item.error_code,
            "reason": item.reason,
        }
        for item in ordered
    ]
    metrics = {
        "attempted": True,
        "model_wait_ms": sum(int(item.metrics.get("model_wait_ms") or 0) for item in ordered),
        "tool_exec_ms": sum(int(item.metrics.get("tool_exec_ms") or 0) for item in ordered),
        "capacity_wait_ms": sum(int(item.metrics.get("capacity_wait_ms") or 0) for item in ordered),
        "rounds": sum(int(item.metrics.get("rounds") or 0) for item in ordered),
        "tasks": [item.metrics for item in ordered],
    }
    ok = not tasks_failed
    if tasks_failed and tasks_ok:
        error_code = "SUBTASKS_PARTIAL"
    elif tasks_failed:
        error_code = "SUBTASKS_FAILED"
    else:
        error_code = None
    logger.info(
        "spawn subtasks job_id=%s tasks=%d ok=%d failed=%d model_wait_ms=%d tool_exec_ms=%d rounds=%d",
        job_id,
        len(tasks),
        len(tasks_ok),
        len(tasks_failed),
        metrics["model_wait_ms"],
        metrics["tool_exec_ms"],
        metrics["rounds"],
    )
    payload: dict[str, Any] = {
        "ok": ok,
        "tasks_ok": tasks_ok,
        "tasks_failed": tasks_failed,
        "results": results,
        "metrics": metrics,
        "execution_started": wrote_any,
        "next_step": MAIN_MODEL_NEXT_STEP,
        "hint": MAIN_MODEL_NEXT_STEP,
    }
    if error_code:
        payload["error_code"] = error_code
        payload["message"] = (
            f"{len(tasks_ok)} subtasks succeeded, {len(tasks_failed)} failed. {MAIN_MODEL_NEXT_STEP}"
        )
    else:
        payload["message"] = MAIN_MODEL_NEXT_STEP
    return payload
