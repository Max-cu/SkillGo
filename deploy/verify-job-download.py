from __future__ import annotations

import asyncio
import hashlib
import io
import os
import sys
import zipfile

import httpx


sys.path.insert(0, "/app")

from app.database import SessionLocal
from app.models import WorkflowJob
from app.security import create_access_token


async def main() -> None:
    job_id = os.environ["SKILLGO_JOB_ID"]
    api_base = os.getenv("SKILLGO_API_BASE", "http://api:8000/api/v1")
    with SessionLocal() as db:
        job = db.get(WorkflowJob, job_id)
        if job is None:
            raise SystemExit("job not found")
        artifact = next(
            (item for item in job.artifacts if item.filename.lower().endswith(".docx")),
            None,
        )
        if artifact is None:
            raise SystemExit("docx artifact not found")
        artifact_id = artifact.id
        expected_size = artifact.size_bytes
        expected_sha256 = artifact.sha256
        token = create_access_token(job.user_id)

    url = f"{api_base}/jobs/{job_id}/artifacts/{artifact_id}/download"
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(url, headers={"Authorization": f"Bearer {token}"})
    response.raise_for_status()
    data = response.content
    assert len(data) == expected_size
    assert hashlib.sha256(data).hexdigest() == expected_sha256
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.testzip() is None
        assert "word/document.xml" in archive.namelist()

    print("download_status", response.status_code)
    print("download_size", len(data))
    print("sha256_verified", True)
    print("docx_integrity", True)


asyncio.run(main())
