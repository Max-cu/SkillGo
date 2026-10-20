"""Inject a skill-written photo plan into fast-mode model context.

Fast mode compacts old exchanges, so a photo list that only existed inside an
earlier tool result can disappear. A skill can instead keep the authoritative
copy at ``/workspace/_state/photo_plan.json``. When ``SKILLGO_STATE_INJECT`` is
on, each fast-mode round reads that file and places a bounded rendering after
the stable history prefix and before the mutable execution-state message.

The durable transcript is not modified. A missing or invalid file is logged
and skipped; it does not fail the turn. Audit mode does not read or inject it.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Missing-file warnings are once per job. Cleared with the job timing entry.
_MISSING_PLAN_WARNED: set[str] = set()

PHOTO_PLAN_PATH = "/workspace/_state/photo_plan.json"
PHOTO_PLAN_TOKEN_CAP = 1024
PHOTO_PLAN_MAX_BYTES = 128 * 1024
_MAX_PHOTOS = 400
_MAX_PAGES = 400
_MAX_IDS_PER_PAGE = 64
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_TRUNCATION_NOTE = (
    "[truncated to token cap; re-read /workspace/_state/photo_plan.json for the full plan]"
)


def validate_photo_plan(value: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Return a normalized plan, or ``(None, error)`` when the schema is wrong.

    Required shape::

        {
          "photos": [{"image_id": "image_001", "path": "/workspace/...", "source": "upload"}],
          "assignments": {"1": ["image_001"]},
          "version": 1,            # optional
          "updated_at": "..."      # optional
        }

    The same image id on two pages is valid here. Callers surface that as a
    warning in the injected block instead of rejecting the file.
    """

    if not isinstance(value, dict):
        return None, "photo plan must be an object"
    version = value.get("version", None)
    if version is not None and (isinstance(version, bool) or not isinstance(version, int)):
        return None, "version must be an integer"
    if isinstance(version, int) and not 0 <= version <= 1_000_000:
        return None, "version is out of range"
    updated_at = value.get("updated_at", None)
    if updated_at is not None:
        updated_at = _plain_text(updated_at, max_len=64)
        if updated_at is None:
            return None, "updated_at must be a string"
    photos = value.get("photos")
    if not isinstance(photos, list):
        return None, "photos must be a list"
    if len(photos) > _MAX_PHOTOS:
        return None, "photos exceeds the schema limit"
    normalized_photos: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(photos):
        if not isinstance(item, dict):
            return None, f"photos[{index}] must be an object"
        image_id = _image_id(item.get("image_id"))
        path = _path(item.get("path"))
        source = _plain_text(item.get("source"), max_len=80)
        if image_id is None or path is None or source is None:
            return None, f"photos[{index}] requires image_id, path, and source"
        if image_id in seen_ids:
            return None, f"photos[{index}].image_id is duplicated"
        seen_ids.add(image_id)
        normalized_photos.append({"image_id": image_id, "path": path, "source": source})
    assignments = value.get("assignments")
    if not isinstance(assignments, dict):
        return None, "assignments must be an object"
    if len(assignments) > _MAX_PAGES:
        return None, "assignments exceeds the schema limit"
    normalized_assignments: dict[str, list[str]] = {}
    for raw_page, raw_ids in assignments.items():
        page = _plain_text(raw_page, max_len=40)
        if page is None:
            return None, "assignment page labels must be strings"
        if page in normalized_assignments:
            return None, f"assignments.{page} is duplicated"
        if not isinstance(raw_ids, list):
            return None, f"assignments.{page} must be a list of image ids"
        if len(raw_ids) > _MAX_IDS_PER_PAGE:
            return None, f"assignments.{page} exceeds the schema limit"
        image_ids: list[str] = []
        for raw_id in raw_ids:
            image_id = _image_id(raw_id)
            if image_id is None:
                return None, f"assignments.{page} must be a list of image ids"
            image_ids.append(image_id)
        normalized_assignments[page] = image_ids
    document: dict[str, Any] = {
        "version": version,
        "updated_at": updated_at,
        "photos": normalized_photos,
        "assignments": normalized_assignments,
    }
    return document, None


def duplicate_image_ids(document: dict[str, Any]) -> list[str]:
    """Image ids that appear on more than one assignment slot."""

    counts: dict[str, int] = {}
    order: list[str] = []
    for image_ids in document.get("assignments", {}).values():
        for image_id in image_ids:
            if image_id not in counts:
                order.append(image_id)
            counts[image_id] = counts.get(image_id, 0) + 1
    return [image_id for image_id in order if counts[image_id] > 1]


def assignment_warnings(document: dict[str, Any]) -> list[str]:
    """Human-readable stale/duplicate warnings. The plan itself stays valid."""

    locations: dict[str, list[str]] = {}
    for page, image_ids in document.get("assignments", {}).items():
        for image_id in image_ids:
            locations.setdefault(image_id, []).append(str(page))
    known = {item["image_id"] for item in document.get("photos", []) if isinstance(item, dict)}
    warnings: list[str] = []
    for image_id, pages in locations.items():
        unique_pages = list(dict.fromkeys(pages))
        if len(pages) > 1:
            warnings.append(
                "WARNING: stale/duplicate assignment: "
                f"{image_id} is on pages {', '.join(unique_pages)}. "
                "Place this image_id on one page only."
            )
        if image_id not in known:
            warnings.append(
                f"WARNING: stale assignment: {image_id} on page {', '.join(unique_pages)} "
                "is not in the photo list."
            )
    return warnings


def annotate_photo_plan_age(snapshot: dict[str, Any], job_started_at: float | None) -> dict[str, Any]:
    """Flag a plan whose file mtime is earlier than the job start."""

    mtime = _coerce_mtime(snapshot.get("file_mtime"))
    started = _coerce_mtime(job_started_at)
    snapshot["stale"] = bool(mtime is not None and started is not None and mtime < started)
    return snapshot


def forget_photo_plan_warning(job_id: str) -> None:
    _MISSING_PLAN_WARNED.discard(str(job_id))


def render_photo_plan(
    document: dict[str, Any],
    *,
    mtime: float | None,
    token_cap: int,
    stale: bool = False,
) -> str:
    """Compact text that stays within ``token_cap`` estimated tokens."""

    header = _header_lines(document, mtime)
    warnings = assignment_warnings(document)
    if stale:
        warnings = [
            "WARNING: stale photo plan: file mtime is earlier than this job's start. "
            f"Rewrite {PHOTO_PLAN_PATH} for this run before placing photos.",
            *warnings,
        ]
    photo_lines = [
        f"- {item['image_id']} {item['path']} source={item['source']}"
        for item in document.get("photos", [])
    ]
    page_lines = [
        f"- {page}: {', '.join(image_ids) if image_ids else '(none)'}"
        for page, image_ids in document.get("assignments", {}).items()
    ]

    def assemble(photos: list[str], pages: list[str], *, truncated: bool) -> str:
        lines = list(header)
        if truncated:
            lines.insert(0, _TRUNCATION_NOTE)
        if warnings:
            lines.extend(warnings)
        lines.append("photos:")
        lines.extend(photos or ["- (none)"])
        lines.append("pages:")
        lines.extend(pages or ["- (none)"])
        return "\n".join(lines)

    full = assemble(photo_lines, page_lines, truncated=False)
    if _message_tokens(full) <= token_cap:
        return full
    photos = list(photo_lines)
    pages = list(page_lines)
    while photos or pages:
        if pages:
            pages.pop()
        elif photos:
            photos.pop()
        text = assemble(photos, pages, truncated=True)
        if _message_tokens(text) <= token_cap:
            return text
    text = assemble([], [], truncated=True)
    if _message_tokens(text) <= token_cap:
        return text
    return _clip_to_cap(text, token_cap)


def evaluate_photo_plan(
    *,
    text: str | None,
    mtime: float | None,
    read_error: str | None,
    job_id: str | None = None,
) -> dict[str, Any]:
    """Validate raw file text. Logs WARNING and skips when it cannot be injected."""

    mtime = _coerce_mtime(mtime)
    base: dict[str, Any] = {
        "validation": "missing",
        "file_mtime": mtime,
        "document": None,
        "duplicates": [],
    }
    if read_error:
        detail = str(read_error)[:120]
        _log_skip(job_id, "read_error", detail)
        return {**base, "validation": "read_error", "error": detail}
    if text is None:
        _log_skip(job_id, "missing", "file not found")
        return base
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        _log_skip(job_id, "invalid", "invalid JSON")
        return {**base, "validation": "invalid", "error": "invalid JSON"}
    document, error = validate_photo_plan(parsed)
    if error or document is None:
        detail = error or "invalid schema"
        _log_skip(job_id, "invalid", detail)
        return {**base, "validation": "invalid", "error": detail}
    duplicates = duplicate_image_ids(document)
    return {
        "validation": "ok",
        "file_mtime": mtime,
        "document": document,
        "duplicates": duplicates,
    }


def apply_photo_plan(
    projected: list[dict[str, Any]],
    *,
    history_len: int,
    snapshot: dict[str, Any] | None,
    token_cap: int,
    max_tokens: int,
    tool_tokens: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Insert the plan after ``history_len`` messages when it fits the budget.

    ``history_len`` is the stable prefix. The message already at that index is
    the mutable execution-state user message, so the plan sits after history
    and before that last user message.
    """

    from .agent_context import estimate_tokens

    snapshot = snapshot or {}
    validation = str(snapshot.get("validation") or "missing")
    metrics: dict[str, Any] = {
        "validation": validation,
        "injected_tokens": 0,
        "file_mtime": _coerce_mtime(snapshot.get("file_mtime")),
        "stale": bool(snapshot.get("stale")),
    }
    document = snapshot.get("document")
    if validation != "ok" or not isinstance(document, dict):
        if snapshot.get("error"):
            metrics["error"] = str(snapshot["error"])[:120]
        return projected, metrics
    duplicates = snapshot.get("duplicates")
    metrics["duplicates"] = list(duplicates) if isinstance(duplicates, list) else duplicate_image_ids(document)
    content = render_photo_plan(
        document, mtime=metrics["file_mtime"], token_cap=token_cap, stale=metrics["stale"],
    )
    message = {"role": "user", "content": content}
    injected_tokens = estimate_tokens([message])
    if injected_tokens > token_cap:
        content = _clip_to_cap(content, token_cap)
        message = {"role": "user", "content": content}
        injected_tokens = estimate_tokens([message])
    if injected_tokens > token_cap or not content:
        metrics["skip_reason"] = "token_cap"
        return projected, metrics
    trial = [*projected[:history_len], message, *projected[history_len:]]
    if estimate_tokens(trial) + tool_tokens > max_tokens:
        metrics["skip_reason"] = "budget"
        return projected, metrics
    metrics["injected_tokens"] = injected_tokens
    return trial, metrics


async def load_photo_plan(sandbox: Any, *, job_id: str | None = None) -> dict[str, Any]:
    """Read the workspace file. Never raises for a missing or unreadable plan."""

    try:
        payload = await _read_state_payload(sandbox)
    except Exception as exc:
        logger.warning(
            "photo plan skipped job_id=%s path=%s validation=read_error detail=%s",
            job_id or "-",
            PHOTO_PLAN_PATH,
            type(exc).__name__,
        )
        return {
            "validation": "read_error",
            "file_mtime": None,
            "document": None,
            "duplicates": [],
            "error": type(exc).__name__,
        }
    if not isinstance(payload, dict):
        return evaluate_photo_plan(text=None, mtime=None, read_error="invalid_response", job_id=job_id)
    error = payload.get("error")
    mtime = payload.get("mtime")
    if error:
        return evaluate_photo_plan(
            text=None,
            mtime=mtime if isinstance(mtime, (int, float)) and not isinstance(mtime, bool) else None,
            read_error=str(error),
            job_id=job_id,
        )
    if payload.get("exists") is False:
        return evaluate_photo_plan(text=None, mtime=None, read_error=None, job_id=job_id)
    content = payload.get("content")
    if not isinstance(content, str):
        return evaluate_photo_plan(text=None, mtime=None, read_error="empty_response", job_id=job_id)
    return evaluate_photo_plan(
        text=content,
        mtime=mtime if isinstance(mtime, (int, float)) and not isinstance(mtime, bool) else None,
        read_error=None,
        job_id=job_id,
    )


async def _read_state_payload(sandbox: Any) -> dict[str, Any]:
    reader = getattr(sandbox, "read_state_file", None)
    if reader is not None:
        payload = await reader(PHOTO_PLAN_PATH, max_bytes=PHOTO_PLAN_MAX_BYTES)
        if not isinstance(payload, dict):
            raise TypeError("read_state_file must return an object")
        return payload
    text_reader = getattr(sandbox, "read_text", None)
    if text_reader is None:
        return {"exists": False, "error": "unsupported"}
    try:
        text = await text_reader(PHOTO_PLAN_PATH, offset=0, limit=PHOTO_PLAN_MAX_BYTES + 1)
    except Exception as exc:
        if _is_missing(exc):
            return {"exists": False}
        raise
    if not isinstance(text, str):
        return {"exists": False, "error": "empty_response"}
    mtime = await _optional_mtime(sandbox)
    if len(text) > PHOTO_PLAN_MAX_BYTES:
        return {"exists": True, "mtime": mtime, "error": "too_large"}
    return {"exists": True, "content": text, "mtime": mtime}


async def _optional_mtime(sandbox: Any) -> float | None:
    stater = getattr(sandbox, "file_mtime", None)
    if stater is None:
        return None
    try:
        value = await stater(PHOTO_PLAN_PATH)
    except Exception:
        return None
    return _coerce_mtime(value)


def _log_skip(job_id: str | None, validation: str, detail: str) -> None:
    if validation == "missing" and job_id:
        if job_id in _MISSING_PLAN_WARNED:
            return
        _MISSING_PLAN_WARNED.add(job_id)
    logger.warning(
        "photo plan skipped job_id=%s path=%s validation=%s detail=%s",
        job_id or "-",
        PHOTO_PLAN_PATH,
        validation,
        detail,
    )


def _header_lines(document: dict[str, Any], mtime: float | None) -> list[str]:
    lines = [
        f"Photo plan from {PHOTO_PLAN_PATH}",
        "Place each image_id on one page only. Do not reuse a photo already listed.",
    ]
    if document.get("version") is not None:
        lines.append(f"version={document['version']}")
    if document.get("updated_at"):
        lines.append(f"updated_at={document['updated_at']}")
    if mtime is not None:
        lines.append(f"mtime={mtime:.3f}")
    return lines


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

    return estimate_tokens({"role": "user", "content": content})


def _image_id(value: Any) -> str | None:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        return None
    return value


def _plain_text(value: Any, *, max_len: int) -> str | None:
    if not isinstance(value, str):
        return None
    if any(ord(ch) < 32 for ch in value):
        return None
    text = value.strip()
    if not text or len(text) > max_len:
        return None
    return text


def _path(value: Any) -> str | None:
    text = _plain_text(value, max_len=500)
    if text is None:
        return None
    parts = text.replace("\\", "/").split("/")
    if any(part == ".." for part in parts):
        return None
    return text


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
