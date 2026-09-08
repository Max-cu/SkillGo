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
                    completed: set[int], max_tokens: int) -> list[dict[str, Any]]:
    pinned = messages[:2]
    # Single-Skill instructions already live in the pinned system message.
    # Keep that stable prefix intact and avoid sending the full guide twice.
    pinned_system = "\n".join(message['content'] for message in pinned
                              if message.get('role') == 'system' and isinstance(message.get('content'), str))
    active = [{"index": i, "root": item["root"], "skill_md": item["skill_md"]}
              for i, item in enumerate(skill_contexts, 1)
              if i in loaded and i not in completed and item['skill_md'] not in pinned_system]
    memory = {"role": "user", "content": "Execution state and active Skill instructions (user requirements take precedence):\n" +
              json.dumps({"checkpoint": json.loads(checkpoint), "active_skills": active}, ensure_ascii=False)}
    # Drop complete exchanges, never edit native assistant reasoning/tool fields.
    groups: list[list[dict[str, Any]]] = []
    for message in messages[2:]:
        if message.get("role") == "assistant" or not groups:
            groups.append([])
        groups[-1].append(message)
    selected: list[list[dict[str, Any]]] = []
    used = estimate_tokens([*pinned, memory])
    if used >= max_tokens:
        raise ValueError("Original request and active Skill exceed the model context budget; select fewer Skills or configure a larger context budget.")
    for group in reversed(groups):
        size = estimate_tokens(group)
        if used + size > max_tokens:
            break
        selected.insert(0, group)
        used += size
    return [*pinned, memory, *(message for group in selected for message in group)]
