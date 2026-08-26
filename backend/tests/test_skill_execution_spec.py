from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from app.deterministic_runtime import execute_fixed_skill
from app.sandbox_runtime import SandboxCommandResult, SandboxRuntimeError
from app.skill_execution_spec import SkillExecutionSpecError, fixed_execution_spec


def _manifest(execution: dict) -> dict:
    return {"spec": {"type": "code", "execution": execution}}


def test_fixed_execution_spec_accepts_immutable_argv_contract():
    parsed = fixed_execution_spec(
        _manifest(
            {
                "mode": "fixed",
                "entrypoint": ["python3", "scripts/run.py"],
                "verifier": ["python3", "scripts/verify.py"],
                "timeoutSeconds": 240,
            }
        )
    )

    assert parsed is not None
    assert parsed.entrypoint == ("python3", "scripts/run.py")
    assert parsed.verifier == ("python3", "scripts/verify.py")
    assert parsed.timeout_seconds == 240


@pytest.mark.parametrize(
    "entrypoint",
    [
        "python3 scripts/run.py",
        ["bash", "-lc", "python3 scripts/run.py"],
        ["python3", "scripts/run.py", "&&", "curl"],
        ["python3", "scripts/{{user_input}}.py"],
    ],
)
def test_fixed_execution_spec_rejects_shell_or_interpolated_entrypoints(entrypoint):
    with pytest.raises(SkillExecutionSpecError):
        fixed_execution_spec(_manifest({"mode": "fixed", "entrypoint": entrypoint}))


@dataclass
class FakeSandbox:
    artifacts: dict[str, bytes]
    verifier_mutates: bool = False
    commands: list[list[str]] = field(default_factory=list)
    written: dict[str, str] = field(default_factory=dict)

    async def write_text(self, path: str, content: str) -> None:
        self.written[path] = content

    async def command(self, argv, *, cwd, timeout_seconds):
        self.commands.append(list(argv))
        if any(str(item).endswith("verify.py") for item in argv) and self.verifier_mutates:
            self.artifacts["/workspace/output/result.txt"] = b"changed"
        return SandboxCommandResult(exit_code=0, stdout="CHECKS=3", stderr="")

    async def list_files(self, path):
        return [
            {"path": artifact_path, "type": "file", "size": len(data)}
            for artifact_path, data in sorted(self.artifacts.items())
        ]

    def download_file(self, path):
        return self.artifacts[path]


def test_fixed_execution_runs_approved_entrypoint_and_binds_verifier_to_hashes():
    sandbox = FakeSandbox({"/workspace/output/result.txt": b"finished"})
    spec = fixed_execution_spec(
        _manifest(
            {
                "mode": "fixed",
                "entrypoint": ["python3", "scripts/run.py"],
                "verifier": ["python3", "scripts/verify.py"],
            }
        )
    )
    assert spec is not None

    result = asyncio.run(
        execute_fixed_skill(
            sandbox,
            execution=spec,
            skill_root="/workspace/skills/01-fixed",
            instruction="Produce the requested result",
            input_files=[
                {
                    "filename": "input.txt",
                    "path": "/workspace/input/input.txt",
                    "size_bytes": 5,
                }
            ],
        )
    )

    assert sandbox.commands == [
        ["python3", "scripts/run.py"],
        ["python3", "scripts/verify.py"],
    ]
    assert "/workspace/work/skillgo-job.json" in sandbox.written
    assert result.artifact_paths == ("/workspace/output/result.txt",)
    assert result.validation_evidence["verifier"]["exit_code"] == 0
    assert len(result.validation_evidence["artifact_sha256"]["/workspace/output/result.txt"]) == 64


def test_fixed_verifier_must_not_change_output_bytes():
    sandbox = FakeSandbox(
        {"/workspace/output/result.txt": b"finished"},
        verifier_mutates=True,
    )
    spec = fixed_execution_spec(
        _manifest(
            {
                "mode": "fixed",
                "entrypoint": ["python3", "scripts/run.py"],
                "verifier": ["python3", "scripts/verify.py"],
            }
        )
    )
    assert spec is not None

    with pytest.raises(SandboxRuntimeError) as caught:
        asyncio.run(
            execute_fixed_skill(
                sandbox,
                execution=spec,
                skill_root="/workspace/skills/01-fixed",
                instruction="Produce the requested result",
                input_files=[],
            )
        )

    assert caught.value.code == "FIXED_VERIFIER_MUTATED_OUTPUT"
