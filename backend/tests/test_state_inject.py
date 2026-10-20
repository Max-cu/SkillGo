"""Declared workspace state files and per-job timing totals."""
from __future__ import annotations

import asyncio
import copy
import inspect
import json
import logging
import os
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.agent_context import estimate_tokens, project_context
from app.config import settings
from app.context_thinning import ThinContextPolicy
from app.model_gateway import AgentToolCall, ModelGatewayError, ModelResult
from app.models import utcnow
from app.sandbox_agent_loop import AgentJobCancelled, _job_started_timestamp, release_job_timing
from app.state_inject import (
    MAX_DECLARED_FILES,
    annotate_state_age,
    declared_state_paths,
    evaluate_state_file,
    load_state_file,
)
from test_orchestration import MemorySandbox

PHOTO = "/workspace/_state/photo_plan.json"
NOTES = "/workspace/_state/notes.json"
PHOTO_BODY = json.dumps(
    {
        "version": 1,
        "updated_at": "2026-10-20T00:00:00Z",
        "photos": [
            {"image_id": "image_001", "path": "/workspace/work/photos/a.jpg", "source": "upload"},
        ],
        "assignments": {"1": ["image_001"], "4": ["image_001"]},
    },
    ensure_ascii=False,
)


def _skill(*paths: str, manifest: dict | None = None) -> dict:
    lines = ["---", "name: demo", "description: Declare workspace state."]
    if paths:
        lines.extend(["metadata:", "  state_files:"])
        lines.extend(f"    - {path}" for path in paths)
    lines.extend(["---", "# Guide"])
    context = {
        "name": "Test",
        "version": "1",
        "root": "/workspace/skill",
        "skill_md": "\n".join(lines),
        "runtime_requirements": {},
    }
    if manifest is not None:
        context["manifest"] = manifest
    return context


def _snapshot(path, text, *, mtime=1_700_000_000.5):
    return evaluate_state_file(path=path, text=text, mtime=mtime, read_error=None)


def _project(messages, *, policy, files=None, state_inject=False, diagnostics=None, token_cap=None, budget=500_000):
    return project_context(
        messages,
        checkpoint="{}",
        skill_contexts=[],
        loaded=set(),
        completed=set(),
        max_tokens=budget,
        diagnostics=diagnostics,
        thin=policy,
        state_files=files,
        state_inject=state_inject,
        state_token_cap=token_cap,
    )


def _fast():
    return ThinContextPolicy(mode="fast", max_exchanges=4, compact_older_tools=True, thin_reference_shelf=True)


def _messages():
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "build the deck"},
    ]
    for index in range(6):
        stdout = f"TURN{index}-" + ("design notes " * 80)
        messages.extend([
            {
                "role": "assistant",
                "tool_calls": [{"id": f"call-{index}", "function": {"name": "command", "arguments": "{}"}}],
            },
            {
                "role": "tool",
                "tool_call_id": f"call-{index}",
                "content": json.dumps({"tool_result": "command", "payload": {"stdout": stdout}}),
            },
        ])
    return messages


def test_state_inject_defaults_off():
    assert settings.state_inject_enabled is False


def test_declarations_come_from_skill_metadata_and_manifest():
    skill = _skill("_state/photo_plan.json", manifest={
        "metadata": {"state_files": ["/workspace/_state/notes.json", "../secret.json"]},
        "spec": {"state_files": ["_state/photo_plan.json", "_state/extra.json"]},
        "x-skillgo": {"state_files": ["_state/ignored.json"]},
    })
    bare = {"skill_md": "# Guide"}
    assert declared_state_paths([skill, bare]) == [PHOTO, NOTES, "/workspace/_state/extra.json"]
    assert declared_state_paths([bare]) == []
    assert declared_state_paths([{
        "skill_md": "---\nname: demo\ndescription: demo\nstate_files:\n  - _state/top.json\n---\n# Guide\n",
    }]) == ["/workspace/_state/top.json"]
    rejected = [
        "/etc/passwd",
        "/workspace/input/secret.json",
        "_state/nested/file.json",
        "_state/../photo_plan.json",
        "notes.txt",
        "_state/.hidden.json",
    ]
    assert declared_state_paths([_skill(*rejected, "_state/ok.json")]) == ["/workspace/_state/ok.json"]
    many = [f"_state/file_{index:02d}.json" for index in range(MAX_DECLARED_FILES + 3)]
    assert len(declared_state_paths([_skill(*many)])) == MAX_DECLARED_FILES


def test_declared_files_sit_after_history_and_before_the_last_user_message():
    messages = _messages()
    before = copy.deepcopy(messages)
    photo = _snapshot(PHOTO, PHOTO_BODY)
    notes = _snapshot(NOTES, '{"topic":"alpha"}')
    other = _snapshot(NOTES, '{"topic":"beta"}')
    diagnostics = {}
    baseline = _project(messages, policy=_fast(), diagnostics={})
    projected = _project(
        messages, policy=_fast(), files=[photo, notes], state_inject=True, diagnostics=diagnostics,
    )
    again = _project(messages, policy=_fast(), files=[photo, other], state_inject=True)

    assert messages == before
    assert projected[-1]["content"].startswith("Execution state and active Skill instructions")
    assert projected[-3]["content"].startswith(f"State file {PHOTO}")
    assert projected[-2]["content"].startswith(f"State file {NOTES}")
    assert projected[-3]["content"].endswith(PHOTO_BODY)
    assert projected[-2]["content"].endswith('{"topic":"alpha"}')
    assert "image_001" in projected[-3]["content"]
    assert projected[:-3] == baseline[:-1] == again[:-3]
    assert projected[-1] == baseline[-1]
    assert projected[-2] != again[-2]
    metrics = diagnostics["state_inject"]
    assert set(metrics) == {PHOTO, NOTES}
    assert metrics[PHOTO]["validation"] == "ok"
    assert metrics[PHOTO]["injected_tokens"] == estimate_tokens([projected[-3]])
    assert metrics[PHOTO]["injected_tokens"] > 0
    assert metrics[PHOTO]["file_mtime"] == 1_700_000_000.5
    assert metrics[PHOTO]["stale"] is False
    assert metrics[NOTES]["injected_tokens"] == estimate_tokens([projected[-2]])
    assert "photo_plan" not in diagnostics
    assert diagnostics["request_estimated_tokens"] == estimate_tokens(projected)


def test_audit_mode_and_disabled_flag_do_not_inject():
    messages = _messages()
    files = [_snapshot(PHOTO, PHOTO_BODY)]
    audit_diag, fast_diag, idle_diag = {}, {}, {}
    audit = _project(
        messages, policy=ThinContextPolicy(), files=files, state_inject=True, diagnostics=audit_diag,
    )
    disabled = _project(messages, policy=_fast(), files=files, state_inject=False, diagnostics=fast_diag)
    idle = _project(messages, policy=_fast(), files=[], state_inject=True, diagnostics=idle_diag)
    untouched = _project(messages, policy=ThinContextPolicy())
    fast_plain = _project(messages, policy=_fast())
    assert audit == untouched
    assert disabled == fast_plain == idle
    assert "state_inject" not in audit_diag
    assert "state_inject" not in fast_diag
    assert "state_inject" not in idle_diag
    assert "State file" not in json.dumps(disabled)


def test_missing_file_skips_injection_and_logs(caplog):
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "build"}]
    with caplog.at_level(logging.WARNING, logger="app.state_inject"):
        snapshot = evaluate_state_file(path=NOTES, text=None, mtime=None, read_error=None, job_id="job-1")
    diagnostics = {}
    projected = _project(messages, policy=_fast(), files=[snapshot], state_inject=True, diagnostics=diagnostics)
    assert snapshot["validation"] == "missing"
    assert "State file" not in json.dumps(projected)
    assert diagnostics["state_inject"][NOTES]["validation"] == "missing"
    assert diagnostics["state_inject"][NOTES]["injected_tokens"] == 0
    assert diagnostics["state_inject"][NOTES]["file_mtime"] is None
    assert any(
        record.levelno == logging.WARNING and "validation=missing" in record.message and NOTES in record.message
        for record in caplog.records
    )


def test_unparsed_text_is_injected_verbatim():
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "build"}]
    raw = "{not-json"
    snapshot = _snapshot(NOTES, raw, mtime=12.0)
    diagnostics = {}
    projected = _project(messages, policy=_fast(), files=[snapshot], state_inject=True, diagnostics=diagnostics)
    assert projected[-2]["content"].endswith(raw)
    assert diagnostics["state_inject"][NOTES]["validation"] == "ok"
    assert diagnostics["state_inject"][NOTES]["file_mtime"] == 12.0
    empty = evaluate_state_file(path=NOTES, text="", mtime=12.0, read_error=None)
    assert empty["validation"] == "invalid"
    skipped = _project(messages, policy=_fast(), files=[empty], state_inject=True, diagnostics={})
    assert "State file" not in json.dumps(skipped)


def test_state_file_respects_the_token_cap():
    raw = '{"blob":"' + ("x" * 4000) + '"}'
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "build"}]
    diagnostics = {}
    cap = 320
    projected = _project(
        messages,
        policy=_fast(),
        files=[_snapshot(NOTES, raw)],
        state_inject=True,
        diagnostics=diagnostics,
        token_cap=cap,
    )
    block = projected[-2]["content"]
    assert diagnostics["state_inject"][NOTES]["injected_tokens"] <= cap
    assert diagnostics["state_inject"][NOTES]["injected_tokens"] == estimate_tokens([projected[-2]])
    assert f"[truncated to token cap; re-read {NOTES} for the full file]" in block
    payload = block.split("\n")[-1]
    assert raw.startswith(payload)
    assert len(payload) < len(raw)


def test_missing_state_file_warns_once_per_job_and_path(caplog):
    with caplog.at_level(logging.WARNING, logger="app.state_inject"):
        evaluate_state_file(path=NOTES, text=None, mtime=None, read_error=None, job_id="job-once")
        evaluate_state_file(path=NOTES, text=None, mtime=None, read_error=None, job_id="job-once")
        evaluate_state_file(path=PHOTO, text=None, mtime=None, read_error=None, job_id="job-once")
        evaluate_state_file(path=NOTES, text=None, mtime=None, read_error=None, job_id="job-other")
    missing = [record.message for record in caplog.records if "validation=missing" in record.message]
    assert sum("job-once" in message and NOTES in message for message in missing) == 1
    assert sum("job-once" in message and PHOTO in message for message in missing) == 1
    assert sum("job-other" in message and NOTES in message for message in missing) == 1
    release_job_timing("job-once")
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.state_inject"):
        evaluate_state_file(path=NOTES, text=None, mtime=None, read_error=None, job_id="job-once")
    assert sum("job-once" in record.message and NOTES in record.message for record in caplog.records) == 1
    release_job_timing("job-once")
    release_job_timing("job-other")


def test_naive_utc_job_start_is_independent_of_host_tz():
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Shanghai"
    time.tzset()
    try:
        naive = datetime(2026, 10, 20, 6, 0, 0)
        expected = naive.replace(tzinfo=timezone.utc).timestamp()
        assert naive.timestamp() != expected
        started = _job_started_timestamp(SimpleNamespace(started_at=naive, created_at=None))
        assert started == pytest.approx(expected)
        created = _job_started_timestamp(SimpleNamespace(started_at=None, created_at=naive))
        assert created == pytest.approx(expected)
        aware = naive.replace(tzinfo=timezone.utc)
        assert _job_started_timestamp(
            SimpleNamespace(started_at=aware, created_at=None)
        ) == pytest.approx(expected)
        stripped = utcnow().replace(tzinfo=None)
        assert _job_started_timestamp(
            SimpleNamespace(started_at=stripped, created_at=None)
        ) == pytest.approx(stripped.replace(tzinfo=timezone.utc).timestamp())
        stale = annotate_state_age(_snapshot(NOTES, "{}", mtime=expected - 60), started)
        fresh = annotate_state_age(_snapshot(NOTES, "{}", mtime=expected + 60), started)
        assert stale["stale"] is True
        assert fresh["stale"] is False
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


def test_state_file_older_than_the_job_is_marked_stale():
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "build"}]
    old = annotate_state_age(_snapshot(NOTES, '{"n":1}', mtime=1_000.0), 2_000.0)
    same = annotate_state_age(_snapshot(NOTES, '{"n":1}', mtime=2_000.0), 2_000.0)
    newer = annotate_state_age(_snapshot(NOTES, '{"n":1}', mtime=2_500.0), 2_000.0)
    assert old["stale"] is True
    assert same["stale"] is False
    assert newer["stale"] is False
    diagnostics = {}
    projected = _project(messages, policy=_fast(), files=[old], state_inject=True, diagnostics=diagnostics)
    assert "WARNING: stale state file:" in projected[-2]["content"]
    assert projected[-2]["content"].endswith('{"n":1}')
    assert diagnostics["state_inject"][NOTES]["stale"] is True
    assert diagnostics["state_inject"][NOTES]["file_mtime"] == 1_000.0
    fresh_diag = {}
    _project(messages, policy=_fast(), files=[newer], state_inject=True, diagnostics=fresh_diag)
    assert fresh_diag["state_inject"][NOTES]["stale"] is False


def test_state_file_that_does_not_fit_the_budget_is_skipped():
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "build"}]
    baseline = project_context(
        messages, checkpoint="{}", skill_contexts=[], loaded=set(), completed=set(),
        max_tokens=500_000, thin=_fast(), initial_exchange_reserve=0,
    )
    diagnostics = {}
    projected = project_context(
        messages, checkpoint="{}", skill_contexts=[], loaded=set(), completed=set(),
        max_tokens=estimate_tokens(baseline), thin=_fast(), initial_exchange_reserve=0,
        diagnostics=diagnostics, state_inject=True, state_files=[_snapshot(NOTES, '{"n":1}')],
    )
    assert projected == baseline
    assert diagnostics["state_inject"][NOTES]["skip_reason"] == "budget"
    assert diagnostics["state_inject"][NOTES]["injected_tokens"] == 0


def test_worker_releases_process_timing_after_the_closing_event():
    from app.sandbox_agent_loop import _JOB_TIMING
    from app.sandbox_worker import execute_sandbox_job

    _JOB_TIMING["job-release"] = {"model_wait_ms": 3, "tool_exec_ms": 4, "rounds": 1}
    release_job_timing("job-release")
    assert "job-release" not in _JOB_TIMING
    source = inspect.getsource(execute_sandbox_job)
    assert source.rindex("add_job_event") < source.rindex("release_job_timing")


def test_load_state_file_reads_mtime_and_tolerates_a_missing_file(caplog):
    class Sandbox(MemorySandbox):
        def __init__(self, raw):
            super().__init__()
            if raw is not None:
                self.files[NOTES] = raw.encode()

        async def file_mtime(self, path):
            if path not in self.files:
                raise KeyError(path)
            return 42

    with caplog.at_level(logging.WARNING, logger="app.state_inject"):
        missing = asyncio.run(load_state_file(Sandbox(None), NOTES, job_id="job-2"))
        loaded = asyncio.run(load_state_file(Sandbox('{"topic":"notes"}'), NOTES, job_id="job-2"))
    assert missing["validation"] == "missing"
    assert missing["file_mtime"] is None
    assert loaded["validation"] == "ok"
    assert loaded["file_mtime"] == 42
    assert loaded["text"] == '{"topic":"notes"}'
    assert sum("validation=missing" in record.message for record in caplog.records) == 1


def test_fast_loop_injects_only_declared_state_files(client, user_headers, fake_model_gateway, monkeypatch, caplog):
    from dataclasses import replace

    from app import sandbox_agent_loop
    from app.database import SessionLocal
    from app.models import JobEvent, WorkflowJob
    from sqlalchemy import select
    from test_workflow_jobs import create_version, sandbox_skill_zip

    monkeypatch.setattr(
        sandbox_agent_loop,
        "settings",
        replace(sandbox_agent_loop.settings, orchestration_mode="fast", state_inject_enabled=True),
    )
    _, version = create_version(client, user_headers, slug="state-inject", package=sandbox_skill_zip())
    created = client.post(
        "/api/v1/jobs", headers=user_headers, data={"version_id": version["id"], "instruction": "keep notes"},
    ).json()
    notes_body = '{"topic":"notes","items":[1,2]}'

    class PlanSandbox(MemorySandbox):
        def __init__(self):
            super().__init__()
            self.reads = []
            self.files[PHOTO] = PHOTO_BODY.encode()
            self.files[NOTES] = notes_body.encode()

        async def read_state_file(self, path, *, max_bytes):
            self.reads.append(path)
            if path not in self.files:
                return {"exists": False}
            data = self.files[path]
            if len(data) > max_bytes:
                return {"exists": True, "mtime": 99.0, "error": "too_large"}
            return {"exists": True, "mtime": time.time() + 3600, "content": data.decode()}

    class Gateway:
        connection = SimpleNamespace(context_tokens=48000)

        def __init__(self):
            self.turn = 0
            self.seen = None

        async def agent_step(self, *, messages):
            self.turn += 1
            if self.turn > 1:
                raise ModelGatewayError("MODEL_RESPONSE_TIMEOUT", "stop after the state round")
            self.seen = messages
            call = AgentToolCall(
                id="call_1",
                action={"action": "list_files", "path": "/workspace", "reason": "查看工作区"},
            )
            return ModelResult(
                output=dict(call.action), model_name="test-model", token_usage={}, tool_calls=(call,),
            )

    gateway = Gateway()
    sandbox = PlanSandbox()
    with caplog.at_level(logging.INFO, logger="app.sandbox_agent_loop"):
        with SessionLocal() as db:
            job = db.get(WorkflowJob, created["id"])
            with pytest.raises(ModelGatewayError):
                asyncio.run(sandbox_agent_loop._run_agent_loop(
                    db, job, sandbox,
                    skill_contexts=[_skill("_state/notes.json")],
                    gateway=gateway, job_cancelled=lambda: False,
                ))
            timing = job.memory.data["timing"]
            assert timing["rounds"] == 2
            assert timing["model_wait_ms"] >= 0
            assert timing["tool_exec_ms"] >= 0
    assert sandbox.reads == [NOTES, NOTES]
    assert gateway.seen[-1]["content"].startswith("Execution state and active Skill instructions")
    assert gateway.seen[-2]["content"].startswith(f"State file {NOTES}")
    assert gateway.seen[-2]["content"].endswith(notes_body)
    assert "image_001" not in json.dumps(gateway.seen)
    assert "photo_plan" not in json.dumps(gateway.seen[-2])
    assert any("end timing" in record.message and "rounds=2" in record.message for record in caplog.records)
    with SessionLocal() as db:
        reasoning = db.scalars(select(JobEvent).where(
            JobEvent.job_id == created["id"], JobEvent.event_type == "reasoning",
        ).order_by(JobEvent.sequence)).all()
        tool = db.scalar(select(JobEvent).where(
            JobEvent.job_id == created["id"], JobEvent.event_type == "tool",
        ))
    metrics = reasoning[0].data["context_budget"]["state_inject"][NOTES]
    assert metrics["validation"] == "ok"
    assert metrics["file_mtime"] > time.time()
    assert metrics["injected_tokens"] > 0
    assert metrics["stale"] is False
    assert "photo_plan" not in reasoning[0].data["context_budget"]
    assert "WARNING: stale state file:" not in gateway.seen[-2]["content"]
    assert reasoning[0].data["timing"]["rounds"] == 1
    assert tool.data["timing"]["tool_exec_ms"] >= 0
    assert tool.data["timing"]["model_wait_ms"] >= 0
    assert tool.data["timing"]["rounds"] == 1


def test_cancelled_model_call_counts_toward_model_wait(client, user_headers, fake_model_gateway, monkeypatch):
    from dataclasses import replace

    from app import sandbox_agent_loop
    from app.database import SessionLocal
    from app.models import WorkflowJob
    from test_workflow_jobs import create_version, sandbox_skill_zip

    monkeypatch.setattr(
        sandbox_agent_loop,
        "settings",
        replace(sandbox_agent_loop.settings, orchestration_mode="fast", state_inject_enabled=False),
    )
    original_wait = sandbox_agent_loop._await_model_with_cancellation

    async def fast_wait(operation, job_cancelled, *, poll_seconds=1.0):
        return await original_wait(operation, job_cancelled, poll_seconds=0.01)

    monkeypatch.setattr(sandbox_agent_loop, "_await_model_with_cancellation", fast_wait)
    _, version = create_version(client, user_headers, slug="state-inject-cancel-wait", package=sandbox_skill_zip())
    created = client.post(
        "/api/v1/jobs", headers=user_headers, data={"version_id": version["id"], "instruction": "stop"},
    ).json()
    phase = {"cancel": False}

    class Gateway:
        connection = SimpleNamespace(context_tokens=48000)

        async def agent_step(self, *, messages):
            phase["cancel"] = True
            await asyncio.Event().wait()

    with SessionLocal() as db:
        job = db.get(WorkflowJob, created["id"])
        with pytest.raises(AgentJobCancelled):
            asyncio.run(sandbox_agent_loop._run_agent_loop(
                db, job, MemorySandbox(),
                skill_contexts=[_skill()],
                gateway=Gateway(), job_cancelled=lambda: phase["cancel"],
            ))
        timing = job.memory.data["timing"]
        assert timing["rounds"] == 1
        assert timing["model_wait_ms"] >= 1
        assert created["id"] in sandbox_agent_loop._JOB_TIMING
        release_job_timing(created["id"])
        assert created["id"] not in sandbox_agent_loop._JOB_TIMING
