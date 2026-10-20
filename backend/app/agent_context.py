"""Budgeted context projection; original history remains available for replay."""
from __future__ import annotations

import json
import hashlib
from typing import Any
from .context_thinning import ThinContextPolicy, compact_tool_message, project_shelf_entry
from .skill_phase_contracts import reference_payloads


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
    # Compaction is applied to the copies the budget walk measures, so a
    # thinned older tool result can stay while its full body remains in
    # ``messages`` for replay. The latest exchange is never compacted:
    # the model still receives the result it just produced.
    exchange_cap_dropped = 0
    older_groups = groups[:-1]
    if policy.max_exchanges and len(groups) > policy.max_exchanges:
        exchange_cap_dropped = len(groups) - policy.max_exchanges
        older_groups = groups[-policy.max_exchanges:-1]
    summarized_count = 0
    removed_tokens = 0
    # Pairs keep the original group for reference dedup and a projected
    # group for the model request. With compaction off they are the same list.
    selected_pairs: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = (
        [(latest, latest)] if latest else []
    )
    for group in reversed(older_groups):
        projected_group, count, removed = _project_exchange(group, policy)
        candidate = [(group, projected_group), *selected_pairs]
        projected_messages = [message for _original, item in candidate for message in item]
        trial = [*pinned, *projected_messages, memory]
        if estimate_tokens(trial) + tool_tokens > max_tokens:
            break
        selected_pairs = candidate
        summarized_count += count
        removed_tokens += removed
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
        })
    return projected


def _project_exchange(
    group: list[dict[str, Any]], policy: ThinContextPolicy,
) -> tuple[list[dict[str, Any]], int, int]:
    """Compact tool results inside one older exchange. Return the same list if unchanged."""
    if not policy.compact_older_tools:
        return group, 0, 0
    projected: list[dict[str, Any]] = []
    count = 0
    removed = 0
    changed = False
    for message in group:
        compacted = compact_tool_message(message, policy)
        if compacted is not message:
            changed = True
            count += 1
            removed += max(0, estimate_tokens(message) - estimate_tokens(compacted))
        projected.append(compacted)
    if not changed:
        return group, 0, 0
    return projected, count, removed
