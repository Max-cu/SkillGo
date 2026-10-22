from __future__ import annotations

import json

from fastapi import HTTPException


def parse_existing_file_ids(raw: str) -> list[str]:
    """Validate historical attachment selections for chat and Skill job requests."""
    if not raw.strip():
        return []
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="历史附件参数格式无效") from exc
    if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
        raise HTTPException(status_code=422, detail="历史附件参数必须是文件 ID 数组")
    normalized = [item.strip() for item in decoded if item.strip()]
    if len(normalized) != len(set(normalized)):
        raise HTTPException(status_code=422, detail="不能重复选择同一个历史附件")
    return normalized
