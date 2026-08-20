from __future__ import annotations

import asyncio
import json
import os
import sys


sys.path.insert(0, "/app")

import httpx
from sqlalchemy import select

from app.database import SessionLocal
from app.models import WorkflowJob
from app.security import create_access_token
from app.storage import storage


async def main() -> None:
    job_prefix = os.environ["SKILLGO_RETRY_JOB_ID"]
    with SessionLocal() as db:
        original = db.scalars(
            select(WorkflowJob).where(WorkflowJob.id.startswith(job_prefix))
        ).first()
        if original is None or not original.input_files:
            raise SystemExit("job or input file not found")
        input_file = original.input_files[0]
        version_id = original.skill_version_id
        user_id = original.user_id
        instruction = original.instruction
        filename = input_file.filename
        content_type = input_file.content_type
        data = storage.read(input_file.storage_path)

    token = create_access_token(user_id)
    api_url = os.getenv("SKILLGO_API_URL", "http://127.0.0.1:8000/api/v1/jobs")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            api_url,
            headers={"Authorization": f"Bearer {token}"},
            data={"version_id": version_id, "instruction": instruction},
            files={"file": (filename, data, content_type)},
        )
    response.raise_for_status()
    payload = response.json()
    print(json.dumps({"job_id": payload["id"], "status": payload["status"]}))


if __name__ == "__main__":
    asyncio.run(main())
