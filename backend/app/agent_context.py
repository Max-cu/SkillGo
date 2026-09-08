"""Budgeted context projection; original history remains available for replay."""
from __future__ import annotations

import json
from typing import Any


def estimate_tokens(value: object) -> int:
    # Conservative UTF-8 estimate works for mixed Chinese/English without
    # assuming a tokenizer shared by all private model providers.
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8")) // 2 + 1


def project_context(messages: list[dict[str, Any]], *, checkpoint: str,
                    skill_contexts: list[dict[str, Any]], loaded: set[int],
                    completed: set[int], max_tokens: int,
                    tool_tokens: int = 0, initial_exchange_reserve: int = 1024,
                    diagnostics: dict[str, Any] | None = None) -> list[dict[str, Any]]:
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
    # Only observation excerpts are expendable. Requirements, plan, validation
    # evidence and active guides remain intact or the request fails explicitly.
    def memory_message(recent: list[dict]) -> dict[str, Any]:
        return {"role": "user", "content": "Execution state and active Skill instructions (user requirements take precedence):\n" +
                json.dumps({"checkpoint": {**state, 'recent_observations': recent},
                            "active_skills": active}, ensure_ascii=False)}

    memory = memory_message([])
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
        trial = memory_message(candidate)
        if estimate_tokens([*pinned, trial, *latest]) + tool_tokens + reserve > max_tokens:
            break
        recent = candidate
        memory = trial

    selected = [latest] if latest else []
    for group in reversed(groups[:-1]):
        candidate = [group, *selected]
        projected = [*pinned, memory, *(message for exchange in candidate for message in exchange)]
        if estimate_tokens(projected) + tool_tokens > max_tokens:
            break
        selected = candidate
    projected = [*pinned, memory, *(message for exchange in selected for message in exchange)]
    if diagnostics is not None:
        diagnostics.update({
            'state_estimated_tokens': estimate_tokens([memory]),
            'request_estimated_tokens': estimate_tokens(projected) + tool_tokens,
            'dropped_exchange_count': len(groups) - len(selected),
            'retained_observation_count': len(recent),
        })
    return projected
