"""Opt-in projection that thins what the model sees on long sandbox runs.

Durable message history is not edited. Full tool bodies stay in the
conversation log, job events, and any workspace file the existing offload
path already wrote. This module only builds the slimmer copy
``project_context`` sends to the model.

The defaults match today's audit projection. ``SKILLGO_ORCHESTRATION_MODE=fast``
turns on an exchange cap, compaction of older tool bodies, and path+sha
pointers for ``references/deep/`` shelf entries. Fast mode can also pin
selected tool results so they stay verbatim and outside the exchange cap.
Workspace snapshot cadence is intentionally not part of this policy.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from .config import settings


_SHRINK_KEYS = frozenset({"stdout", "stderr", "content", "excerpt", "text"})
_DEEP_REFERENCE_MARK = "/references/deep/"
# Command text must contain these script filenames. Section labels such as
# §0d / §0f are not pins. count_photos.py is the highest ceiling priority.
DEFAULT_FAST_PIN_TOOLS: tuple[str, ...] = (
    "count_photos.py",
    "fetch_web_images.py",
    "inject_image_credits.py",
    "icon_sync.py",
)
# Fallback when the command text does not name the script. The whole tool
# result is pinned, including stderr (icon_sync writes its result there).
DEFAULT_FAST_PIN_PATTERNS: tuple[str, ...] = (
    r"✅ 可用照片: \d+ 张",
    r"\[✓\] image_\d+",
    r"联网补图 \d+ 张",
    r"\[\[image_\d+\.\w+\]\]",
    r"_web_images\.json",
)
# File reads. design_spec.md is pinnable even though it lives outside assets/sources/.
# These lose to command pins when the token ceiling is tight, and re-reads dedupe.
DEFAULT_FAST_PIN_READS: tuple[str, ...] = (
    "assets/sources/文本.md",
    "_web_images.json",
    "assets/design_spec.md",
)
DEFAULT_FAST_PIN_TOKEN_CEILING = 8192
_COMMAND_STRING_KEYS = ("command", "cmd", "script", "tool", "action", "name")
_READ_TOOL_NAMES = frozenset({"read_file", "read_files"})
_READ_PRIORITY = 2
# Regex-only hits never share a command or file-read key. Lowest ceiling
# priority, so a later ``ls`` or SVG write cannot displace count_photos.
_PATTERN_PRIORITY = 3


@dataclass(frozen=True)
class ThinContextPolicy:
    """How ``project_context`` may shrink the model-facing transcript.

    ``max_exchanges`` is ``None`` when there is no cap beyond the token budget.
    Head/tail sizes are characters, matching the Workbuddy compact notes.
    """

    mode: str = "audit"
    max_exchanges: int | None = None
    compact_older_tools: bool = False
    thin_reference_shelf: bool = False
    compact_head_chars: int = 400
    compact_tail_chars: int = 200
    compact_min_chars: int = 800
    # Applied only when mode == "fast". Audit leaves these empty.
    # pin_tools are command-text substrings (script filenames). An optional
    # parallel priority tuple uses smaller numbers first under the ceiling.
    pin_tools: tuple[str, ...] = ()
    pin_tool_priorities: tuple[int, ...] = ()
    pin_patterns: tuple[str, ...] = ()
    pin_reads: tuple[str, ...] = ()
    pin_token_ceiling: int = DEFAULT_FAST_PIN_TOKEN_CEILING


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
    pin_tools, pin_priorities, pin_patterns, pin_reads, pin_ceiling = _resolve_fast_pins(source, mode)
    return ThinContextPolicy(
        mode=mode,
        max_exchanges=max_exchanges,
        compact_older_tools=compact,
        thin_reference_shelf=thin_shelf,
        pin_tools=pin_tools,
        pin_tool_priorities=pin_priorities,
        pin_patterns=pin_patterns,
        pin_reads=pin_reads,
        pin_token_ceiling=pin_ceiling,
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


def _resolve_fast_pins(
    source: Any, mode: str,
) -> tuple[tuple[str, ...], tuple[int, ...], tuple[str, ...], tuple[str, ...], int]:
    """Resolve pin env into commands, priorities, regexes, file reads, and a ceiling.

    Audit mode drops the lists so a fast-only knob cannot change the audit
    projection. Unset values take the fast-mode defaults; an explicit empty
    list turns that side off. ``name@N`` on a command sets its ceiling priority
    (smaller N is kept first). ``count_photos.py`` defaults to priority 0.
    """

    raw_tools = getattr(source, "fast_pin_tools", None)
    raw_patterns = getattr(source, "fast_pin_patterns", None)
    raw_reads = getattr(source, "fast_pin_reads", None)
    raw_ceiling = getattr(source, "fast_pin_token_ceiling", None)
    if raw_ceiling is None:
        ceiling = DEFAULT_FAST_PIN_TOKEN_CEILING
    elif isinstance(raw_ceiling, bool) or not isinstance(raw_ceiling, int) or raw_ceiling < 0:
        raise ValueError("SKILLGO_FAST_PIN_TOKEN_CEILING must be an integer >= 0")
    else:
        ceiling = raw_ceiling
    if mode != "fast":
        return (), (), (), (), ceiling
    tool_tokens = DEFAULT_FAST_PIN_TOOLS if raw_tools is None else _pin_strings(raw_tools, "SKILLGO_FAST_PIN_TOOLS")
    tools: list[str] = []
    priorities: list[int] = []
    for token in tool_tokens:
        needle, priority = _split_pin_priority(token)
        tools.append(needle)
        priorities.append(priority)
    patterns = (
        DEFAULT_FAST_PIN_PATTERNS
        if raw_patterns is None
        else _pin_strings(raw_patterns, "SKILLGO_FAST_PIN_PATTERNS")
    )
    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"SKILLGO_FAST_PIN_PATTERNS contains an invalid regex: {pattern}") from exc
    reads = DEFAULT_FAST_PIN_READS if raw_reads is None else _pin_strings(raw_reads, "SKILLGO_FAST_PIN_READS")
    return tuple(tools), tuple(priorities), patterns, reads, ceiling


def _pin_strings(value: Any, env_name: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (tuple, list)):
        raise ValueError(f"{env_name} must be a list of strings")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{env_name} entries must be strings")
        stripped = item.strip()
        if stripped:
            items.append(stripped)
    return tuple(items)


@lru_cache(maxsize=64)
def _compiled_pin_patterns(patterns: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(pattern) for pattern in patterns)


@dataclass(frozen=True)
class PinnedHit:
    """One tool result selected as a fast-mode pin candidate.

    ``key`` dedupes repeats (only the latest message for that key is kept).
    ``priority`` is the ceiling order: smaller is kept first.
    """

    index: int
    key: str
    priority: int


def _split_pin_priority(token: str) -> tuple[str, int]:
    match = re.fullmatch(r"(.+?)@(\d+)$", token.strip())
    if match:
        return match.group(1), int(match.group(2))
    needle = token.strip()
    if "count_photos.py" in needle.casefold():
        return needle, 0
    return needle, 1


def _command_rules(policy: ThinContextPolicy) -> tuple[tuple[str, int], ...]:
    priorities = policy.pin_tool_priorities
    rules: list[tuple[str, int]] = []
    for index, token in enumerate(policy.pin_tools):
        needle, derived = _split_pin_priority(token)
        priority = priorities[index] if index < len(priorities) else derived
        if needle:
            rules.append((needle, priority))
    return tuple(rules)


def pinned_hits(group: list[dict[str, Any]], policy: ThinContextPolicy) -> tuple[PinnedHit, ...]:
    """Tool results fast mode may keep verbatim. Audit mode returns nothing.

    Primary match is the command text (argv / script path), not prose and not
    section labels. The whole result is a hit, so stderr is pinned with stdout.
    """

    if policy.mode != "fast":
        return ()
    if not policy.pin_tools and not policy.pin_patterns and not policy.pin_reads:
        return ()
    call_blobs: dict[str, str] = {}
    call_names: dict[str, str] = {}
    for message in group:
        calls = message.get("tool_calls") or []
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict):
                continue
            call_id = call.get("id")
            if isinstance(call_id, str) and call_id:
                call_blobs[call_id] = _call_command_blob(call)
                call_names[call_id] = _call_function_name(call)
    hits: list[PinnedHit] = []
    for index, message in enumerate(group):
        if message.get("role") not in {"tool", "user"}:
            continue
        classified = _classify_pin(message, call_blobs, call_names, policy)
        if classified is not None:
            key, priority = classified
            hits.append(PinnedHit(index, key, priority))
    return tuple(hits)


def pinned_message_indexes(group: list[dict[str, Any]], policy: ThinContextPolicy) -> tuple[int, ...]:
    return tuple(hit.index for hit in pinned_hits(group, policy))


def _classify_pin(
    message: dict[str, Any],
    call_blobs: dict[str, str],
    call_names: dict[str, str],
    policy: ThinContextPolicy,
) -> tuple[str, int] | None:
    call_id = message.get("tool_call_id")
    blob = ""
    function_name = ""
    if isinstance(call_id, str):
        blob = call_blobs.get(call_id, "")
        function_name = call_names.get(call_id, "")
    payload_blob = _payload_command_blob(message)
    command_text = "\n".join(part for part in (blob, payload_blob) if part)
    command_hit = _match_command(command_text, policy)
    if command_hit is not None:
        return command_hit
    read_hit = _match_read(function_name, command_text, message, policy)
    if read_hit is not None:
        return read_hit
    return _match_pattern(message, policy)


def _match_command(command_text: str, policy: ThinContextPolicy) -> tuple[str, int] | None:
    if not command_text:
        return None
    folded = command_text.replace("\\", "/").casefold()
    found: list[tuple[int, str]] = []
    for needle, priority in _command_rules(policy):
        token = needle.replace("\\", "/").casefold()
        if token and token in folded:
            found.append((priority, token))
    if not found:
        return None
    found.sort()
    priority, key = found[0]
    return key, priority


def _match_read(
    function_name: str,
    command_text: str,
    message: dict[str, Any],
    policy: ThinContextPolicy,
) -> tuple[str, int] | None:
    if not policy.pin_reads:
        return None
    tool_name = function_name or _tool_result_name(message)
    # Only read_file / read_files. A later write or script that mentions the
    # path must not replace the pinned file body. design_spec.md matches even
    # though it is outside assets/sources/.
    if tool_name not in _READ_TOOL_NAMES:
        return None
    path_blob = "\n".join(part for part in (command_text, "\n".join(_path_values(message))) if part)
    folded = path_blob.replace("\\", "/").casefold()
    matched = [
        read.replace("\\", "/").casefold()
        for read in policy.pin_reads
        if read and read.replace("\\", "/").casefold() in folded
    ]
    if not matched:
        return None
    # Longest configured path wins so design_spec.md is distinct from sources/.
    needle = max(matched, key=len)
    return f"read:{needle}", _READ_PRIORITY


def _match_pattern(message: dict[str, Any], policy: ThinContextPolicy) -> tuple[str, int] | None:
    content = message.get("content")
    if not isinstance(content, str) or not content or not policy.pin_patterns:
        return None
    patterns = _compiled_pin_patterns(policy.pin_patterns)
    for pattern, compiled in zip(policy.pin_patterns, patterns):
        if compiled.search(content):
            # Own key. A command-name hit is classified earlier and keeps its
            # script key even when this result also matches a fallback regex.
            return f"pattern:{pattern}", _PATTERN_PRIORITY
    return None


def _call_function_name(call: dict[str, Any]) -> str:
    function = call.get("function")
    if isinstance(function, dict) and isinstance(function.get("name"), str):
        return function["name"]
    return ""


def _call_command_blob(call: dict[str, Any]) -> str:
    function = call.get("function")
    texts: list[str] = []
    if isinstance(function, dict):
        name = function.get("name")
        if isinstance(name, str):
            texts.append(name)
        arguments = function.get("arguments")
        parsed: Any = None
        if isinstance(arguments, str):
            texts.append(arguments)
            try:
                parsed = json.loads(arguments)
            except ValueError:
                parsed = None
        elif isinstance(arguments, (dict, list)):
            parsed = arguments
        if parsed is not None:
            texts.extend(_iter_command_texts(parsed))
    return "\n".join(texts)


def _payload_command_blob(message: dict[str, Any]) -> str:
    content = message.get("content")
    if not isinstance(content, str) or not content:
        return ""
    try:
        body = json.loads(content)
    except ValueError:
        return ""
    texts: list[str] = []
    if isinstance(body, dict):
        texts.extend(_iter_command_texts(body))
        texts.extend(_iter_command_texts(body.get("payload")))
    return "\n".join(texts)


def _tool_result_name(message: dict[str, Any]) -> str:
    content = message.get("content")
    if not isinstance(content, str):
        return ""
    try:
        body = json.loads(content)
    except ValueError:
        return ""
    if isinstance(body, dict) and isinstance(body.get("tool_result"), str):
        return body["tool_result"]
    return ""


def _path_values(message: dict[str, Any]) -> list[str]:
    content = message.get("content")
    if not isinstance(content, str):
        return []
    try:
        body = json.loads(content)
    except ValueError:
        return []
    found: list[str] = []
    _collect_paths(body, found)
    return found


def _collect_paths(value: Any, found: list[str]) -> None:
    if isinstance(value, dict):
        for key in ("path", "paths", "file", "filepath"):
            item = value.get(key)
            if isinstance(item, str):
                found.append(item)
            elif isinstance(item, list):
                found.extend(entry for entry in item if isinstance(entry, str))
        for item in value.values():
            if isinstance(item, (dict, list)):
                _collect_paths(item, found)
    elif isinstance(value, list):
        for item in value:
            _collect_paths(item, found)


def _iter_command_texts(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key in _COMMAND_STRING_KEYS:
            item = value.get(key)
            if isinstance(item, str):
                found.append(item)
        argv = value.get("argv")
        if isinstance(argv, list):
            for item in argv:
                if isinstance(item, str):
                    found.append(item)
        for key in ("commands", "calls", "children"):
            nested = value.get(key)
            if isinstance(nested, list):
                for item in nested:
                    found.extend(_iter_command_texts(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_iter_command_texts(item))
    return found
