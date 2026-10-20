"""Budgeted context projection; original history remains available for replay."""
from __future__ import annotations

import json
import hashlib
import logging
from typing import Any
from .context_thinning import (
    ThinContextPolicy,
    compact_tool_message,
    pinned_hits,
    project_shelf_entry,
)
from .skill_phase_contracts import reference_payloads

logger = logging.getLogger(__name__)


def estimate_tokens(value: object) -> int:
    # Conservative UTF-8 estimate works for mixed Chinese/English without
    # assuming a tokenizer shared by all private model providers.
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8")) // 2 + 1


def _deduplicate_reference_shelf(shelf, retained_messages):
    """Remove only an identical full reference copy already in native history.

    Original history and checkpoint data are never edited. If history was
    dropped, changed, partial, mutable, or legacy/unrecognized, keep the full
    shelf entry. This does not summarize instructions or trim extra exchanges.
    """
    references = {}
    for message in retained_messages:
        if message.get('role') != 'tool' or not isinstance(message.get('content'), str):
            continue
        try:
            result = json.loads(message['content'])
        except (ValueError, TypeError):
            continue
        if not isinstance(result, dict):
            continue
        payloads = reference_payloads(result)
        for payload in payloads:
            if (not isinstance(payload, dict) or payload.get('reference') is not True
                    or not (payload.get('complete') is True or payload.get('retained') is True)
                    or not isinstance(payload.get('path'), str) or not isinstance(payload.get('content'), str)):
                continue
            text = payload['content']
            try:
                digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
            except UnicodeEncodeError:
                continue
            if payload.get('sha256') not in (None, digest):
                continue
            references[(payload['path'], text, digest)] = message.get('tool_call_id')
    view = []
    removed = 0
    removed_chars = 0
    for entry in shelf:
        text = entry.get('content')
        key = (entry.get('path'), text, entry.get('sha256'))
        if (isinstance(text, str) and isinstance(entry.get('path'), str)
                and isinstance(entry.get('sha256'), str) and key in references):
            metadata = {k: v for k, v in entry.items() if k != 'content'}
            metadata.update(content_in_retained_tool_history=True,
                            history_tool_call_id=references[key])
            if estimate_tokens(metadata) < estimate_tokens(entry):
                view.append(metadata)
                removed += 1
                removed_chars += len(text)
                continue
        view.append(entry)
    return view, removed, removed_chars


def project_context(messages: list[dict[str, Any]], *, checkpoint: str,
                    skill_contexts: list[dict[str, Any]], loaded: set[int],
                    completed: set[int], max_tokens: int,
                    tool_tokens: int = 0, initial_exchange_reserve: int = 1024,
                    deduplicate_references: bool = True,
                    diagnostics: dict[str, Any] | None = None,
                    thin: ThinContextPolicy | None = None) -> list[dict[str, Any]]:
    # Default policy is a no-op so existing callers keep the audit projection.
    # The sandbox loop passes resolve_thin_context_policy(settings).
    policy = thin or ThinContextPolicy()
    pinned = messages[:2]
    # Single-Skill instructions already live in the pinned system message.
    # Keep that stable prefix intact and avoid sending the full guide twice.
    pinned_system = "\n".join(message['content'] for message in pinned
                              if message.get('role') == 'system' and isinstance(message.get('content'), str))
    active = [{"index": i, "root": item["root"], "skill_md": item["skill_md"]}
              for i, item in enumerate(skill_contexts, 1)
              if i in loaded and i not in completed and item['skill_md'] not in pinned_system]
    state = json.loads(checkpoint)
    observations = state.pop('recent_observations', [])
    shelf_entries = [
        project_shelf_entry(entry, policy)
        for entry in (state.pop('reference_shelf', []) or [])
    ]
    # Only observation excerpts are expendable. Requirements, plan, validation
    # evidence and active guides remain intact or the request fails explicitly.
    def memory_message(recent: list[dict], shelf: list[dict]) -> dict[str, Any]:
        return {"role": "user", "content": "Execution state and active Skill instructions (user requirements take precedence):\n" +
                json.dumps({"checkpoint": {**state, 'recent_observations': recent,
                                           'reference_shelf': shelf},
                            "active_skills": active}, ensure_ascii=False)}

    memory = memory_message([], [])
    # Drop complete exchanges, never edit native assistant reasoning/tool fields.
    groups: list[list[dict[str, Any]]] = []
    for message in messages[2:]:
        if message.get("role") == "assistant" or not groups:
            groups.append([])
        groups[-1].append(message)
    latest = groups[-1] if groups else []
    reserve = initial_exchange_reserve if not latest else 0
    required = estimate_tokens([*pinned, memory, *latest]) + tool_tokens + reserve
    parts = {
        'input_budget_tokens': max_tokens,
        'pinned_estimated_tokens': estimate_tokens(pinned),
        'state_estimated_tokens': estimate_tokens([memory]),
        'latest_exchange_estimated_tokens': estimate_tokens(latest) if latest else 0,
        'tool_schema_estimated_tokens': tool_tokens,
        'initial_exchange_reserve_tokens': reserve,
        'minimum_required_tokens': required,
    }
    if diagnostics is not None:
        diagnostics.update(parts)
    if required > max_tokens:
        raise ValueError(
            'Original request / 上下文预算不足：'
            f'输入预算 {max_tokens}，至少需要约 {required} token'
            f'（固定指令 {parts["pinned_estimated_tokens"]}、状态与活动 Skill {parts["state_estimated_tokens"]}、'
            f'最近完整交互 {parts["latest_exchange_estimated_tokens"]}、工具定义 {tool_tokens}、启动预留 {reserve}）。'
            '已停止请求，避免丢失最近工具结果；请配置模型实际支持的预算或减少本次 Skill 范围。'
        )

    # Pin retained immutable references (Skill specs, user inputs) so they
    # remain usable after old exchanges leave the window. Newest entries win;
    # an entry that does not fit the remaining budget is skipped, never fatal.
    shelf: list[dict[str, Any]] = []
    for entry in reversed(shelf_entries):
        if not isinstance(entry, dict):
            continue
        trial = memory_message([], [entry, *shelf])
        if estimate_tokens([*pinned, trial, *latest]) + tool_tokens + reserve > max_tokens:
            continue
        shelf = [entry, *shelf]
    memory = memory_message([], shelf)

    # Keep at most 1024 estimated tokens of observation metadata. Raw stdout and
    # file content stay in the history/files, never in the pinned checkpoint.
    recent: list[dict] = []
    for observation in reversed(observations[-10:]):
        compact = {key: value for key, value in observation.items()
                   if key in {'tool', 'ok', 'operation', 'mutation_epoch', 'path', 'cwd',
                              'exit_code', 'error_code', 'full_result_path'}}
        compact = {key: value[:300] if isinstance(value, str) else value
                   for key, value in compact.items()}
        candidate = [compact, *recent]
        if estimate_tokens(candidate) > 1024:
            break
        trial = memory_message(candidate, shelf)
        if estimate_tokens([*pinned, trial, *latest]) + tool_tokens + reserve > max_tokens:
            break
        recent = candidate
        memory = trial

    # Exchange cap drops the oldest whole turns before the budget walk.
    # Fast-mode pins are extra: a matching tool result outside that window
    # stays verbatim and does not consume a cap slot. Only the latest result
    # for each pin key is eligible, then the ceiling keeps higher-priority
    # keys first (count_photos, then other image commands, then file reads,
    # then regex-only hits). The recent window is budgeted before any rescue
    # so a pin cannot crowd out those exchanges. A rescue that does not fit
    # is skipped; the next candidate is still tried. Compaction runs on the
    # copies the budget walk measures. The latest exchange is never compacted.
    # Durable ``messages`` stay intact.
    hit_groups = [pinned_hits(group, policy) for group in groups]
    if policy.max_exchanges and len(groups) > policy.max_exchanges:
        window_start = len(groups) - policy.max_exchanges
    else:
        window_start = 0
    protected_by_group, ceiling_dropped = _protect_pinned_results(groups, hit_groups, policy)
    rescued = [gi for gi in range(window_start) if protected_by_group.get(gi)]
    summarized_count = 0
    removed_tokens = 0
    # Pairs keep the original group for reference dedup and a projected
    # group for the model request. With compaction off they are the same list.
    selected: list[tuple[int, list[dict[str, Any]], list[dict[str, Any]]]] = []
    if groups:
        latest_index = len(groups) - 1
        selected.append((latest_index, groups[latest_index], groups[latest_index]))

    def _within_budget(pairs: list[tuple[int, list[dict[str, Any]], list[dict[str, Any]]]]) -> bool:
        projected_messages = [message for _gi, _original, item in pairs for message in item]
        trial = [*pinned, *projected_messages, memory]
        return estimate_tokens(trial) + tool_tokens <= max_tokens

    def _append_older(gi: int, *, rescue: bool = False) -> bool:
        nonlocal summarized_count, removed_tokens
        group = groups[gi]
        pinned_indexes = protected_by_group.get(gi, set())
        if rescue:
            projected_group = _project_rescued_exchange(group, pinned_indexes)
            count = 0
            removed = max(0, estimate_tokens(group) - estimate_tokens(projected_group))
        else:
            projected_group, count, removed = _project_exchange(
                group, policy, pinned_indexes=pinned_indexes,
            )
        candidate = [*selected, (gi, group, projected_group)]
        if not _within_budget(candidate):
            return False
        selected.append((gi, group, projected_group))
        summarized_count += count
        removed_tokens += removed
        return True

    # Recent window first, newest exchange first. Stop at the first miss so
    # the window stays a contiguous tail. Rescued pins come after and cannot
    # take that budget.
    cap_older = list(range(window_start, max(len(groups) - 1, 0)))
    for gi in reversed(cap_older):
        if not _append_older(gi):
            break

    def _rescue_rank(gi: int) -> tuple[int, int]:
        protected = protected_by_group.get(gi, set())
        priorities = [hit.priority for hit in hit_groups[gi] if hit.index in protected]
        priority = min(priorities) if priorities else _RESCUE_FALLBACK_PRIORITY
        return (priority, -gi)

    # Highest priority, then newer. Skip a candidate that does not fit and
    # keep trying; a large newer file read must not block count_photos.
    for gi in sorted(rescued, key=_rescue_rank):
        _append_older(gi, rescue=True)
    selected.sort(key=lambda item: item[0])
    selected_pairs: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = [
        (original, projected) for _gi, original, projected in selected
    ]
    selected_ids = {gi for gi, _original, _projected in selected}
    if policy.max_exchanges and len(groups) > policy.max_exchanges:
        exchange_cap_dropped = sum(1 for gi in range(window_start) if gi not in selected_ids)
    else:
        exchange_cap_dropped = 0
    pinned_result_tokens, pinned_result_count = _measure_pinned_results(
        groups, protected_by_group, selected,
    )
    rescued_exchange_tokens = sum(
        estimate_tokens(projected)
        for gi, _original, projected in selected
        if gi < window_start
    )
    if ceiling_dropped:
        logger.info(
            "fast pin token ceiling exceeded: dropped %s older pinned result(s); "
            "kept %s pinned result(s) (~%s tokens, ceiling %s)",
            ceiling_dropped,
            pinned_result_count,
            pinned_result_tokens,
            policy.pin_token_ceiling,
        )
    # Dedup against the uncompacted retained exchanges so an identical
    # reference still drops out of the shelf. The model copy may then
    # replace older non-reference tool bodies with a head/tail pointer.
    history_for_dedup = [
        *pinned,
        *(message for original, _projected in selected_pairs for message in original),
    ]
    shelf_view, duplicate_count, duplicate_chars = (
        _deduplicate_reference_shelf(shelf, history_for_dedup)
        if deduplicate_references else (shelf, 0, 0)
    )
    # The mutable state message goes LAST: history is append-only between
    # turns, so [pinned + history] stays a stable token prefix and providers
    # with prefix caching skip re-prefilling the whole exchange log each turn.
    history = [
        *pinned,
        *(message for _original, projected in selected_pairs for message in projected),
    ]
    memory = memory_message(recent, shelf_view)
    projected = [*history, memory]
    if diagnostics is not None:
        diagnostics.update({
            'state_estimated_tokens': estimate_tokens([memory]),
            'request_estimated_tokens': estimate_tokens(projected) + tool_tokens,
            'dropped_exchange_count': len(groups) - len(selected_pairs),
            'retained_observation_count': len(recent),
            'reference_duplicate_copies_removed': duplicate_count,
            'reference_duplicate_chars_removed': duplicate_chars,
            'orchestration_mode': policy.mode,
            'max_exchanges': policy.max_exchanges,
            'compact_older_tools': policy.compact_older_tools,
            'thin_reference_shelf': policy.thin_reference_shelf,
            'exchange_cap_dropped_count': exchange_cap_dropped,
            'preserved_exchange_count': len(selected_pairs),
            'summarized_count': summarized_count,
            'removed_tokens': removed_tokens,
            'pinned_result_estimated_tokens': pinned_result_tokens,
            'pinned_result_count': pinned_result_count,
            'pinned_ceiling_dropped_count': ceiling_dropped,
            'pinned_rescued_exchange_tokens': rescued_exchange_tokens,
        })
    return projected


def _protect_pinned_results(
    groups: list[list[dict[str, Any]]],
    hit_groups: list,
    policy: ThinContextPolicy,
) -> tuple[dict[int, set[int]], int]:
    """Keep one verbatim result per pin key, then apply the token ceiling.

    Repeats of the same command or the same file read collapse to the latest
    message so pinned tokens stay flat as rounds grow. Among those winners,
    smaller priority numbers are kept first (count_photos, then other image
    commands, then file reads, then regex-only hits); ties keep the newer
    result. The latest exchange is already retained whole and does not spend
    the ceiling.
    Anything not selected falls back to normal cap and compaction.
    """

    latest_gi = len(groups) - 1
    best: dict[str, tuple[int, int, int]] = {}
    for gi, hits in enumerate(hit_groups):
        for hit in hits:
            current = best.get(hit.key)
            if current is None or (gi, hit.index) >= (current[0], current[1]):
                best[hit.key] = (gi, hit.index, hit.priority)
    candidates: list[tuple[int, int, int, int, int, int]] = []
    for gi, mi, priority in best.values():
        if gi == latest_gi:
            continue
        candidates.append((
            priority, -gi, -mi, gi, mi, estimate_tokens(groups[gi][mi]),
        ))
    candidates.sort()
    protected: dict[int, set[int]] = {}
    running = 0
    dropped = 0
    for _priority, _newer_gi, _newer_mi, gi, mi, tok in candidates:
        if running + tok > policy.pin_token_ceiling:
            dropped += 1
            continue
        protected.setdefault(gi, set()).add(mi)
        running += tok
    return protected, dropped


def _measure_pinned_results(
    groups: list[list[dict[str, Any]]],
    protected_by_group: dict[int, set[int]],
    selected: list[tuple[int, list[dict[str, Any]], list[dict[str, Any]]]],
) -> tuple[int, int]:
    """Tokens of older pinned results kept verbatim in the projection.

    The latest exchange is omitted. It is already retained in full and does
    not spend the ceiling, so this field stays within ``pin_token_ceiling``
    and shows whether pinned history is growing.
    """

    selected_ids = {gi for gi, _original, _projected in selected}
    last_gi = len(groups) - 1
    tokens = 0
    count = 0
    for gi, group in enumerate(groups):
        if gi not in selected_ids or gi == last_gi:
            continue
        for mi in protected_by_group.get(gi, ()):
            tokens += estimate_tokens(group[mi])
            count += 1
    return tokens, count


_RESCUE_FALLBACK_PRIORITY = 99


def _project_rescued_exchange(
    group: list[dict[str, Any]], pinned_indexes: set[int],
) -> list[dict[str, Any]]:
    """Return the pinned tool results plus the assistant calls that produced them.

    The rest of the exchange (reasoning, prose, sibling tool results) stays in
    durable history and is not sent again. In-window exchanges are unchanged.
    """

    pinned_messages = [group[index] for index in range(len(group)) if index in pinned_indexes]
    needed_ids = {
        message.get("tool_call_id")
        for message in pinned_messages
        if isinstance(message.get("tool_call_id"), str) and message.get("tool_call_id")
    }
    projected: list[dict[str, Any]] = []
    for message in group:
        if message.get("role") != "assistant":
            continue
        calls = message.get("tool_calls") or []
        if not isinstance(calls, list):
            continue
        kept = [
            call for call in calls
            if isinstance(call, dict) and call.get("id") in needed_ids
        ]
        if kept:
            projected.append({"role": "assistant", "tool_calls": kept})
        break
    projected.extend(pinned_messages)
    return projected


def _project_exchange(
    group: list[dict[str, Any]], policy: ThinContextPolicy,
    *, pinned_indexes: set[int] | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    """Compact tool results inside one older exchange. Return the same list if unchanged.

    Messages in ``pinned_indexes`` stay verbatim. Other tool bodies still compact.
    """
    if not policy.compact_older_tools:
        return group, 0, 0
    protected = pinned_indexes or set()
    projected: list[dict[str, Any]] = []
    count = 0
    removed = 0
    changed = False
    for index, message in enumerate(group):
        if index in protected:
            projected.append(message)
            continue
        compacted = compact_tool_message(message, policy)
        if compacted is not message:
            changed = True
            count += 1
            removed += max(0, estimate_tokens(message) - estimate_tokens(compacted))
        projected.append(compacted)
    if not changed:
        return group, 0, 0
    return projected, count, removed
