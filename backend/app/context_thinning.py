"""Opt-in projection that thins what the model sees on long sandbox runs.

Durable message history is not edited. Full tool bodies stay in the
conversation log, job events, and any workspace file the existing offload
path already wrote. This module only builds the slimmer copy
``project_context`` sends to the model.

The defaults match today's audit projection. ``SKILLGO_ORCHESTRATION_MODE=fast``
turns on an exchange cap, compaction of older tool bodies, and path+sha
pointers for ``references/deep/`` shelf entries. Workspace snapshot cadence
is intentionally not part of this policy.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .config import settings


_SHRINK_KEYS = frozenset({"stdout", "stderr", "content", "excerpt", "text"})
_DEEP_REFERENCE_MARK = "/references/deep/"


@dataclass(frozen=True)
class ThinContextPolicy:
    """How ``project_context`` may shrink the model-facing transcript.

    ``max_exchanges`` is ``None`` when there is no cap beyond the token budget.
    Head/tail sizes are characters, matching compact-note layout.
    """

    mode: str = "audit"
    max_exchanges: int | None = None
    compact_older_tools: bool = False
    thin_reference_shelf: bool = False
    compact_head_chars: int = 400
    compact_tail_chars: int = 200
    compact_min_chars: int = 800


def resolve_thin_context_policy(source: Any | None = None) -> ThinContextPolicy:
    """Resolve env/settings into a projection policy.

    Unset knobs pick the mode default. An explicit ``0`` exchange cap disables
    the cap. Explicit booleans override the mode, including turning a fast-mode
    lever back off for an A/B.
    """

    source = settings if source is None else source
    mode = str(getattr(source, "orchestration_mode", "audit") or "audit").strip().lower()
    if mode not in {"audit", "fast"}:
        raise ValueError("SKILLGO_ORCHESTRATION_MODE must be 'audit' or 'fast'")
    raw_exchanges = getattr(source, "context_max_exchanges", None)
    raw_compact = getattr(source, "compact_older_tools", None)
    raw_shelf = getattr(source, "thin_reference_shelf", None)
    if raw_exchanges is not None and not isinstance(raw_exchanges, int):
        raise ValueError("SKILLGO_CONTEXT_MAX_EXCHANGES must be an integer")
    if raw_exchanges is not None and raw_exchanges < 0:
        raise ValueError("SKILLGO_CONTEXT_MAX_EXCHANGES must be >= 0")
    if mode == "fast":
        max_exchanges = 4 if raw_exchanges is None else (None if raw_exchanges <= 0 else raw_exchanges)
        compact = True if raw_compact is None else bool(raw_compact)
        thin_shelf = True if raw_shelf is None else bool(raw_shelf)
    else:
        max_exchanges = None if raw_exchanges is None or raw_exchanges <= 0 else raw_exchanges
        compact = bool(raw_compact)
        thin_shelf = bool(raw_shelf)
    return ThinContextPolicy(
        mode=mode,
        max_exchanges=max_exchanges,
        compact_older_tools=compact,
        thin_reference_shelf=thin_shelf,
    )


def is_deep_reference_path(path: object) -> bool:
    if not isinstance(path, str):
        return False
    return _DEEP_REFERENCE_MARK in path.replace("\\", "/")


def project_shelf_entry(entry: Any, policy: ThinContextPolicy) -> Any:
    """Replace a deep reference's full text with a path+sha pointer.

    Other shelf entries, including ordinary Skill specs, stay intact. The
    checkpoint object is not mutated.
    """

    if not policy.thin_reference_shelf or not isinstance(entry, dict):
        return entry
    path = entry.get("path")
    text = entry.get("content")
    if not is_deep_reference_path(path) or not isinstance(text, str):
        return entry
    if len(text) <= policy.compact_head_chars:
        return entry
    digest = entry.get("sha256")
    if not isinstance(digest, str) or not digest:
        digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    return {
        "path": path,
        "sha256": digest,
        "bytes": entry.get("bytes", len(text.encode("utf-8", errors="replace"))),
        "chars": entry.get("chars", len(text)),
        "content_omitted": True,
        "summary": text[: policy.compact_head_chars],
        "pointer": (
            "Full text stays in the sandbox file and in durable history. "
            "Re-read this path only if the current turn needs a passage outside the summary."
        ),
    }


def compact_tool_message(message: dict[str, Any], policy: ThinContextPolicy) -> dict[str, Any]:
    """Return a slimmer copy of an older tool result, or the same object.

    Assistant messages, short results, and pinned reference payloads are left
    untouched so a spec that is no longer duplicated on the shelf is still
    present in the retained exchange. The caller must not use the result to
    replace the durable transcript.
    """

    if not policy.compact_older_tools:
        return message
    if message.get("role") not in {"tool", "user"}:
        return message
    content = message.get("content")
    if not isinstance(content, str) or len(content) < policy.compact_min_chars:
        return message
    try:
        body = json.loads(content)
    except ValueError:
        body = None
    if isinstance(body, dict) and "payload" in body:
        payload = body.get("payload")
        if isinstance(payload, str):
            clipped = _clip_text(payload, policy, location="durable history")
            if clipped is None or len(clipped) >= len(payload):
                return message
            new_payload: Any = clipped
            changed = True
        elif isinstance(payload, (dict, list)):
            new_payload, changed = _compact_value(payload, policy)
        else:
            return message
        if not changed:
            return message
        new_body = {**body, "payload": new_payload, "context_compacted": True}
        new_content = json.dumps(new_body, ensure_ascii=False)
        if len(new_content) >= len(content):
            return message
        return {**message, "content": new_content}
    clipped = _clip_text(content, policy, location="durable history")
    if clipped is None or len(clipped) >= len(content):
        return message
    return {**message, "content": clipped}


def _protected_reference(payload: dict[str, Any]) -> bool:
    return (
        payload.get("reference") is True
        and (payload.get("retained") is True or payload.get("complete") is True)
        and isinstance(payload.get("content"), str)
    )


def _clip_text(text: str, policy: ThinContextPolicy, *, location: str, sha: str | None = None) -> str | None:
    if len(text) < policy.compact_min_chars:
        return None
    head = policy.compact_head_chars
    tail = policy.compact_tail_chars
    if head < 0 or tail < 0 or len(text) <= head + tail + 80:
        return None
    digest = sha or hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    omitted = len(text) - head - tail
    marker = (
        f"\n[... {omitted} chars omitted from model context; sha256={digest}; "
        f"full text remains in {location}; re-read that path if a passage "
        f"outside this excerpt is required ...]\n"
    )
    return text[:head] + marker + text[-tail:]


def recovery_thin_policy(policy: ThinContextPolicy | None = None) -> ThinContextPolicy:
    """Tighter projection used for one overflow retry.

    Reuses the same levers as a normal thin policy: an exchange cap, older
    tool compaction, and deep-reference pointers. Head and tail stay within
    the caller's sizes when those are already smaller.
    """

    current = policy or ThinContextPolicy()
    exchange_cap = current.max_exchanges if current.max_exchanges else 2
    return ThinContextPolicy(
        mode=current.mode,
        max_exchanges=min(exchange_cap, 2),
        compact_older_tools=True,
        thin_reference_shelf=True,
        compact_head_chars=min(current.compact_head_chars, 240),
        compact_tail_chars=min(current.compact_tail_chars, 120),
        compact_min_chars=min(current.compact_min_chars, 400),
    )


def compact_transcript_for_retry(
    messages: list[dict[str, Any]], policy: ThinContextPolicy,
) -> list[dict[str, Any]]:
    """Return a slimmer copy of a transcript for one model retry.

    The first two messages stay as they are. Later tool and user bodies go
    through ``compact_tool_message``, including the latest exchange. The
    input list and its message dicts are not mutated; durable history stays
    intact when the caller keeps the original list.
    """

    compacted: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        if index < 2 or not policy.compact_older_tools:
            compacted.append(message)
            continue
        updated = compact_tool_message(message, policy)
        compacted.append(updated if updated is not message else message)
    return compacted


def _compact_value(value: Any, policy: ThinContextPolicy, *, location_hint: str = "durable history") -> tuple[Any, bool]:
    if isinstance(value, dict):
        if _protected_reference(value):
            return value, False
        path = value.get("path") if isinstance(value.get("path"), str) else None
        full_path = value.get("full_result_path") if isinstance(value.get("full_result_path"), str) else None
        sha = value.get("sha256") if isinstance(value.get("sha256"), str) else None
        location = full_path or path or location_hint
        changed = False
        new = dict(value)
        for key, item in value.items():
            if key in _SHRINK_KEYS and isinstance(item, str):
                clipped = _clip_text(
                    item,
                    policy,
                    location=location,
                    sha=sha if key == "content" else None,
                )
                if clipped is not None and len(clipped) < len(item):
                    new[key] = clipped
                    changed = True
                continue
            child, child_changed = _compact_value(item, policy, location_hint=location)
            if child_changed:
                new[key] = child
                changed = True
        return (new if changed else value), changed
    if isinstance(value, list):
        changed = False
        items = []
        for item in value:
            child, child_changed = _compact_value(item, policy, location_hint=location_hint)
            items.append(child)
            changed = changed or child_changed
        return (items if changed else value), changed
    return value, False
