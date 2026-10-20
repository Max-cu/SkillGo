"""Opt-in spill of oversized tool results into the job workspace.

Disabled unless ``SKILLGO_TOOL_OUTPUT_SPILL`` is on. While it is off, callers
keep today's inline and legacy offload behavior and this module writes nothing.

While it is on, a tool result whose serialized form exceeds
``SKILLGO_TOOL_OUTPUT_SPILL_BYTES`` is written in full to
``/workspace/_tool_output/<round>_<call>.txt``. The model context keeps a
head and tail, the absolute path, and the byte size. The model reads the
omitted middle back with the existing ``read_file`` tool. The directory is
scratch for that job: it is removed when the job sandbox closes and is left
out of workspace snapshots.
"""
from __future__ import annotations

import json
import logging
from pathlib import PurePosixPath
from typing import Any

from .config import settings
from .sandbox_runtime import SandboxRuntimeError, _workspace_path


logger = logging.getLogger(__name__)

TOOL_OUTPUT_ROOT = "/workspace/_tool_output"
# Head and tail stay short enough to keep a round's context usable while
# still showing the command preamble and the final status or traceback.
SPILL_HEAD_BYTES = 2_000
SPILL_TAIL_BYTES = 2_000
_STATUS_KEYS = (
    "ok",
    "exit_code",
    "error_code",
    "elapsed_seconds",
    "timeout_seconds",
    "verification_id",
)

CLEANUP_SCRIPT = r"""
import shutil
import sys
from pathlib import Path

root = Path(sys.argv[1])
if not root.is_absolute():
    raise SystemExit(2)
root_resolved = root.resolve()
target = root / "_tool_output"
if not target.exists() and not target.is_symlink():
    raise SystemExit(0)
if target.is_symlink():
    # Drop the link itself. Never follow it outside the workspace.
    target.unlink()
    raise SystemExit(0)
resolved = target.resolve()
expected = root_resolved / "_tool_output"
if resolved != expected:
    raise SystemExit(3)
if target.is_dir():
    shutil.rmtree(target)
elif target.exists():
    target.unlink()
"""


def snapshot_excluded_prefixes() -> tuple[str, ...]:
    """Prefixes omitted from a workspace snapshot.

    Empty when spill is off, so snapshots of ordinary workspaces stay the same.
    """

    if settings.tool_output_spill_enabled:
        return (TOOL_OUTPUT_ROOT,)
    return ()


def is_tool_output_path(value: str) -> bool:
    """True only for the scratch directory and files nested inside it."""

    path = PurePosixPath(value)
    if not path.is_absolute():
        path = PurePosixPath("/workspace") / path
    if ".." in path.parts:
        return False
    try:
        if not path.is_relative_to("/workspace"):
            return False
    except ValueError:
        return False
    root = PurePosixPath(TOOL_OUTPUT_ROOT)
    return path == root or path.is_relative_to(root)


def tool_output_path(round_number: int, call_number: int) -> str:
    """Build a sandbox-safe spill path from the reasoning round and call index.

    Names are decimal digits only. The result is checked with the same
    workspace rule the sandbox uses for reads and writes.
    """

    if type(round_number) is not int or type(call_number) is not int:
        raise ValueError("tool output round and call must be integers")
    if round_number < 1 or call_number < 1:
        raise ValueError("tool output round and call must be positive")
    if round_number > 1_000_000 or call_number > 1_000_000:
        raise ValueError("tool output round or call is out of range")
    path = f"{TOOL_OUTPUT_ROOT}/{round_number}_{call_number}.txt"
    normalized = _workspace_path(path, allow_root=False)
    if normalized != path or not is_tool_output_path(normalized):
        raise SandboxRuntimeError(
            "SANDBOX_PATH_DENIED",
            "Tool output path must stay inside /workspace/_tool_output",
        )
    return normalized


def serialized_tool_result(action: str, payload: object) -> str:
    return json.dumps(
        {"tool_result": action, "payload": payload},
        ensure_ascii=False,
    )


def head_tail_excerpt(text: str, *, output_path: str | None, total_bytes: int) -> str:
    """Return text unchanged when it already fits, otherwise head + tail."""

    encoded = text.encode("utf-8", errors="ignore")
    if len(encoded) <= SPILL_HEAD_BYTES + SPILL_TAIL_BYTES:
        return text
    head = encoded[:SPILL_HEAD_BYTES].decode("utf-8", errors="ignore")
    tail = encoded[-SPILL_TAIL_BYTES:].decode("utf-8", errors="ignore")
    omitted = total_bytes - len(head.encode("utf-8", errors="ignore")) - len(tail.encode("utf-8", errors="ignore"))
    if output_path:
        location = (
            f"full output is {total_bytes} bytes at {output_path}; "
            "use read_file on that path to read the omitted middle"
        )
    else:
        location = f"full output is {total_bytes} bytes and was not saved"
    return f"{head}\n\n[... {omitted} bytes omitted; {location} ...]\n\n{tail}"


def _keep_inline(action: str, payload: object) -> bool:
    """Bounded reads and pinned references already fit the read_file contract."""

    if action == "read_file":
        return True
    return isinstance(payload, dict) and (
        payload.get("retained") is True or payload.get("reference") is True
    )


def _status_fields(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    fields: dict[str, Any] = {}
    for key in _STATUS_KEYS:
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, bool):
            fields[key] = value
        elif isinstance(value, (int, float)):
            fields[key] = value
        elif isinstance(value, str):
            fields[key] = value[:400]
    for key in ("stderr", "stdout", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            fields["diagnostic_tail"] = value[-640:]
            break
    return fields


def _context_view(
    payload: object,
    full_text: str,
    *,
    output_path: str | None,
    total_bytes: int,
) -> dict[str, Any]:
    view: dict[str, Any] = {
        "truncated": True,
        "bytes": total_bytes,
        "excerpt": head_tail_excerpt(full_text, output_path=output_path, total_bytes=total_bytes),
        **_status_fields(payload),
    }
    if output_path:
        view["output_path"] = output_path
        view["full_result_path"] = output_path
    else:
        view["persist_failed"] = True
        view["hint"] = (
            "Full output could not be written under /workspace/_tool_output. "
            "The excerpt keeps the head and tail only."
        )
    return view


async def spill_tool_result(
    action: str,
    payload: object,
    *,
    sandbox: object,
    round_number: int,
    call_number: int,
) -> object:
    """Return the payload to place in model context.

    The original payload is returned unchanged when spill is off, the result
    is within the threshold, or the result is a bounded read / pinned reference.
    Callers that only run when the flag is on may call this directly.
    """

    if not settings.tool_output_spill_enabled or _keep_inline(action, payload):
        return payload
    full_text = serialized_tool_result(action, payload)
    total_bytes = len(full_text.encode("utf-8"))
    if total_bytes <= settings.tool_output_spill_bytes:
        return payload
    try:
        path = tool_output_path(round_number, call_number)
        await sandbox.write_text(path, full_text)  # type: ignore[attr-defined]
    except (SandboxRuntimeError, ValueError):
        logger.warning(
            "Tool output spill failed round=%s call=%s bytes=%s",
            round_number,
            call_number,
            total_bytes,
            exc_info=True,
        )
        return _context_view(payload, full_text, output_path=None, total_bytes=total_bytes)
    return _context_view(payload, full_text, output_path=path, total_bytes=total_bytes)


async def note_unavailable_tool_outputs(messages: list[dict[str, Any]], sandbox: object) -> None:
    """A resumed transcript can point at scratch files excluded from its snapshot."""

    if not settings.tool_output_spill_enabled:
        return
    paths: set[str] = set()

    def collect(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"output_path", "full_result_path"} and isinstance(item, str):
                    if item.startswith(TOOL_OUTPUT_ROOT + "/") and is_tool_output_path(item):
                        paths.add(item)
                elif isinstance(item, (dict, list)):
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    for message in messages:
        content = message.get("content")
        if not isinstance(content, str):
            continue
        try:
            collect(json.loads(content))
        except (ValueError, TypeError):
            continue
    unavailable = []
    for path in sorted(paths):
        try:
            await sandbox.read_text(path, offset=0, limit=1)
        except (FileNotFoundError, KeyError, SandboxRuntimeError):
            unavailable.append(path)
    if unavailable:
        messages.append({
            "role": "user",
            "content": (
                "Recovered tool output: the original full output is unavailable (原文已丢失). "
                "The previous head/tail excerpts remain valid, but do not read these stale paths. "
                "Recreate the output only if the omitted content is needed:\n" + "\n".join(unavailable)
            ),
        })


def cleanup_argv(root: str = "/workspace") -> list[str]:
    return ["python3", "-c", CLEANUP_SCRIPT, root]


def drop_tool_output_files(files: dict[str, Any]) -> list[str]:
    """Remove scratch entries from an in-memory workspace. Other paths stay."""

    removed: list[str] = []
    for key in list(files):
        if is_tool_output_path(str(key)):
            removed.append(str(key))
            del files[key]
    return removed


async def cleanup_tool_output(sandbox: object) -> None:
    """Delete ``/workspace/_tool_output`` for this job. No-op when spill is off."""

    if not settings.tool_output_spill_enabled:
        return
    files = getattr(sandbox, "files", None)
    if isinstance(files, dict):
        drop_tool_output_files(files)
        return
    command = getattr(sandbox, "command", None)
    if command is None:
        return
    result = await command(cleanup_argv("/workspace"), timeout_seconds=30)
    exit_code = getattr(result, "exit_code", 1)
    if exit_code not in (0,):
        logger.warning(
            "Tool output cleanup exited %s",
            exit_code,
        )


def discard_tool_output_sync(sandbox: object) -> None:
    """Best-effort removal before a Docker sandbox container is destroyed."""

    if not settings.tool_output_spill_enabled:
        return
    container = getattr(sandbox, "container", None)
    if container is None:
        return
    try:
        container.exec_run(cleanup_argv("/workspace"), user="10001:10001")
    except Exception:
        logger.warning(
            "Tool output cleanup failed job_id=%s",
            getattr(sandbox, "job_id", ""),
            exc_info=True,
        )
