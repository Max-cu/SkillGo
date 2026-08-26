from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .artifact_validation import snapshot_sandbox_artifacts
from .sandbox_runtime import DockerSandbox, SandboxRuntimeError
from .skill_execution_spec import FixedExecutionSpec


@dataclass(frozen=True)
class DeterministicRunResult:
    summary: str
    artifact_paths: tuple[str, ...]
    tool_operations: int
    validation_evidence: dict[str, Any]


async def execute_fixed_skill(
    sandbox: DockerSandbox,
    *,
    execution: FixedExecutionSpec,
    skill_root: str,
    instruction: str,
    input_files: list[dict[str, Any]],
) -> DeterministicRunResult:
    """Execute an approved immutable argv contract without model selection."""

    contract_path = "/workspace/work/skillgo-job.json"
    contract = {
        "version": 1,
        "instruction": instruction,
        "input_files": input_files,
        "input_root": "/workspace/input",
        "output_root": "/workspace/output",
        "work_root": "/workspace/work",
        "skill_root": skill_root,
    }
    await sandbox.write_text(
        contract_path,
        json.dumps(contract, ensure_ascii=False, indent=2),
    )

    entrypoint_result = await sandbox.command(
        list(execution.entrypoint),
        cwd=skill_root,
        timeout_seconds=execution.timeout_seconds,
    )
    if entrypoint_result.exit_code != 0:
        raise SandboxRuntimeError(
            "FIXED_ENTRYPOINT_FAILED",
            entrypoint_result.stderr
            or entrypoint_result.stdout
            or "Fixed Skill entrypoint failed",
        )

    before_verification = await snapshot_sandbox_artifacts(sandbox)
    if not before_verification:
        raise SandboxRuntimeError(
            "SANDBOX_ARTIFACT_MISSING",
            "Fixed Skill entrypoint did not create a regular file under /workspace/output",
        )

    operations = 1
    verifier_evidence: dict[str, Any]
    if execution.verifier:
        verifier_result = await sandbox.command(
            list(execution.verifier),
            cwd=skill_root,
            timeout_seconds=execution.verifier_timeout_seconds,
        )
        operations += 1
        if verifier_result.exit_code != 0:
            raise SandboxRuntimeError(
                "FIXED_VERIFIER_FAILED",
                verifier_result.stderr
                or verifier_result.stdout
                or "Fixed Skill verifier failed",
            )
        after_verification = await snapshot_sandbox_artifacts(sandbox)
        if after_verification != before_verification:
            raise SandboxRuntimeError(
                "FIXED_VERIFIER_MUTATED_OUTPUT",
                "The fixed verifier changed output bytes; verification must be read-only",
            )
        verifier_evidence = {
            "type": "manifest_argv",
            "argv": list(execution.verifier),
            "exit_code": verifier_result.exit_code,
            "stdout": verifier_result.stdout[:4000],
            "stderr": verifier_result.stderr[:2000],
        }
    else:
        verifier_evidence = {
            "type": "platform_structure",
            "exit_code": 0,
            "note": "No manifest verifier declared; platform structural validation completed.",
        }

    return DeterministicRunResult(
        summary=(
            f"固定入口执行完成并验证 {len(before_verification)} 个产物文件"
        ),
        artifact_paths=tuple(before_verification),
        tool_operations=operations,
        validation_evidence={
            "entrypoint": list(execution.entrypoint),
            "entrypoint_exit_code": entrypoint_result.exit_code,
            "contract_path": contract_path,
            "verifier": verifier_evidence,
            "artifact_sha256": before_verification,
        },
    )
