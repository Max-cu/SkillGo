"""Oversized tool results can be spilled into the job workspace.

The feature is off unless SKILLGO_TOOL_OUTPUT_SPILL is set. These tests lock
the off path to the existing tool-result behavior and the on path to a
head+tail excerpt plus a readable workspace file.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import subprocess
import sys
import tarfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import settings
from app.sandbox_runtime import SandboxRuntimeError, _workspace_path
from app.tool_output_spill import (
    CLEANUP_SCRIPT,
    cleanup_tool_output,
    discard_tool_output_sync,
    drop_tool_output_files,
    head_tail_excerpt,
    is_tool_output_path,
    snapshot_excluded_prefixes,
    spill_tool_result,
    tool_output_path,
)


@contextmanager
def spill_settings(*, enabled: bool, threshold: int | None = None):
    previous_enabled = settings.tool_output_spill_enabled
    previous_threshold = settings.tool_output_spill_bytes
    object.__setattr__(settings, "tool_output_spill_enabled", enabled)
    if threshold is not None:
        object.__setattr__(settings, "tool_output_spill_bytes", threshold)
    try:
        yield
    finally:
        object.__setattr__(settings, "tool_output_spill_enabled", previous_enabled)
        object.__setattr__(settings, "tool_output_spill_bytes", previous_threshold)


class RecordingSandbox:
    def __init__(self):
        self.files: dict[str, str] = {}
        self.writes: list[str] = []

    async def write_text(self, path, content):
        normalized = _workspace_path(path, allow_root=False)
        self.writes.append(normalized)
        self.files[normalized] = content

    async def read_text(self, path, offset=0, limit=60_000):
        normalized = _workspace_path(path, allow_root=False)
        return self.files[normalized][offset:offset + limit]


def _large_command_payload() -> tuple[dict, str]:
    stdout = "START-" + ("middle\n" * 20000) + "-FINAL_TRACEBACK_LINE"
    assert len(stdout.encode()) > 52 * 1024
    return {"exit_code": 1, "stdout": stdout, "stderr": ""}, stdout


def test_spill_flag_defaults_off():
    assert settings.tool_output_spill_enabled is False
    assert settings.tool_output_spill_bytes == 32 * 1024
    assert snapshot_excluded_prefixes() == ()


def test_flag_off_large_command_matches_legacy_offload_and_skips_spill(monkeypatch):
    from app.sandbox_agent_loop import _append_tool_result_with_offload

    def boom(*_args, **_kwargs):
        raise AssertionError("spill must not run while the flag is off")

    monkeypatch.setattr("app.sandbox_agent_loop.spill_tool_result", boom)
    sandbox = RecordingSandbox()
    original, stdout = _large_command_payload()
    messages: list[dict] = []
    with spill_settings(enabled=False):
        result = asyncio.run(_append_tool_result_with_offload(
            messages, SimpleNamespace(), "command", original,
            sandbox=sandbox, turn_number=1, operation_number=1, tool_call_id="a",
        ))
        again_messages: list[dict] = []
        again_sandbox = RecordingSandbox()
        again = asyncio.run(_append_tool_result_with_offload(
            again_messages, SimpleNamespace(), "command", original,
            sandbox=again_sandbox, turn_number=1, operation_number=1, tool_call_id="a",
        ))
    assert result == again
    assert messages == again_messages
    assert sandbox.files == again_sandbox.files
    assert result["truncated"] is True
    assert result["full_result_path"] == "/workspace/work/tool-results/turn-1-op-1.json"
    assert not any("_tool_output" in path for path in sandbox.files)
    stored = json.loads(sandbox.files[result["full_result_path"]])
    assert stored == original
    assert stored["stdout"] == stdout
    clipped = result["stdout"]
    assert "START-" in clipped and "FINAL_TRACEBACK_LINE" in clipped
    assert "bytes omitted" in clipped and result["full_result_path"] in clipped
    assert "_tool_output" not in json.dumps(messages)


def test_flag_off_non_command_keeps_legacy_4kb_tier():
    from app.sandbox_agent_loop import _append_tool_result_with_offload

    sandbox = RecordingSandbox()
    original = {"ok": True, "content": "x" * 8000}
    messages: list[dict] = []
    with spill_settings(enabled=False):
        result = asyncio.run(_append_tool_result_with_offload(
            messages, SimpleNamespace(), "read_skill", original,
            sandbox=sandbox, turn_number=4, operation_number=2, tool_call_id="s",
        ))
    assert result["truncated"] is True
    assert result["full_result_path"] == "/workspace/work/tool-results/turn-4-op-2.json"
    assert result["full_result_path"] in sandbox.files
    assert not any("_tool_output" in path for path in sandbox.files)


def test_flag_off_direct_append_does_not_touch_workspace():
    from app.sandbox_agent_loop import _append_model_tool_result

    sandbox = RecordingSandbox()
    payload = {"ok": True, "stdout": "short"}
    messages: list[dict] = []
    with spill_settings(enabled=False):
        returned = asyncio.run(_append_model_tool_result(
            messages, SimpleNamespace(tool_call_id="c"), "command", payload, tool_call_id="c",
        ))
    assert returned == payload
    assert sandbox.writes == []
    body = json.loads(messages[0]["content"])
    assert body["payload"] == payload
    assert "_tool_output" not in messages[0]["content"]


def test_flag_off_prompt_has_no_spill_instructions():
    from app.sandbox_agent_loop import _agent_messages

    job = SimpleNamespace(
        instruction="report",
        input_files=[],
        memory=None,
        routing_mode="legacy",
        network_enabled=False,
        skill_id="skill",
        selected_skills=[],
    )
    contexts = [{
        "name": "Demo",
        "version": "1",
        "root": "/workspace/skills/demo",
        "skill_md": "# Demo",
        "runtime_requirements": {},
    }]
    with spill_settings(enabled=False):
        system = _agent_messages(job, contexts, [])[0]["content"]
    assert "_tool_output" not in system


def test_tool_output_path_stays_inside_workspace():
    path = tool_output_path(2, 3)
    assert path == "/workspace/_tool_output/2_3.txt"
    assert _workspace_path(path, allow_root=False) == path
    assert is_tool_output_path(path)
    assert not is_tool_output_path("/workspace/_tool_output_extra/1_1.txt")
    assert not is_tool_output_path("/workspace/_tool_output/../output/secret.txt")
    assert not is_tool_output_path("/etc/passwd")
    for bad in ((0, 1), (1, 0), (True, 1), (1, False), (-1, 1), (1, 1_000_001)):
        with pytest.raises(ValueError):
            tool_output_path(*bad)


def test_flag_on_spills_full_text_and_keeps_head_tail_path_and_size():
    from app.sandbox_agent_loop import _append_tool_result_with_offload

    sandbox = RecordingSandbox()
    stdout = "START-" + ("a" * 5000) + "UNIQUE_MIDDLE_TOKEN" + ("b" * 5000) + "-FINAL_TRACEBACK_LINE"
    original = {"exit_code": 1, "stdout": stdout, "stderr": ""}
    messages: list[dict] = []
    with spill_settings(enabled=True, threshold=1024):
        result = asyncio.run(_append_tool_result_with_offload(
            messages, SimpleNamespace(), "command", original,
            sandbox=sandbox, turn_number=3, operation_number=2, tool_call_id="call-1",
        ))
    path = "/workspace/_tool_output/3_2.txt"
    assert result["output_path"] == path
    assert result["bytes"] == len(sandbox.files[path].encode("utf-8"))
    assert result["truncated"] is True
    assert result["exit_code"] == 1
    full = sandbox.files[path]
    assert json.loads(full)["payload"] == original
    assert json.loads(full)["tool_result"] == "command"
    excerpt = result["excerpt"]
    assert "START-" in excerpt and "FINAL_TRACEBACK_LINE" in excerpt
    assert path in excerpt and str(result["bytes"]) in excerpt
    assert "use read_file" in excerpt
    assert "UNIQUE_MIDDLE_TOKEN" in full
    assert "UNIQUE_MIDDLE_TOKEN" not in excerpt
    assert len(excerpt) < len(full)
    restored = asyncio.run(sandbox.read_text(path))
    assert restored == full
    assert stdout in restored
    inline = json.loads(messages[0]["content"])
    assert inline["tool_result"] == "command"
    assert inline["payload"]["output_path"] == path
    assert inline["payload"]["bytes"] == result["bytes"]
    assert "FINAL_TRACEBACK_LINE" in inline["payload"]["excerpt"]
    assert not any(name.startswith("/workspace/work/tool-results/") for name in sandbox.files)
    assert head_tail_excerpt(full, output_path=path, total_bytes=result["bytes"]) == excerpt


def test_flag_on_below_threshold_stays_inline_and_writes_nothing():
    from app.sandbox_agent_loop import _append_tool_result_with_offload

    sandbox = RecordingSandbox()
    original = {"ok": True, "content": "x" * 8000}
    messages: list[dict] = []
    with spill_settings(enabled=True, threshold=32 * 1024):
        result = asyncio.run(_append_tool_result_with_offload(
            messages, SimpleNamespace(), "read_skill", original,
            sandbox=sandbox, turn_number=1, operation_number=1, tool_call_id="s",
        ))
    assert result == original
    assert sandbox.writes == []
    assert json.loads(messages[0]["content"])["payload"]["content"] == original["content"]


def test_flag_on_read_file_and_pinned_reference_stay_inline():
    from app.sandbox_agent_loop import _append_tool_result_with_offload

    sandbox = RecordingSandbox()
    text = "条目" * 6500
    assert len(text.encode("utf-8")) > 32 * 1024
    messages: list[dict] = []
    with spill_settings(enabled=True, threshold=128):
        result = asyncio.run(_append_tool_result_with_offload(
            messages, SimpleNamespace(), "read_file", text,
            sandbox=sandbox, turn_number=1, operation_number=1, tool_call_id="r",
        ))
    assert result == text
    assert sandbox.writes == []
    pinned = {
        "ok": True,
        "path": "/workspace/skills/demo/spec.md",
        "reference": True,
        "retained": True,
        "content": "规范" * 4000,
    }
    with spill_settings(enabled=True, threshold=128):
        kept = asyncio.run(_append_tool_result_with_offload(
            [], SimpleNamespace(), "read_skill", pinned,
            sandbox=sandbox, turn_number=2, operation_number=2, tool_call_id="q",
        ))
    assert kept == pinned
    assert sandbox.writes == []


def test_flag_on_persist_failure_keeps_head_tail_without_raising():
    from app.sandbox_agent_loop import _append_tool_result_with_offload

    class FailingSandbox:
        async def write_text(self, path, content):
            raise SandboxRuntimeError("SANDBOX_WRITE_TOO_LARGE", "boom")

    stdout = "START_MARKER-" + ("middle line\n" * 20000) + "-END_MARKER"
    original = {"exit_code": 0, "stdout": stdout, "stderr": ""}
    messages: list[dict] = []
    with spill_settings(enabled=True, threshold=1024):
        result = asyncio.run(_append_tool_result_with_offload(
            messages, SimpleNamespace(), "command", original,
            sandbox=FailingSandbox(), turn_number=9, operation_number=4, tool_call_id="x",
        ))
    assert result["truncated"] is True
    assert result["persist_failed"] is True
    assert "output_path" not in result
    assert result["exit_code"] == 0
    assert "START_MARKER" in result["excerpt"] and "END_MARKER" in result["excerpt"]
    assert "was not saved" in result["excerpt"]
    assert json.loads(messages[0]["content"])["payload"]["persist_failed"] is True


def test_flag_on_prompt_tells_the_model_how_to_read_the_file():
    from app.sandbox_agent_loop import _agent_messages

    job = SimpleNamespace(
        instruction="report",
        input_files=[],
        memory=None,
        routing_mode="legacy",
        network_enabled=False,
        skill_id="skill",
        selected_skills=[],
    )
    contexts = [{
        "name": "Demo",
        "version": "1",
        "root": "/workspace/skills/demo",
        "skill_md": "# Demo",
        "runtime_requirements": {},
    }]
    with spill_settings(enabled=True):
        system = _agent_messages(job, contexts, [])[0]["content"]
    assert "/workspace/_tool_output/<round>_<call>.txt" in system
    assert "read_file" in system


def test_cleanup_removes_only_the_scratch_directory(tmp_path: Path):
    root = tmp_path / "workspace"
    (root / "_tool_output").mkdir(parents=True)
    (root / "_tool_output" / "1_1.txt").write_text("full output", encoding="utf-8")
    (root / "output").mkdir()
    (root / "output" / "keep.txt").write_text("deliverable", encoding="utf-8")
    (root / "work" / "tool-results").mkdir(parents=True)
    (root / "work" / "tool-results" / "turn-1-op-1.json").write_text("{}", encoding="utf-8")
    (root / "_tool_output_extra").mkdir()
    (root / "_tool_output_extra" / "stay.txt").write_text("stay", encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "-c", CLEANUP_SCRIPT, str(root)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert not (root / "_tool_output").exists()
    assert (root / "output" / "keep.txt").read_text(encoding="utf-8") == "deliverable"
    assert (root / "work" / "tool-results" / "turn-1-op-1.json").read_text(encoding="utf-8") == "{}"
    assert (root / "_tool_output_extra" / "stay.txt").read_text(encoding="utf-8") == "stay"


def test_cleanup_unlinks_symlink_without_following_it(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("keep", encoding="utf-8")
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "_tool_output").symlink_to(outside)
    completed = subprocess.run(
        [sys.executable, "-c", CLEANUP_SCRIPT, str(root)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert not (root / "_tool_output").exists()
    assert (outside / "secret").read_text(encoding="utf-8") == "keep"


def test_cleanup_is_noop_when_flag_off_and_scoped_when_on():
    files = {
        "/workspace/_tool_output/1_1.txt": "big",
        "/workspace/output/report.txt": "ok",
        "/workspace/work/notes.txt": "keep",
    }
    with spill_settings(enabled=False):
        asyncio.run(cleanup_tool_output(SimpleNamespace(files=files, command=None)))
    assert "/workspace/_tool_output/1_1.txt" in files
    with spill_settings(enabled=True):
        removed = drop_tool_output_files(files)
    assert removed == ["/workspace/_tool_output/1_1.txt"]
    assert set(files) == {"/workspace/output/report.txt", "/workspace/work/notes.txt"}


def test_sync_discard_respects_the_flag():
    calls: list[list[str]] = []

    class Container:
        def exec_run(self, argv, user):
            calls.append(list(argv))
            assert user == "10001:10001"
            return SimpleNamespace(exit_code=0)

    sandbox = SimpleNamespace(container=Container(), job_id="job-1")
    with spill_settings(enabled=False):
        discard_tool_output_sync(sandbox)
    assert calls == []
    with spill_settings(enabled=True):
        discard_tool_output_sync(sandbox)
    assert calls and calls[0][0] == "python3"
    assert calls[0][-1] == "/workspace"
    sandbox.container = None
    with spill_settings(enabled=True):
        discard_tool_output_sync(sandbox)
    assert len(calls) == 1


def _tar_with(entries: dict[str, bytes]) -> tarfile.TarFile:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    buffer.seek(0)
    return tarfile.open(fileobj=buffer, mode="r")


def test_snapshot_keeps_spill_files_when_flag_off_and_drops_them_when_on():
    from app.sandbox_checkpoint import _collect_entries

    entries = {
        "workspace/output/report.txt": b"deliverable",
        "workspace/_tool_output/1_1.txt": b"full tool result",
        "workspace/work/notes.txt": b"notes",
    }
    with spill_settings(enabled=False):
        kept, _modes, _dirs = _collect_entries(
            _tar_with(entries),
            excluded_prefixes=snapshot_excluded_prefixes(),
        )
    assert "/workspace/_tool_output/1_1.txt" in kept
    assert kept["/workspace/output/report.txt"] == b"deliverable"
    with spill_settings(enabled=True):
        spilled, _modes, _dirs = _collect_entries(
            _tar_with(entries),
            excluded_prefixes=snapshot_excluded_prefixes(),
        )
    assert "/workspace/_tool_output/1_1.txt" not in spilled
    assert spilled["/workspace/output/report.txt"] == b"deliverable"
    assert spilled["/workspace/work/notes.txt"] == b"notes"


def test_job_timing_log_splits_model_wait_and_tool_run(caplog, client, user_headers, fake_model_gateway):
    from app.database import SessionLocal
    from app.model_gateway import ModelGatewayError, ModelResult
    from app.models import WorkflowJob
    from app.sandbox_agent_loop import _run_agent_loop
    from test_orchestration import MemorySandbox
    from test_workflow_jobs import create_version, sandbox_skill_zip

    _, version = create_version(client, user_headers, slug="spill-timing", package=sandbox_skill_zip())
    created = client.post(
        "/api/v1/jobs",
        headers=user_headers,
        data={"version_id": version["id"], "instruction": "report"},
    ).json()

    class Gateway:
        connection = SimpleNamespace(context_tokens=48000)

        async def agent_step(self, **kwargs):
            await asyncio.sleep(0.02)
            raise ModelGatewayError("MODEL_RESPONSE_TIMEOUT", "deadline")

    sandbox = MemorySandbox()
    sandbox.files["/workspace/_tool_output/1_1.txt"] = b"scratch"
    sandbox.files["/workspace/output/result.txt"] = b"42"
    contexts = [{
        "name": "Test",
        "version": "1",
        "root": "/workspace/skill",
        "skill_md": "# Answer",
        "runtime_requirements": {},
    }]
    caplog.set_level(logging.INFO, logger="app.sandbox_agent_loop")
    with spill_settings(enabled=False):
        with SessionLocal() as db:
            job = db.get(WorkflowJob, created["id"])
            with pytest.raises(ModelGatewayError):
                asyncio.run(_run_agent_loop(
                    db, job, sandbox, skill_contexts=contexts,
                    gateway=Gateway(), job_cancelled=lambda: False,
                ))
            db.rollback()
    timing = [record.getMessage() for record in caplog.records if "timing model_wait_ms=" in record.getMessage()]
    assert len(timing) == 1
    assert f"Sandbox job {created['id']} timing model_wait_ms=" in timing[0]
    assert "tool_run_ms=0" in timing[0]
    wait_ms = int(timing[0].split("model_wait_ms=")[1].split(" ")[0])
    assert wait_ms >= 10
    assert "/workspace/_tool_output/1_1.txt" in sandbox.files
    assert sandbox.files["/workspace/output/result.txt"] == b"42"


def test_job_cleanup_removes_spill_files_only_when_flag_is_on(client, user_headers, fake_model_gateway):
    from app.database import SessionLocal
    from app.model_gateway import ModelResult
    from app.model_gateway import AgentToolCall
    from app.models import WorkflowJob
    from app.sandbox_agent_loop import _run_agent_loop
    from app.sandbox_runtime import SandboxRuntimeError
    from test_orchestration import MemorySandbox
    from test_workflow_jobs import create_version, sandbox_skill_zip

    _, version = create_version(client, user_headers, slug="spill-cleanup", package=sandbox_skill_zip())
    created = client.post(
        "/api/v1/jobs",
        headers=user_headers,
        data={"version_id": version["id"], "instruction": "report"},
    ).json()
    block = AgentToolCall(
        id="call_1",
        action={
            "action": "block",
            "summary": "输入不可用，无法完成目标",
            "evidence": "沙箱没有可执行的检查",
            "reason": "stop",
        },
    )

    class Gateway:
        connection = SimpleNamespace(context_tokens=48000)

        async def agent_step(self, **kwargs):
            return ModelResult(
                output=dict(block.action),
                model_name="test-model",
                token_usage={},
                tool_calls=(block,),
            )

    contexts = [{
        "name": "Test",
        "version": "1",
        "root": "/workspace/skill",
        "skill_md": "# Answer",
        "runtime_requirements": {},
    }]
    sandbox = MemorySandbox()
    sandbox.files["/workspace/_tool_output/2_1.txt"] = b"scratch"
    with spill_settings(enabled=True, threshold=128):
        with SessionLocal() as db:
            job = db.get(WorkflowJob, created["id"])
            with pytest.raises(SandboxRuntimeError):
                asyncio.run(_run_agent_loop(
                    db, job, sandbox, skill_contexts=contexts,
                    gateway=Gateway(), job_cancelled=lambda: False,
                ))
            db.rollback()
    assert "/workspace/_tool_output/2_1.txt" not in sandbox.files
    assert sandbox.files["/workspace/output/result.txt"] == b"42"
