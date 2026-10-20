"""Opt-in projection that thins what the model sees on long sandbox runs.

Durable message history is not edited. Full tool bodies stay in the
conversation log, job events, and any workspace file the existing offload
path already wrote. This module only builds the slimmer copy
``project_context`` sends to the model.

The defaults match today's audit projection. ``SKILLGO_ORCHESTRATION_MODE=fast``
turns on an exchange cap, compaction of older tool bodies, and path+sha
pointers for ``references/deep/`` shelf entries. Workspace snapshot cadence
is intentionally not part of this policy.

``SKILLGO_STRUCTURED_COMPACTION_SUMMARY`` is independent of that mode and
defaults off. When it is on, dropped exchanges and compacted tool bodies
are reduced to one fixed, skill-agnostic summary. The caller attaches that
summary to the trailing state message so it stays outside the stable
cached prefix. When the flag is off, no summary is built.
"""
from __future__ import annotations

import hashlib
import json
import re
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
    # Independent of mode. Off keeps the model projection unchanged.
    structured_compaction_summary: bool = False


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
    raw_summary = getattr(source, "structured_compaction_summary", None)
    if raw_exchanges is not None and not isinstance(raw_exchanges, int):
        raise ValueError("SKILLGO_CONTEXT_MAX_EXCHANGES must be an integer")
    if raw_exchanges is not None and raw_exchanges < 0:
        raise ValueError("SKILLGO_CONTEXT_MAX_EXCHANGES must be >= 0")
    if raw_summary is None:
        structured_summary = False
    elif isinstance(raw_summary, bool):
        structured_summary = raw_summary
    else:
        raise ValueError("SKILLGO_STRUCTURED_COMPACTION_SUMMARY must be a boolean")
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
        structured_compaction_summary=structured_summary,
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


def recovery_exchange_cap(current: int | None) -> int | None:
    """Half of a kept-exchange limit, floored, for one overflow retry.

    The latest exchange is always one of the kept exchanges. Keep at least
    one older exchange as well, so the floor is 2 when a cap exists. A
    missing or non-positive cap stays uncapped.
    """

    if current is None or current <= 0:
        return None
    return max(current // 2, 2)


def recovery_thin_policy(policy: ThinContextPolicy | None = None) -> ThinContextPolicy:
    """Tighter projection used for one overflow retry.

    Reuses the same levers as a normal thin policy: an exchange cap, older
    tool compaction, and deep-reference pointers. Head and tail stay within
    the caller's sizes when those are already smaller. The exchange cap is
    half the current limit (see ``recovery_exchange_cap``), not a fixed 2.
    """

    current = policy or ThinContextPolicy()
    return ThinContextPolicy(
        mode=current.mode,
        structured_compaction_summary=current.structured_compaction_summary,
        max_exchanges=recovery_exchange_cap(current.max_exchanges),
        compact_older_tools=True,
        thin_reference_shelf=True,
        compact_head_chars=min(current.compact_head_chars, 240),
        compact_tail_chars=min(current.compact_tail_chars, 120),
        compact_min_chars=min(current.compact_min_chars, 400),
    )


def _latest_exchange_start(messages: list[dict[str, Any]]) -> int:
    """Index of the latest exchange, using the same grouping as projection.

    The first two messages are the pinned prefix. After that, an exchange
    starts at each assistant message and includes the tool results and notes
    that follow it. Messages before the first assistant are their own
    exchange. Returns ``len(messages)`` when there is no tail.
    """

    start: int | None = None
    for index, message in enumerate(messages):
        if index < 2 or not isinstance(message, dict):
            continue
        if message.get("role") == "assistant" or start is None:
            start = index
    return len(messages) if start is None else start


def compact_transcript_for_retry(
    messages: list[dict[str, Any]], policy: ThinContextPolicy,
) -> list[dict[str, Any]]:
    """Return a slimmer copy of a transcript for one model retry.

    The pinned prefix and the latest exchange stay as they are. The latest
    exchange is the last assistant tool-call message plus its tool results.
    Older tool and user bodies go through ``compact_tool_message``. The input
    list and its message dicts are not mutated; durable history stays intact
    when the caller keeps the original list.
    """

    latest = _latest_exchange_start(messages)
    compacted: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        if index < 2 or index >= latest or not policy.compact_older_tools:
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


# Fixed section order. Callers and tests depend on these headings.
COMPACTION_SUMMARY_HEADINGS: tuple[str, ...] = (
    "User goal:",
    "Constraints:",
    "Decisions made:",
    "Files created or modified:",
    "Current progress:",
    "Pending steps:",
    "Known errors:",
)

_OUTPUT_PATH_RE = re.compile(r"/workspace/(?:work|output)/[A-Za-z0-9_./\-~]+")
_ERROR_TOKENS = ("Traceback", "Exception", "ERROR", "Error", "error:")
_OBSERVATION_ACTIONS = frozenset({"read_file", "list_files", "read_files", "read_skill"})
_MAX_SUMMARY_ITEMS = 8
_MAX_FILE_ITEMS = 12
_MAX_ITEM_CHARS = 240
_MAX_GOAL_CHARS = 500
_SECTION_KEYS = (
    ("user_goal", "User goal:"),
    ("constraints", "Constraints:"),
    ("decisions_made", "Decisions made:"),
    ("files_created_or_modified", "Files created or modified:"),
    ("current_progress", "Current progress:"),
    ("pending_steps", "Pending steps:"),
    ("known_errors", "Known errors:"),
)


def _clip_fact(text: object, limit: int = _MAX_ITEM_CHARS) -> str:
    raw = text if isinstance(text, str) else str(text)
    collapsed = " ".join(raw.split())
    if len(collapsed) <= limit:
        return collapsed
    if limit <= 1:
        return collapsed[:limit]
    return collapsed[: limit - 1] + "…"


def _append_unique(bucket: list[str], value: object, limit: int) -> None:
    if len(bucket) >= limit or not isinstance(value, str):
        return
    text = _clip_fact(value)
    if not text or text in bucket:
        return
    bucket.append(text)


def _parse_json_object(value: object) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = json.loads(value)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _collect_output_paths(text: object, into: list[str]) -> None:
    if not isinstance(text, str) or "/workspace/" not in text:
        return
    for match in _OUTPUT_PATH_RE.findall(text):
        if "/tool-results/" in match:
            continue
        cleaned = match.rstrip(".,);:]\"'")
        if cleaned:
            into.append(cleaned)


def _error_excerpts(text: object) -> list[str]:
    if not isinstance(text, str) or not text:
        return []
    excerpts: list[str] = []
    for token in _ERROR_TOKENS:
        start = 0
        while True:
            index = text.find(token, start)
            if index < 0:
                break
            begin = max(0, index - 60)
            excerpts.append(_clip_fact(text[begin:index + 180]))
            if len(excerpts) >= 3:
                return excerpts
            start = index + len(token)
    return excerpts


def _actions_from_message(message: dict[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    calls = message.get("tool_calls")
    if isinstance(calls, list):
        for call in calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            arguments = _parse_json_object(function.get("arguments")) or {}
            if isinstance(name, str) and name:
                actions.append({"action": name, **{key: value for key, value in arguments.items() if key != "action"}})
    if message.get("role") == "assistant":
        body = _parse_json_object(message.get("content"))
        if isinstance(body, dict) and isinstance(body.get("action"), str):
            actions.append(body)
    return actions


def _assistant_note(message: dict[str, Any]) -> str | None:
    if message.get("role") != "assistant":
        return None
    content = message.get("content")
    if not isinstance(content, str):
        return None
    text = content.strip()
    if not text or text[0] in "{[":
        return None
    return _clip_fact(text, 400)


def _payloads_from_message(message: dict[str, Any]) -> list[tuple[object, dict[str, Any]]]:
    body = _parse_json_object(message.get("content"))
    if not isinstance(body, dict):
        return []
    found: list[tuple[object, dict[str, Any]]] = []

    def add(tool: object, payload: object) -> None:
        if isinstance(payload, dict) and not _protected_reference(payload):
            found.append((tool, payload))

    tool = body.get("tool_result")
    payload = body.get("payload")
    if isinstance(payload, dict):
        add(tool, payload)
        results = payload.get("results")
        if isinstance(results, list):
            for item in results:
                if isinstance(item, dict):
                    add(item.get("tool") or tool, item.get("payload"))
    return found


def _absorb_paths(paths: list[str], files: list[str]) -> None:
    for path in paths:
        _append_unique(files, path, _MAX_FILE_ITEMS)


def _absorb_action(
    action: dict[str, Any],
    *,
    decisions: list[str],
    files: list[str],
    discovered_goals: list[str],
) -> None:
    name = str(action.get("action") or "")
    reason = action.get("reason")
    if isinstance(reason, str) and reason.strip() and name not in _OBSERVATION_ACTIONS:
        label = f"{name}: {reason.strip()}" if name else reason.strip()
        _append_unique(decisions, label, _MAX_SUMMARY_ITEMS)
    if name == "write_file" and isinstance(action.get("path"), str):
        _append_unique(files, action["path"].strip(), _MAX_FILE_ITEMS)
    if name == "update_plan":
        plan_goal = action.get("goal")
        if isinstance(plan_goal, str) and plan_goal.strip():
            discovered_goals.append(plan_goal.strip())
            _append_unique(decisions, f"Set goal: {plan_goal.strip()}", _MAX_SUMMARY_ITEMS)
        steps = action.get("steps")
        if isinstance(steps, list):
            for step in steps:
                if not isinstance(step, dict):
                    continue
                title = step.get("title") or step.get("id")
                if isinstance(title, str) and title.strip():
                    _append_unique(decisions, f"Planned: {title.strip()}", _MAX_SUMMARY_ITEMS)
                refs = step.get("output_refs")
                if isinstance(refs, list):
                    for ref in refs:
                        if isinstance(ref, str) and ref.strip():
                            _append_unique(files, ref.strip(), _MAX_FILE_ITEMS)
    artifacts = action.get("artifacts")
    if isinstance(artifacts, list):
        for artifact in artifacts:
            if isinstance(artifact, str) and artifact.startswith("/"):
                _append_unique(files, artifact.strip(), _MAX_FILE_ITEMS)
    discovered: list[str] = []
    if name == "command" and isinstance(action.get("argv"), list):
        _collect_output_paths(
            " ".join(item for item in action["argv"] if isinstance(item, str)),
            discovered,
        )
    if name == "run_python" and isinstance(action.get("code"), str):
        _collect_output_paths(action["code"], discovered)
    _absorb_paths(discovered, files)


def _absorb_payload(tool: object, payload: dict[str, Any], *, files: list[str], errors: list[str]) -> None:
    if tool == "write_file" and isinstance(payload.get("path"), str):
        _append_unique(files, payload["path"].strip(), _MAX_FILE_ITEMS)
    elif (
        payload.get("ok") is True
        and isinstance(payload.get("bytes"), int)
        and isinstance(payload.get("path"), str)
    ):
        _append_unique(files, payload["path"].strip(), _MAX_FILE_ITEMS)
    artifacts = payload.get("artifacts")
    if isinstance(artifacts, list):
        for artifact in artifacts:
            if isinstance(artifact, str) and artifact.startswith("/"):
                _append_unique(files, artifact.strip(), _MAX_FILE_ITEMS)
    discovered: list[str] = []
    for field in ("stdout", "stderr"):
        _collect_output_paths(payload.get(field), discovered)
    _absorb_paths(discovered, files)
    failed = payload.get("ok") is False or (
        isinstance(payload.get("exit_code"), int) and payload.get("exit_code") != 0
    )
    if not failed:
        return
    exit_code = payload.get("exit_code")
    if isinstance(exit_code, int) and exit_code != 0:
        _append_unique(errors, f"exit {exit_code}", _MAX_SUMMARY_ITEMS)
    parts: list[str] = []
    code = payload.get("error_code")
    message = payload.get("message")
    if isinstance(code, str) and code.strip():
        parts.append(code.strip())
    if isinstance(message, str) and message.strip():
        parts.append(message.strip())
    if parts:
        _append_unique(errors, ": ".join(parts), _MAX_SUMMARY_ITEMS)
    for field in ("stdout", "stderr"):
        for excerpt in _error_excerpts(payload.get(field)):
            _append_unique(errors, excerpt, _MAX_SUMMARY_ITEMS)


def render_structured_compaction_summary(facts: dict[str, Any]) -> str:
    """Render the fixed compaction summary. Every section is always present."""

    blocks: list[str] = []
    for key, heading in _SECTION_KEYS:
        blocks.append(heading)
        value = facts.get(key)
        if key == "user_goal":
            text = value.strip() if isinstance(value, str) else ""
            blocks.append(text or "none")
        else:
            items = [item.strip() for item in value if isinstance(item, str) and item.strip()] if isinstance(value, list) else []
            if not items:
                blocks.append("none")
            else:
                blocks.extend(f"- {item}" for item in items)
        blocks.append("")
    return "\n".join(blocks).rstrip() + "\n"


def build_structured_compaction_summary(
    omitted_messages: list[dict[str, Any]],
    *,
    checkpoint: dict[str, Any],
    pinned_messages: list[dict[str, Any]],
    omitted_exchange_count: int,
    compacted_tool_count: int,
) -> str:
    """Summarize history that the model will no longer see in full.

    Facts come from the omitted or compacted messages plus the execution
    checkpoint (goal, constraints, step status, unresolved failures). Reference
    bodies are skipped. The result is a fixed template, not a model call.
    """

    goal = ""
    constraints: list[str] = []
    decisions: list[str] = []
    files: list[str] = []
    progress: list[str] = [
        (
            f"Omitted exchanges: {max(0, int(omitted_exchange_count))}. "
            f"Compacted tool results: {max(0, int(compacted_tool_count))}."
        )
    ]
    pending: list[str] = []
    errors: list[str] = []
    discovered_goals: list[str] = []

    plan = checkpoint.get("plan") if isinstance(checkpoint.get("plan"), dict) else {}
    if isinstance(plan.get("goal"), str) and plan["goal"].strip():
        goal = _clip_fact(plan["goal"], _MAX_GOAL_CHARS)
    for source in (
        checkpoint.get("requirements"),
        checkpoint.get("constraints"),
        plan.get("success_criteria"),
        plan.get("constraints"),
    ):
        if isinstance(source, list):
            for item in source:
                if isinstance(item, str):
                    _append_unique(constraints, item, _MAX_SUMMARY_ITEMS)
        elif isinstance(source, str):
            _append_unique(constraints, source, _MAX_SUMMARY_ITEMS)
    steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
    for step in steps:
        if not isinstance(step, dict):
            continue
        title = step.get("title") or step.get("id")
        if not isinstance(title, str) or not title.strip():
            continue
        status = str(step.get("status") or "pending").strip() or "pending"
        if status == "completed":
            _append_unique(progress, f"Completed: {title.strip()}", _MAX_SUMMARY_ITEMS)
        elif status in {"pending", "in_progress", "blocked"}:
            _append_unique(pending, f"{title.strip()} ({status})", _MAX_SUMMARY_ITEMS)
        refs = step.get("output_refs")
        if isinstance(refs, list):
            for ref in refs:
                if isinstance(ref, str) and ref.strip():
                    _append_unique(files, ref.strip(), _MAX_FILE_ITEMS)

    for message in omitted_messages:
        if not isinstance(message, dict):
            continue
        note = _assistant_note(message)
        if note:
            _append_unique(decisions, note, _MAX_SUMMARY_ITEMS)
        for action in _actions_from_message(message):
            _absorb_action(action, decisions=decisions, files=files, discovered_goals=discovered_goals)
        for tool, payload in _payloads_from_message(message):
            _absorb_payload(tool, payload, files=files, errors=errors)

    if not goal and discovered_goals:
        goal = _clip_fact(discovered_goals[0], _MAX_GOAL_CHARS)
    if not goal:
        for message in pinned_messages:
            if message.get("role") == "user" and isinstance(message.get("content"), str) and message["content"].strip():
                goal = _clip_fact(message["content"], _MAX_GOAL_CHARS)
                break

    failures = checkpoint.get("unresolved_failures")
    if isinstance(failures, list):
        for item in failures:
            if not isinstance(item, dict):
                continue
            tool = item.get("tool") or "tool"
            diagnostic = item.get("diagnostic")
            if isinstance(diagnostic, str) and diagnostic.strip():
                _append_unique(errors, f"{tool}: {_clip_fact(diagnostic)}", _MAX_SUMMARY_ITEMS)

    return render_structured_compaction_summary({
        "user_goal": goal,
        "constraints": constraints,
        "decisions_made": decisions,
        "files_created_or_modified": files,
        "current_progress": progress,
        "pending_steps": pending,
        "known_errors": errors,
    })
