"""Create a SkillGo sandbox workflow job and download its artifacts.

Required environment variables:
    SKILLGO_API_KEY
    SKILLGO_ENDPOINT_SLUG
    SKILLGO_INPUT_FILE

Optional:
    SKILLGO_BASE_URL (default: http://127.0.0.1:8080)
    SKILLGO_INSTRUCTION
    SKILLGO_OUTPUT_DIR (default: ./skillgo-artifacts)
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import requests


TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "blocked"}


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def main() -> None:
    base_url = os.getenv("SKILLGO_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
    api_key = required_env("SKILLGO_API_KEY")
    endpoint_slug = required_env("SKILLGO_ENDPOINT_SLUG")
    input_path = Path(required_env("SKILLGO_INPUT_FILE")).expanduser().resolve()
    output_dir = Path(os.getenv("SKILLGO_OUTPUT_DIR", "skillgo-artifacts")).resolve()
    instruction = os.getenv("SKILLGO_INSTRUCTION", "请完整执行工作流并生成最终产物")
    if not input_path.is_file():
        raise SystemExit(f"Input file does not exist: {input_path}")

    endpoint_url = f"{base_url}/api/v1/workflow-endpoints/{endpoint_slug}"
    headers = {
        "X-SkillGo-Key": api_key,
        "Idempotency-Key": str(uuid.uuid4()),
    }
    with input_path.open("rb") as input_file:
        response = requests.post(
            f"{endpoint_url}/jobs",
            headers=headers,
            data={"instruction": instruction},
            files={"file": (input_path.name, input_file, "application/octet-stream")},
            timeout=60,
        )
    response.raise_for_status()
    job = response.json()
    job_id = job["id"]
    print(f"Created job {job_id}: {job['status']}")

    while job["status"] not in TERMINAL_STATUSES:
        time.sleep(2)
        response = requests.get(
            f"{endpoint_url}/jobs/{job_id}",
            headers={"X-SkillGo-Key": api_key},
            timeout=30,
        )
        response.raise_for_status()
        job = response.json()
        print(f"Job {job_id}: {job['status']}")

    if job["status"] != "succeeded":
        raise SystemExit(
            f"Job ended as {job['status']}: "
            f"{job.get('error_code') or '-'} {job.get('error_message') or ''}"
        )

    response = requests.get(
        f"{endpoint_url}/jobs/{job_id}/artifacts",
        headers={"X-SkillGo-Key": api_key},
        timeout=30,
    )
    response.raise_for_status()
    artifacts = response.json()
    output_dir.mkdir(parents=True, exist_ok=True)
    for artifact in artifacts:
        target = output_dir / Path(artifact["filename"]).name
        download = requests.get(
            f"{endpoint_url}/jobs/{job_id}/artifacts/{artifact['id']}/download",
            headers={"X-SkillGo-Key": api_key},
            timeout=120,
        )
        download.raise_for_status()
        target.write_bytes(download.content)
        print(f"Downloaded {target} (verified={artifact['verified']})")


if __name__ == "__main__":
    main()
