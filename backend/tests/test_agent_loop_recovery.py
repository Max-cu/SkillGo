"""Opt-in repeated-tool guard and one-shot context compaction retry."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.agent_context import project_context
from app.agent_policy import AgentExecutionState
from app.config import settings
from app.context_thinning import (
    ThinContextPolicy,
    compact_transcript_for_retry,
    recovery_thin_policy,
)
from app.model_gateway import ModelGatewayError, ModelResult, is_context_length_error
from app.sandbox_agent_loop import REPEAT_TOOL_NOTE, repeated_tool_signature
from app.sandbox_runtime import SandboxRuntimeError


class _Stop(Exception):
    pass


def _contexts():
    return [{
        "name": "Test",
        "version": "1",
        "root": "/workspace/skill",
        "skill_md": "# Guide",
        "runtime_requirements": {},
    }]


def _enable(monkeypatch, *, enabled: bool, note: int = 3, stop: int = 5):
    from app import sandbox_agent_loop

    monkeypatch.setattr(
        sandbox_agent_loop,
        "settings",
        replace(
            sandbox_agent_loop.settings,
            agent_loop_recovery_enabled=enabled,
            agent_repeat_tool_note=note,
            agent_repeat_tool_stop=stop,
        ),
    )
    return sandbox_agent_loop


def _run(loop, db, job, sandbox, gateway):
    return asyncio.run(loop._run_agent_loop(
        db, job, sandbox,
        skill_contexts=_contexts(),
        gateway=gateway,
        job_cancelled=lambda: False,
    ))


def _job(client, user_headers, slug):
    from app.database import SessionLocal
    from app.models import WorkflowJob
    from test_workflow_jobs import create_version, sandbox_skill_zip

    _, version = create_version(client, user_headers, slug=slug, package=sandbox_skill_zip())
    created = client.post(
        "/api/v1/jobs", headers=user_headers,
        data={"version_id": version["id"], "instruction": "report"},
    ).json()
    return SessionLocal, WorkflowJob, created["id"]


def test_recovery_defaults_are_off_and_stop_is_higher():
    assert settings.agent_loop_recovery_enabled is False
    assert settings.agent_repeat_tool_note == 3
    assert settings.agent_repeat_tool_stop > settings.agent_repeat_tool_note
    assert settings.agent_repeat_tool_stop == 5


def test_repeated_tool_signature_ignores_reason_and_timeout():
    left = {
        "action": "command",
        "argv": ["python3", "run.py"],
        "cwd": "/workspace/skill",
        "reason": "first wording",
        "timeout_seconds": 30,
    }
    right = {**left, "reason": "another wording", "timeout_seconds": 90}
    other = {**left, "argv": ["python3", "other.py"]}
    assert repeated_tool_signature(left) == repeated_tool_signature(right)
    assert repeated_tool_signature(left) != repeated_tool_signature(other)
    assert "command" in repeated_tool_signature(left)


def test_context_length_classifier_ignores_bad_arguments():
    length = ModelGatewayError(
        "MODEL_HTTP_ERROR",
        "Private model returned HTTP 400: maximum context length exceeded",
        {"http_status": 400, "provider_error": {
            "code": "context_length_exceeded",
            "message": "maximum context length exceeded",
        }},
    )
    chinese = ModelGatewayError(
        "MODEL_HTTP_ERROR",
        "Private model returned HTTP 400: 超出上下文窗口",
        {"http_status": 400, "provider_error": {"message": "超出上下文窗口"}},
    )
    too_large = ModelGatewayError(
        "MODEL_HTTP_ERROR",
        "Private model returned HTTP 413",
        {"http_status": 413},
    )
    bad_args = ModelGatewayError(
        "MODEL_HTTP_ERROR",
        "Private model returned HTTP 400: Invalid function.arguments",
        {"http_status": 400, "provider_error": {
            "code": "invalid_tool_arguments",
            "message": "Invalid function.arguments",
        }},
    )
    timeout = ModelGatewayError("MODEL_RESPONSE_TIMEOUT", "deadline")
    assert is_context_length_error(length)
    assert is_context_length_error(chinese)
    assert is_context_length_error(too_large)
    assert not is_context_length_error(bad_args)
    assert not is_context_length_error(timeout)


def test_compact_transcript_shrinks_latest_body_and_keeps_durable_history():
    huge = "UNIQUE_BODY_" + ("x" * 5000)
    payload = {
        "ok": True,
        "path": "/workspace/work/notes.txt",
        "content": huge,
        "reference": False,
        "retained": False,
    }
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "{\"action\":\"read_file\"}"},
        {"role": "user", "content": json.dumps(
            {"tool_result": "read_file", "payload": payload}, ensure_ascii=False)},
    ]
    before = copy.deepcopy(messages)
    policy = recovery_thin_policy(ThinContextPolicy())
    compacted = compact_transcript_for_retry(messages, policy)
    assert messages == before
    assert huge not in compacted[-1]["content"]
    assert "omitted from model context" in compacted[-1]["content"]
    assert compacted[0] is messages[0]
    assert policy.compact_older_tools is True
    assert policy.max_exchanges == 2

    protected = {
        "ok": True,
        "reference": True,
        "retained": True,
        "path": "/workspace/skill/SKILL.md",
        "content": huge,
    }
    pinned = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": json.dumps(
            {"tool_result": "read_file", "payload": protected}, ensure_ascii=False)},
    ]
    assert huge in compact_transcript_for_retry(pinned, policy)[1]["content"]


def test_recovery_projection_fits_when_the_audit_projection_does_not():
    huge = "Y" * 8000
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "read"},
        {"role": "user", "content": json.dumps({
            "tool_result": "read_file",
            "payload": {"ok": True, "path": "/workspace/work/notes.txt", "content": huge},
        })},
    ]
    kwargs = dict(
        checkpoint="{}",
        skill_contexts=[],
        loaded=set(),
        completed=set(),
        max_tokens=2500,
        initial_exchange_reserve=0,
    )
    with pytest.raises(ValueError, match="上下文预算不足"):
        project_context(messages, thin=ThinContextPolicy(), **kwargs)
    policy = recovery_thin_policy(ThinContextPolicy())
    projected = project_context(
        compact_transcript_for_retry(messages, policy), thin=policy, **kwargs)
    rendered = json.dumps(projected, ensure_ascii=False)
    assert huge not in rendered
    assert messages[3]["content"].count("Y") == 8000


def test_flag_off_repeated_tool_stalls_without_a_note(
    client, user_headers, fake_model_gateway, monkeypatch, caplog,
):
    loop = _enable(monkeypatch, enabled=False)
    SessionLocal, WorkflowJob, job_id = _job(client, user_headers, "recovery-off-stall")
    seen = []

    class Gateway:
        connection = SimpleNamespace(context_tokens=48000)

        async def agent_step(self, *, messages):
            seen.append(json.dumps(messages, ensure_ascii=False))
            return ModelResult(
                {"action": "ask_user", "question": "Which units?", "reason": f"try-{len(seen)}"},
                "scripted",
                {},
            )

    from test_orchestration import MemorySandbox

    with caplog.at_level(logging.INFO, logger="app.sandbox_agent_loop"):
        with SessionLocal() as db:
            job = db.get(WorkflowJob, job_id)
            with pytest.raises(SandboxRuntimeError) as exc:
                _run(loop, db, job, MemorySandbox(), Gateway())
    assert exc.value.code == "SANDBOX_AGENT_STALLED"
    assert "same arguments" not in str(exc.value)
    assert len(seen) == 4
    assert all(REPEAT_TOOL_NOTE not in item for item in seen)
    assert any(
        "end timing" in record.message and "model_wait_ms=" in record.message and "tool_exec_ms=" in record.message
        for record in caplog.records
    )


def test_flag_on_notes_then_stops_after_the_higher_threshold(
    client, user_headers, fake_model_gateway, monkeypatch,
):
    loop = _enable(monkeypatch, enabled=True, note=3, stop=5)
    SessionLocal, WorkflowJob, job_id = _job(client, user_headers, "recovery-on-repeat")
    seen = []

    class Gateway:
        connection = SimpleNamespace(context_tokens=48000)

        async def agent_step(self, *, messages):
            seen.append(json.dumps(messages, ensure_ascii=False))
            return ModelResult(
                {"action": "ask_user", "question": "Which units?", "reason": f"try-{len(seen)}"},
                "scripted",
                {},
            )

    from test_orchestration import MemorySandbox

    with SessionLocal() as db:
        job = db.get(WorkflowJob, job_id)
        with pytest.raises(SandboxRuntimeError) as exc:
            _run(loop, db, job, MemorySandbox(), Gateway())
    assert exc.value.code == "SANDBOX_REPEATED_TOOL"
    assert "ask_user" in str(exc.value)
    assert "5 times in a row" in str(exc.value)
    assert len(seen) == 5
    assert REPEAT_TOOL_NOTE not in seen[2]
    assert REPEAT_TOOL_NOTE in seen[3]
    assert REPEAT_TOOL_NOTE in seen[4]


def test_flag_on_different_arguments_reset_the_streak(
    client, user_headers, fake_model_gateway, monkeypatch,
):
    loop = _enable(monkeypatch, enabled=True, note=3, stop=5)
    SessionLocal, WorkflowJob, job_id = _job(client, user_headers, "recovery-on-reset")
    # Six identical fingerprints still trip the cumulative stall. Stay under it.
    questions = ["A", "A", "A", "B", "A", "C"]
    seen = []

    class Gateway:
        connection = SimpleNamespace(context_tokens=48000)

        async def agent_step(self, *, messages):
            seen.append(json.dumps(messages, ensure_ascii=False))
            if len(seen) > len(questions):
                raise _Stop()
            return ModelResult(
                {"action": "ask_user", "question": questions[len(seen) - 1], "reason": "same"},
                "scripted",
                {},
            )

    from test_orchestration import MemorySandbox

    with SessionLocal() as db:
        job = db.get(WorkflowJob, job_id)
        with pytest.raises(_Stop):
            _run(loop, db, job, MemorySandbox(), Gateway())
    assert len(seen) == len(questions) + 1
    assert REPEAT_TOOL_NOTE in seen[3]
    assert REPEAT_TOOL_NOTE not in seen[2]


def test_flag_off_context_length_error_is_not_retried(
    client, user_headers, fake_model_gateway, monkeypatch, caplog,
):
    loop = _enable(monkeypatch, enabled=False)
    SessionLocal, WorkflowJob, job_id = _job(client, user_headers, "recovery-off-context")
    calls = {"n": 0}

    class Gateway:
        connection = SimpleNamespace(context_tokens=48000)

        async def agent_step(self, *, messages):
            calls["n"] += 1
            raise _context_length_error()

    from test_orchestration import MemorySandbox

    with caplog.at_level(logging.INFO, logger="app.sandbox_agent_loop"):
        with SessionLocal() as db:
            job = db.get(WorkflowJob, job_id)
            with pytest.raises(ModelGatewayError) as exc:
                _run(loop, db, job, MemorySandbox(), Gateway())
    assert calls["n"] == 1
    assert exc.value.code == "MODEL_HTTP_ERROR"
    assert not any("compacted context once" in record.message for record in caplog.records)


def test_flag_on_retries_once_after_a_context_length_error(
    client, user_headers, fake_model_gateway, monkeypatch,
):
    loop = _enable(monkeypatch, enabled=True)
    SessionLocal, WorkflowJob, job_id = _job(client, user_headers, "recovery-on-context")
    calls = {"n": 0}

    class Gateway:
        connection = SimpleNamespace(context_tokens=48000)

        async def agent_step(self, *, messages):
            calls["n"] += 1
            await asyncio.sleep(0.03)
            if calls["n"] == 1:
                raise _context_length_error()
            raise _Stop()

    from test_orchestration import MemorySandbox

    with SessionLocal() as db:
        job = db.get(WorkflowJob, job_id)
        with pytest.raises(_Stop):
            _run(loop, db, job, MemorySandbox(), Gateway())
        timing = job.memory.data["timing"]
    assert calls["n"] == 2
    assert timing["model_wait_ms"] >= 40
    assert timing["rounds"] == 1


def test_flag_on_does_not_retry_a_second_context_length_error(
    client, user_headers, fake_model_gateway, monkeypatch,
):
    loop = _enable(monkeypatch, enabled=True)
    SessionLocal, WorkflowJob, job_id = _job(client, user_headers, "recovery-on-context-twice")
    calls = {"n": 0}

    class Gateway:
        connection = SimpleNamespace(context_tokens=48000)

        async def agent_step(self, *, messages):
            calls["n"] += 1
            raise _context_length_error()

    from test_orchestration import MemorySandbox

    with SessionLocal() as db:
        job = db.get(WorkflowJob, job_id)
        with pytest.raises(ModelGatewayError):
            _run(loop, db, job, MemorySandbox(), Gateway())
    assert calls["n"] == 2


def test_flag_on_does_not_retry_ordinary_http_errors(
    client, user_headers, fake_model_gateway, monkeypatch,
):
    loop = _enable(monkeypatch, enabled=True)
    SessionLocal, WorkflowJob, job_id = _job(client, user_headers, "recovery-on-bad-args")
    calls = {"n": 0}

    class Gateway:
        connection = SimpleNamespace(context_tokens=48000)

        async def agent_step(self, *, messages):
            calls["n"] += 1
            raise ModelGatewayError(
                "MODEL_HTTP_ERROR",
                "Private model returned HTTP 400: Invalid function.arguments",
                {"http_status": 400, "provider_error": {
                    "code": "invalid_tool_arguments",
                    "message": "Invalid function.arguments",
                }},
            )

    from test_orchestration import MemorySandbox

    with SessionLocal() as db:
        job = db.get(WorkflowJob, job_id)
        with pytest.raises(ModelGatewayError):
            _run(loop, db, job, MemorySandbox(), Gateway())
    assert calls["n"] == 1


def test_flag_off_budget_overflow_fails_before_the_model(
    client, user_headers, fake_model_gateway, monkeypatch,
):
    loop = _enable(monkeypatch, enabled=False)
    SessionLocal, WorkflowJob, job_id = _job(client, user_headers, "recovery-off-budget")
    calls = {"n": 0}

    class Gateway:
        connection = SimpleNamespace(context_tokens=1000)

        async def agent_step(self, *, messages):
            calls["n"] += 1
            raise AssertionError("model must not be called")

    from test_orchestration import MemorySandbox

    with SessionLocal() as db:
        job = db.get(WorkflowJob, job_id)
        with pytest.raises(SandboxRuntimeError, match="上下文预算不足") as exc:
            _run(loop, db, job, MemorySandbox(), Gateway())
    assert exc.value.code == "AGENT_CONTEXT_LIMIT"
    assert calls["n"] == 0


def test_flag_on_still_fails_when_compaction_cannot_make_the_budget(
    client, user_headers, fake_model_gateway, monkeypatch,
):
    loop = _enable(monkeypatch, enabled=True)
    SessionLocal, WorkflowJob, job_id = _job(client, user_headers, "recovery-on-uncompactable")
    calls = {"n": 0, "compacts": 0}
    original = loop.compact_transcript_for_retry

    def _counting(messages, policy):
        calls["compacts"] += 1
        return original(messages, policy)

    monkeypatch.setattr(loop, "compact_transcript_for_retry", _counting)

    class Gateway:
        connection = SimpleNamespace(context_tokens=1000)

        async def agent_step(self, *, messages):
            calls["n"] += 1
            raise AssertionError("model must not be called")

    from test_orchestration import MemorySandbox

    with SessionLocal() as db:
        job = db.get(WorkflowJob, job_id)
        with pytest.raises(SandboxRuntimeError, match="上下文预算不足") as exc:
            _run(loop, db, job, MemorySandbox(), Gateway())
    assert exc.value.code == "AGENT_CONTEXT_LIMIT"
    assert calls["n"] == 0
    assert calls["compacts"] == 1


def test_flag_on_compacts_a_too_large_tool_result_and_continues(
    client, user_headers, fake_model_gateway, monkeypatch,
):
    loop = _enable(monkeypatch, enabled=False)
    SessionLocal, WorkflowJob, job_id = _job(client, user_headers, "recovery-on-budget-retry")
    from test_orchestration import MemorySandbox

    marker = "UNIQUE_OVERFLOW_MARKER_"
    sandbox = MemorySandbox()
    sandbox.files["/workspace/work/notes.txt"] = (marker + ("z" * 20000)).encode()

    with SessionLocal() as db:
        job = db.get(WorkflowJob, job_id)
        budget = _budget_that_fits_the_opening_turn(loop, job, sandbox)
    _enable(monkeypatch, enabled=True)
    seen = []

    class Gateway:
        connection = SimpleNamespace(context_tokens=budget)

        async def agent_step(self, *, messages):
            seen.append(json.dumps(messages, ensure_ascii=False))
            if len(seen) == 1:
                return ModelResult(
                    {"action": "read_file", "path": "/workspace/work/notes.txt", "reason": "inspect"},
                    "scripted",
                    {},
                )
            raise _Stop()

    with SessionLocal() as db:
        job = db.get(WorkflowJob, job_id)
        with pytest.raises(_Stop):
            _run(loop, db, job, sandbox, Gateway())
        assert job.memory.data["timing"]["tool_exec_ms"] >= 0
        assert job.memory.data["timing"]["model_wait_ms"] >= 0
    assert len(seen) == 2
    assert "omitted from model context" in seen[1]
    assert "z" * 5000 not in seen[1]
    assert marker in seen[1]


def test_flag_off_same_tool_result_fails_instead_of_compacting(
    client, user_headers, fake_model_gateway, monkeypatch,
):
    loop = _enable(monkeypatch, enabled=False)
    SessionLocal, WorkflowJob, job_id = _job(client, user_headers, "recovery-off-budget-retry")
    from test_orchestration import MemorySandbox

    sandbox = MemorySandbox()
    sandbox.files["/workspace/work/notes.txt"] = ("UNIQUE_OVERFLOW_MARKER_" + ("z" * 20000)).encode()
    with SessionLocal() as db:
        job = db.get(WorkflowJob, job_id)
        budget = _budget_that_fits_the_opening_turn(loop, job, sandbox)
    calls = {"n": 0}

    class Gateway:
        connection = SimpleNamespace(context_tokens=budget)

        async def agent_step(self, *, messages):
            calls["n"] += 1
            return ModelResult(
                {"action": "read_file", "path": "/workspace/work/notes.txt", "reason": "inspect"},
                "scripted",
                {},
            )

    with SessionLocal() as db:
        job = db.get(WorkflowJob, job_id)
        with pytest.raises(SandboxRuntimeError, match="上下文预算不足") as exc:
            _run(loop, db, job, sandbox, Gateway())
    assert exc.value.code == "AGENT_CONTEXT_LIMIT"
    assert calls["n"] == 1


def test_repeated_tool_failure_is_stored_on_the_job(
    client, user_headers, fake_model_gateway, monkeypatch,
):
    from app import config, sandbox_worker
    from app.database import SessionLocal
    from app.models import JobStatus, RunStatus, WorkflowJob
    from test_orchestration import MemorySandbox
    from test_workflow_jobs import create_version, sandbox_skill_zip

    loop = _enable(monkeypatch, enabled=True, note=3, stop=5)
    monkeypatch.setattr(config, "settings", replace(config.settings, sandbox_worker_enabled=True))
    monkeypatch.setattr(
        sandbox_worker, "settings",
        replace(sandbox_worker.settings, sandbox_worker_enabled=True, sandbox_worker_max_attempts=1),
    )
    _, version = create_version(client, user_headers, slug="recovery-job-error", package=sandbox_skill_zip())
    created = client.post(
        "/api/v1/jobs", headers=user_headers,
        data={"version_id": version["id"], "instruction": "generate report"},
    ).json()

    class Sandbox(MemorySandbox):
        closed = False

        def __init__(self, *args, **kwargs):
            super().__init__()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            type(self).closed = True

        def put_files(self, files):
            self.files.update({f"/workspace/{path}": data for path, data in files.items()})

    class Gateway:
        connection = SimpleNamespace(context_tokens=48000)

        def for_model(self, name):
            return self

        async def agent_step(self, **kwargs):
            return ModelResult({"action": "ask_user", "question": "Which units?"}, "scripted", {})

    async def environment(*args):
        return {"schema_version": 1, "capabilities": [], "providers": {}}

    monkeypatch.setattr(sandbox_worker, "preflight_environment", environment)
    monkeypatch.setattr(sandbox_worker, "DockerSandbox", Sandbox)
    monkeypatch.setattr(sandbox_worker, "get_model_gateway", Gateway)
    claimed = sandbox_worker._claim_job("recovery-worker")
    assert claimed.job_id == created["id"]
    asyncio.run(sandbox_worker.execute_sandbox_job(claimed.job_id, object(), lease=claimed))
    with SessionLocal() as db:
        job = db.get(WorkflowJob, claimed.job_id)
        assert job.status == JobStatus.FAILED
        assert job.agent_run.status == RunStatus.FAILED
        assert job.error_code == "SANDBOX_REPEATED_TOOL"
        assert "ask_user" in job.error_message
        assert "times in a row" in job.error_message
    assert loop.settings.agent_loop_recovery_enabled is True


def _context_length_error() -> ModelGatewayError:
    return ModelGatewayError(
        "MODEL_HTTP_ERROR",
        "Private model returned HTTP 400: maximum context length exceeded",
        {"http_status": 400, "provider_error": {
            "code": "context_length_exceeded",
            "message": "maximum context length exceeded",
        }},
    )


def _budget_that_fits_the_opening_turn(loop, job, sandbox) -> int:
    """Pick a budget that accepts the opening turn and rejects a 20k tool body."""

    file_tree = asyncio.run(sandbox.list_files("/workspace"))
    messages = loop._agent_messages(job, _contexts(), file_tree)
    state = AgentExecutionState(
        skill_count=1,
        plan_required=True,
        ordered_skills=False,
        loaded_skills={1},
        delivery_only=loop.delivery_only_job(job),
        plan_status_tracking=False,
    )
    policy = loop.resolve_thin_context_policy(loop.settings)

    def fits(budget: int, extra: list | None = None) -> bool:
        try:
            project_context(
                [*messages, *(extra or [])],
                checkpoint=state.checkpoint(),
                skill_contexts=_contexts(),
                loaded={1},
                completed=set(),
                max_tokens=budget,
                tool_tokens=0,
                initial_exchange_reserve=2048 if not extra else 0,
                thin=policy,
            )
        except ValueError:
            return False
        return True

    low, high = 1000, 200_000
    assert not fits(low)
    assert fits(high)
    while low + 1 < high:
        mid = (low + high) // 2
        if fits(mid):
            high = mid
        else:
            low = mid
    opening = high
    huge = json.dumps({
        "tool_result": "read_file",
        "payload": {"ok": True, "path": "/workspace/work/notes.txt", "content": "z" * 20000},
    }, ensure_ascii=False)
    extra = [
        {"role": "assistant", "content": "{\"action\":\"read_file\"}"},
        {"role": "user", "content": huge},
    ]
    budget = opening + 1500
    assert fits(budget)
    assert not fits(budget, extra)
    recovery = recovery_thin_policy(policy)
    compacted = compact_transcript_for_retry([*messages, *extra], recovery)
    project_context(
        compacted,
        checkpoint=state.checkpoint(),
        skill_contexts=_contexts(),
        loaded={1},
        completed=set(),
        max_tokens=budget,
        tool_tokens=0,
        initial_exchange_reserve=0,
        thin=recovery,
    )
    return budget
