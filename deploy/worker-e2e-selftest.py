import asyncio
import hashlib
import io
import sys
import zipfile
from uuid import uuid4


sys.path.insert(0, "/app")

from app.database import Base, SessionLocal, engine
from app.model_gateway import ModelResult
from app.models import (
    JobInputFile,
    JobStatus,
    JobStep,
    JobStepStatus,
    Role,
    Skill,
    SkillType,
    SkillVersion,
    User,
    VersionStatus,
    Visibility,
    WorkflowJob,
)
from app.sandbox_runtime import docker_client
from app.storage import storage
import app.sandbox_worker as worker


class FakeGateway:
    def __init__(self) -> None:
        self.index = 0

    async def agent_step(self, *, messages):
        self.index += 1
        if self.index == 1:
            action = {
                "action": "read_file",
                "path": "/workspace/input",
                "reason": "故意触发一次可恢复的目录读取错误",
            }
        elif self.index == 2:
            action = {
                "action": "write_file",
                "path": "/workspace/work/large.txt",
                "content": "x" * 10_000,
                "reason": "验证受控写文件可承载超过普通命令参数限制的内容",
            }
        elif self.index == 3:
            action = {
                "action": "command",
                "argv": [
                    "python3",
                    "-c",
                    "from pathlib import Path; out=Path('/workspace/output'); out.mkdir(); source=Path('/workspace/input/input.txt').read_text(encoding='utf-8'); assert len(Path('/workspace/work/large.txt').read_text()) == 10000; out.joinpath('result.txt').write_text('verified:'+source,encoding='utf-8')",
                ],
                "cwd": "/workspace/skill/demo",
                "timeout_seconds": 30,
                "reason": "生成真实测试产物",
            }
        elif self.index == 4:
            action = {
                "action": "finish",
                "summary": "故意先声明一个不存在的产物以验证自动纠正",
                "artifacts": [
                    "/workspace/output/result.txt",
                    "/workspace/output/missing.txt",
                ],
            }
        else:
            action = {
                "action": "finish",
                "summary": "测试工作流已在沙箱完成",
                "artifacts": ["/workspace/output/result.txt"],
            }
        return ModelResult(output=action, model_name="selftest", token_usage={})


async def main() -> None:
    Base.metadata.create_all(bind=engine)
    run_token = uuid4().hex[:10]
    package_buffer = io.BytesIO()
    with zipfile.ZipFile(package_buffer, "w") as archive:
        archive.writestr("demo/SKILL.md", "# Self test\nCreate one verified result file.")
        archive.writestr("demo/scripts/placeholder.py", "print('placeholder')")
    package = package_buffer.getvalue()
    input_data = "tenant-isolated".encode("utf-8")

    with SessionLocal() as db:
        user = User(
            email=f"selftest-{run_token}@example.com",
            display_name="Self Test",
            password_hash="not-used",
            role=Role.SUPER_ADMIN,
        )
        db.add(user)
        db.flush()
        skill = Skill(
            owner_id=user.id,
            slug=f"sandbox-selftest-{run_token}",
            name="Sandbox Self Test",
            summary="Sandbox end-to-end verification",
            visibility=Visibility.PRIVATE,
        )
        db.add(skill)
        db.flush()
        package_path = storage.put("selftest/package.zip", package)
        version = SkillVersion(
            skill_id=skill.id,
            created_by_id=user.id,
            version="1.0.0",
            status=VersionStatus.PUBLISHED,
            skill_type=SkillType.CODE,
            package_sha256=hashlib.sha256(package).hexdigest(),
            package_path=package_path,
            manifest={"apiVersion": "skillgo.dev/v1", "spec": {"type": "code"}},
            skill_md="# Self test\nCreate one verified result file.",
            input_schema={},
            output_schema={},
            requested_permissions={},
        )
        db.add(version)
        db.flush()
        job = WorkflowJob(
            user_id=user.id,
            skill_id=skill.id,
            skill_version_id=version.id,
            status=JobStatus.RUNNING,
            execution_mode="sandbox_required",
            instruction="Run the deterministic self test.",
        )
        db.add(job)
        db.flush()
        for position, (key, name) in enumerate(
            (
                ("prepare-input", "Prepare"),
                ("execute-workflow", "Execute"),
                ("collect-artifacts", "Collect"),
                ("verify-artifacts", "Verify"),
            ),
            1,
        ):
            status = JobStepStatus.SUCCEEDED if key == "prepare-input" else JobStepStatus.PENDING
            if key == "execute-workflow":
                status = JobStepStatus.RUNNING
            db.add(JobStep(job_id=job.id, step_key=key, name=name, position=position, status=status))
        input_path = storage.put("selftest/input.txt", input_data)
        db.add(
            JobInputFile(
                job_id=job.id,
                user_id=user.id,
                filename="input.txt",
                content_type="text/plain",
                size_bytes=len(input_data),
                sha256=hashlib.sha256(input_data).hexdigest(),
                storage_path=input_path,
                readable=True,
                extracted_text=input_data.decode(),
            )
        )
        db.commit()
        job_id = job.id

    worker.OpenAICompatibleGateway = FakeGateway
    await worker.execute_sandbox_job(job_id, docker_client())

    with SessionLocal() as db:
        job = db.get(WorkflowJob, job_id)
        assert job.status == JobStatus.SUCCEEDED, (job.status, job.error_code, job.error_message)
        assert len(job.artifacts) == 1
        artifact = job.artifacts[0]
        assert artifact.verified is True
        assert storage.read(artifact.storage_path) == b"verified:tenant-isolated"
        print("job_status", job.status.value)
        print("steps", [step.status.value for step in job.steps])
        print("artifact_verified", artifact.verified)
        print("artifact_content", "verified")


asyncio.run(main())
