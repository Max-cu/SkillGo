"""Inject skill-declared workspace state files into fast-mode model context.

Fast mode compacts old exchanges, so a skill can keep a small JSON file under
``/workspace/_state/`` and declare it in SKILL.md or the skill manifest. When
``SKILLGO_STATE_INJECT`` is on, each fast-mode round reads those files and
places a bounded copy after the stable history prefix and before the mutable
execution-state message.

The durable transcript is not modified. A missing or unreadable file is logged
and skipped; it does not fail the turn. Audit mode does not read or inject
state files. With the flag off, projection matches a run that never declares
any.
"""
from __future__ import annotations

import logging
import re
from pathlib import PurePosixPath
from typing import Any

from .skill_metadata import parse_skill_frontmatter

logger = logging.getLogger(__name__)

# Missing-file warnings are once per job and path. Cleared with job timing.
_MISSING_WARNED: set[tuple[str, str]] = set()

STATE_DIR = "/workspace/_state"
STATE_TOKEN_CAP = 1024
STATE_MAX_BYTES = 128 * 1024
MAX_DECLARED_FILES = 8
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.json$")


def declared_state_paths(skill_contexts: list[dict[str, Any]] | None) -> list[str]:
    """Absolute ``/workspace/_state/<name>.json`` paths, in declaration order.

    Sources, in order, for each skill: frontmatter ``state_files``, frontmatter
    ``metadata.state_files``, manifest ``metadata.state_files``, then manifest
    ``spec.state_files``. Repeated paths are kept once. Entries outside that
    directory, or with more than one path segment, are ignored.
    """

    ordered: list[str] = []
    seen: set[str] = set()
    for context in skill_contexts or []:
        if not isinstance(context, dict):
            continue
        for raw in _declared_entries(context.get("skill_md"), context.get("manifest")):
            path = normalize_state_path(raw)
            if path is None or path in seen:
                continue
            seen.add(path)
            ordered.append(path)
            if len(ordered) >= MAX_DECLARED_FILES:
                return ordered
    return ordered


def normalize_state_path(value: Any) -> str | None:
    """Return the absolute state path, or None when the declaration is unusable."""

    if not isinstance(value, str):
        return None
    text = value.strip().replace("\\", "/")
    if text.startswith("./"):
        text = text[2:]
    prefix = STATE_DIR + "/"
    if text.startswith(prefix):
        name = text[len(prefix):]
    elif text.startswith("_state/"):
        name = text[len("_state/"):]
    else:
        return None
    if not name or "/" in name or name != PurePosixPath(name).name:
        return None
    if ".." in PurePosixPath(name).parts or not _NAME_RE.fullmatch(name):
        return None
    return prefix + name


def annotate_state_age(snapshot: dict[str, Any], job_started_at: float | None) -> dict[str, Any]:
    """Flag a file whose mtime is earlier than the job start."""

    mtime = _coerce_mtime(snapshot.get("file_mtime"))
    started = _coerce_mtime(job_started_at)
    snapshot["stale"] = bool(mtime is not None and started is not None and mtime < started)
    return snapshot


def forget_state_warnings(job_id: str) -> None:
    job = str(job_id)
    for key in [item for item in _MISSING_WARNED if item[0] == job]:
        _MISSING_WARNED.discard(key)


def render_state_file(
    path: str,
    text: str,
    *,
    mtime: float | None,
    token_cap: int,
    stale: bool = False,
) -> str:
    """Header plus the file text. The text itself is not rewritten."""

    header = [f"State file {path}"]
    if mtime is not None:
        header.append(f"mtime={mtime:.3f}")
    if stale:
        header.append(
            "WARNING: stale state file: file mtime is earlier than this job's start. "
            f"Rewrite {path} for this run."
        )
    note = f"[truncated to token cap; re-read {path} for the full file]"

    def assemble(body: str, *, truncated: bool) -> str:
        lines = [note] if truncated else []
        lines.extend(header)
        if body:
            lines.append(body)
        return "\n".join(lines)

    full = assemble(text, truncated=False)
    if _message_tokens(full) <= token_cap:
        return full
    best = ""
    lo = 0
    hi = len(text)
    while lo <= hi:
        mid = (lo + hi) // 2
        piece = text[:mid]
        candidate = assemble(piece, truncated=True)
        if piece and _message_tokens(candidate) <= token_cap:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    if best:
        return best
    return _clip_to_cap(assemble("", truncated=True), token_cap)


def evaluate_state_file(
    *,
    path: str,
    text: str | None,
    mtime: float | None,
    read_error: str | None,
    job_id: str | None = None,
) -> dict[str, Any]:
    """Accept file text as stored. Logs WARNING and skips when it cannot be injected."""

    mtime = _coerce_mtime(mtime)
    base: dict[str, Any] = {
        "path": path,
        "validation": "missing",
        "file_mtime": mtime,
        "text": None,
    }
    if read_error:
        detail = str(read_error)[:120]
        _log_skip(job_id, path, "read_error", detail)
        return {**base, "validation": "read_error", "error": detail}
    if text is None:
        _log_skip(job_id, path, "missing", "file not found")
        return base
    if not isinstance(text, str) or text == "":
        _log_skip(job_id, path, "invalid", "empty")
        return {**base, "validation": "invalid", "error": "empty"}
    return {**base, "validation": "ok", "text": text}


def apply_state_files(
    projected: list[dict[str, Any]],
    *,
    history_len: int,
    snapshots: list[dict[str, Any]] | None,
    token_cap: int,
    max_tokens: int,
    tool_tokens: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Insert each file after ``history_len`` messages when it fits the budget.

    ``history_len`` is the stable prefix. The message already at that index is
    the mutable execution-state user message, so state files sit after history
    and before that last user message. Metrics are keyed by absolute path.
    """

    from .agent_context import estimate_tokens

    working = list(projected)
    cursor = history_len
    metrics: dict[str, Any] = {}
    for snapshot in snapshots or []:
        if not isinstance(snapshot, dict):
            continue
        path = str(snapshot.get("path") or "")
        if not path:
            continue
        validation = str(snapshot.get("validation") or "missing")
        file_metrics: dict[str, Any] = {
            "path": path,
            "validation": validation,
            "injected_tokens": 0,
            "file_mtime": _coerce_mtime(snapshot.get("file_mtime")),
            "stale": bool(snapshot.get("stale")),
        }
        if snapshot.get("error"):
            file_metrics["error"] = str(snapshot["error"])[:120]
        metrics[path] = file_metrics
        text = snapshot.get("text")
        if validation != "ok" or not isinstance(text, str) or text == "":
            continue
        content = render_state_file(
            path,
            text,
            mtime=file_metrics["file_mtime"],
            token_cap=token_cap,
            stale=file_metrics["stale"],
        )
        message = {"role": "user", "content": content}
        injected_tokens = estimate_tokens([message])
        if injected_tokens > token_cap:
            content = _clip_to_cap(content, token_cap)
            message = {"role": "user", "content": content}
            injected_tokens = estimate_tokens([message])
        if injected_tokens > token_cap or not content:
            file_metrics["skip_reason"] = "token_cap"
            continue
        trial = [*working[:cursor], message, *working[cursor:]]
        if estimate_tokens(trial) + tool_tokens > max_tokens:
            file_metrics["skip_reason"] = "budget"
            continue
        working = trial
        cursor += 1
        file_metrics["injected_tokens"] = injected_tokens
    return working, metrics


async def load_state_file(sandbox: Any, path: str, *, job_id: str | None = None) -> dict[str, Any]:
    """Read one declared file. Never raises for a missing or unreadable file."""

    try:
        payload = await _read_state_payload(sandbox, path)
    except Exception as exc:
        logger.warning(
            "state file skipped job_id=%s path=%s validation=read_error detail=%s",
            job_id or "-",
            path,
            type(exc).__name__,
        )
        return {
            "path": path,
            "validation": "read_error",
            "file_mtime": None,
            "text": None,
            "error": type(exc).__name__,
        }
    if not isinstance(payload, dict):
        return evaluate_state_file(
            path=path, text=None, mtime=None, read_error="invalid_response", job_id=job_id,
        )
    error = payload.get("error")
    mtime = payload.get("mtime")
    coerced = mtime if isinstance(mtime, (int, float)) and not isinstance(mtime, bool) else None
    if error:
        return evaluate_state_file(
            path=path, text=None, mtime=coerced, read_error=str(error), job_id=job_id,
        )
    if payload.get("exists") is False:
        return evaluate_state_file(path=path, text=None, mtime=None, read_error=None, job_id=job_id)
    content = payload.get("content")
    if not isinstance(content, str):
        return evaluate_state_file(
            path=path, text=None, mtime=None, read_error="empty_response", job_id=job_id,
        )
    return evaluate_state_file(
        path=path, text=content, mtime=coerced, read_error=None, job_id=job_id,
    )


def _declared_entries(skill_md: Any, manifest: Any) -> list[str]:
    entries: list[str] = []
    front: dict[str, Any] = {}
    if isinstance(skill_md, str) and skill_md.strip():
        front = parse_skill_frontmatter(skill_md)
    if isinstance(front, dict):
        entries.extend(_as_strings(front.get("state_files")))
        metadata = front.get("metadata")
        if isinstance(metadata, dict):
            entries.extend(_as_strings(metadata.get("state_files")))
    if isinstance(manifest, dict):
        metadata = manifest.get("metadata")
        if isinstance(metadata, dict):
            entries.extend(_as_strings(metadata.get("state_files")))
        spec = manifest.get("spec")
        if isinstance(spec, dict):
            entries.extend(_as_strings(spec.get("state_files")))
    return entries


def _as_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


async def _read_state_payload(sandbox: Any, path: str) -> dict[str, Any]:
    reader = getattr(sandbox, "read_state_file", None)
    if reader is not None:
        payload = await reader(path, max_bytes=STATE_MAX_BYTES)
        if not isinstance(payload, dict):
            raise TypeError("read_state_file must return an object")
        return payload
    text_reader = getattr(sandbox, "read_text", None)
    if text_reader is None:
        return {"exists": False, "error": "unsupported"}
    try:
        text = await text_reader(path, offset=0, limit=STATE_MAX_BYTES + 1)
    except Exception as exc:
        if _is_missing(exc):
            return {"exists": False}
        raise
    if not isinstance(text, str):
        return {"exists": False, "error": "empty_response"}
    mtime = await _optional_mtime(sandbox, path)
    if len(text) > STATE_MAX_BYTES:
        return {"exists": True, "mtime": mtime, "error": "too_large"}
    return {"exists": True, "content": text, "mtime": mtime}


async def _optional_mtime(sandbox: Any, path: str) -> float | None:
    stater = getattr(sandbox, "file_mtime", None)
    if stater is None:
        return None
    try:
        value = await stater(path)
    except Exception:
        return None
    return _coerce_mtime(value)


def _log_skip(job_id: str | None, path: str, validation: str, detail: str) -> None:
    if validation == "missing" and job_id:
        key = (str(job_id), path)
        if key in _MISSING_WARNED:
            return
        _MISSING_WARNED.add(key)
    logger.warning(
        "state file skipped job_id=%s path=%s validation=%s detail=%s",
        job_id or "-",
        path,
        validation,
        detail,
    )


def _clip_to_cap(content: str, token_cap: int) -> str:
    if token_cap <= 0:
        return ""
    best = ""
    lo = 0
    hi = len(content)
    while lo <= hi:
        mid = (lo + hi) // 2
        piece = content[:mid].rstrip()
        if piece and _message_tokens(piece) <= token_cap:
            best = piece
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _message_tokens(content: str) -> int:
    from .agent_context import estimate_tokens

    return estimate_tokens([{"role": "user", "content": content}])


def _coerce_mtime(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _is_missing(exc: BaseException) -> bool:
    if isinstance(exc, (FileNotFoundError, KeyError)):
        return True
    code = getattr(exc, "code", "")
    if code == "SANDBOX_ARTIFACT_MISSING":
        return True
    text = str(exc).lower()
    return "does not exist" in text or "no such file" in text
