from __future__ import annotations

import asyncio
import json
import os
import zipfile
from io import BytesIO

import httpx
from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models import WorkflowJob
from app.sandbox_runtime import package_skill_root
from app.sandbox_worker import _agent_messages
from app.storage import storage


async def main() -> None:
    job_id = os.environ["SKILLGO_DEBUG_JOB_ID"]
    with SessionLocal() as db:
        job = db.get(WorkflowJob, job_id) if len(job_id) >= 32 else db.scalars(
            select(WorkflowJob).where(WorkflowJob.id.startswith(job_id))
        ).first()
        if job is None:
            raise SystemExit("job not found")
        package = storage.read(job.skill_version.package_path)
        with zipfile.ZipFile(BytesIO(package)) as archive:
            names = archive.namelist()
        skill_root = package_skill_root(names)
        file_tree = [
            {"path": f"/workspace/skill/{name}", "type": "file", "size": 0}
            for name in names[:500]
            if not name.endswith("/")
        ]
        file_tree.extend(
            {
                "path": f"/workspace/input/{item.filename}",
                "type": "file",
                "size": item.size_bytes,
            }
            for item in job.input_files
        )
        messages = _agent_messages(job, skill_root, file_tree)

    base_url = (settings.model_base_url or "").rstrip("/")
    endpoint = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if settings.model_api_key:
        headers["Authorization"] = f"Bearer {settings.model_api_key}"
    body = {
        "model": settings.model_name,
        "messages": messages,
        "temperature": settings.model_temperature,
        "response_format": {"type": "json_object"},
    }
    async with httpx.AsyncClient(timeout=settings.model_timeout_seconds) as client:
        response = await client.post(endpoint, headers=headers, json=body)
    print(f"status={response.status_code}")
    response.raise_for_status()
    payload = response.json()
    message = payload.get("choices", [{}])[0].get("message", {})
    content = message.get("content")
    reasoning = message.get("reasoning_content")
    print(
        json.dumps(
            {
                "message_keys": sorted(message),
                "content_type": type(content).__name__,
                "content_length": len(content) if isinstance(content, str) else None,
                "content_preview": content[:3000] if isinstance(content, str) else content,
                "reasoning_length": len(reasoning) if isinstance(reasoning, str) else None,
                "finish_reason": payload.get("choices", [{}])[0].get("finish_reason"),
                "usage": payload.get("usage"),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
