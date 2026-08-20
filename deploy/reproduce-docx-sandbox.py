from __future__ import annotations

import asyncio
import io
import os
import zipfile

from sqlalchemy import select

from app.database import SessionLocal
from app.models import WorkflowJob
from app.sandbox_runtime import DockerSandbox, docker_client, package_skill_root
from app.storage import storage


async def main() -> None:
    job_prefix = os.environ["SKILLGO_DEBUG_JOB_ID"]
    with SessionLocal() as db:
        job = db.scalars(
            select(WorkflowJob).where(WorkflowJob.id.startswith(job_prefix))
        ).first()
        if job is None:
            raise SystemExit("job not found")
        package = storage.read(job.skill_version.package_path)
        inputs = {
            f"input/{item.filename}": storage.read(item.storage_path)
            for item in job.input_files
        }
        input_names = [item.filename for item in job.input_files]

    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        skill_root = package_skill_root(archive.namelist())

    with DockerSandbox(docker_client(), job_id=f"debug-{job_prefix}") as sandbox:
        sandbox.put_files({"skill.zip": package, **inputs})
        setup = await sandbox.command(
            [
                "python3",
                "-c",
                "import zipfile;zipfile.ZipFile('/workspace/skill.zip').extractall('/workspace/skill')",
            ],
            timeout_seconds=60,
        )
        print(f"setup_exit={setup.exit_code}")
        listing = await sandbox.list_files("/workspace/input")
        print(f"input_files={[item['path'] for item in listing if item['type'] == 'file']}")
        input_path = f"/workspace/input/{input_names[0]}"
        workdir = "/workspace/work/reproduction"
        extraction = await sandbox.command(
            [
                "python3",
                f"{skill_root}/scripts/extract_structure.py",
                input_path,
                "--out",
                workdir,
            ],
            cwd=skill_root,
            timeout_seconds=120,
        )
        print(f"extract_exit={extraction.exit_code}")
        print(f"extract_stdout={extraction.stdout[-1000:]}")
        print(f"extract_stderr={extraction.stderr[-1000:]}")
        output_listing = await sandbox.list_files(workdir) if extraction.exit_code == 0 else []
        print(f"output_files={[item['path'] for item in output_listing if item['type'] == 'file']}")


if __name__ == "__main__":
    asyncio.run(main())
