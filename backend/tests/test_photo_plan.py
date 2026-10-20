"""Durable photo-plan injection and per-job timing totals."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
from types import SimpleNamespace

import pytest

from app.agent_context import estimate_tokens, project_context
from app.config import settings
from app.context_thinning import ThinContextPolicy
from app.model_gateway import AgentToolCall, ModelGatewayError, ModelResult
from app.photo_plan import (
    PHOTO_PLAN_PATH,
    evaluate_photo_plan,
    load_photo_plan,
    validate_photo_plan,
)
from test_orchestration import MemorySandbox


def _plan(**overrides):
    document = {
        "version": 1,
        "updated_at": "2026-10-20T00:00:00Z",
        "photos": [
            {"image_id": "image_001", "path": "/workspace/work/photos/a.jpg", "source": "upload"},
            {"image_id": "image_002", "path": "/workspace/work/photos/b.jpg", "source": "library"},
        ],
        "assignments": {"1": ["image_001"], "2": ["image_002"]},
    }
    document.update(overrides)
    return document


def _snapshot(document, *, mtime=1_700_000_000.5):
    return evaluate_photo_plan(text=json.dumps(document), mtime=mtime, read_error=None)


def _project(messages, *, policy, plan=None, state_inject=False, diagnostics=None, token_cap=None, budget=500_000):
    return project_context(
        messages,
        checkpoint="{}",
        skill_contexts=[],
        loaded=set(),
        completed=set(),
        max_tokens=budget,
        diagnostics=diagnostics,
        thin=policy,
        photo_plan=plan,
        state_inject=state_inject,
        photo_plan_token_cap=token_cap,
    )


def _fast():
    return ThinContextPolicy(mode="fast", max_exchanges=4, compact_older_tools=True, thin_reference_shelf=True)


def _messages():
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "build the deck"},
    ]
    for index in range(6):
        stdout = f"TURN{index}-" + ("design_spec photo list " * 80)
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


def test_photo_plan_sits_after_history_and_before_the_last_user_message():
    messages = _messages()
    before = copy.deepcopy(messages)
    plan_a = _snapshot(_plan())
    other = _plan()
    other["assignments"] = {"3": ["image_002"]}
    plan_b = _snapshot(other)
    diagnostics = {}
    baseline = _project(messages, policy=_fast(), diagnostics={})
    projected = _project(messages, policy=_fast(), plan=plan_a, state_inject=True, diagnostics=diagnostics)
    again = _project(messages, policy=_fast(), plan=plan_b, state_inject=True)

    assert messages == before
    assert projected[-1]["content"].startswith("Execution state and active Skill instructions")
    assert projected[-2]["content"].startswith(f"Photo plan from {PHOTO_PLAN_PATH}")
    assert "image_001" in projected[-2]["content"]
    assert "image_002" in projected[-2]["content"]
    assert projected[:-2] == baseline[:-1] == again[:-2]
    assert projected[-1] == baseline[-1]
    assert projected[-2] != again[-2]
    assert diagnostics["photo_plan"]["validation"] == "ok"
    assert diagnostics["photo_plan"]["injected_tokens"] == estimate_tokens([projected[-2]])
    assert diagnostics["photo_plan"]["injected_tokens"] > 0
    assert diagnostics["photo_plan"]["file_mtime"] == 1_700_000_000.5
    assert diagnostics["photo_plan"]["duplicates"] == []
    assert "version=1" in projected[-2]["content"]
    assert "updated_at=2026-10-20T00:00:00Z" in projected[-2]["content"]
    assert diagnostics["request_estimated_tokens"] == estimate_tokens(projected)


def test_audit_mode_and_disabled_flag_do_not_inject():
    messages = _messages()
    plan = _snapshot(_plan())
    audit_diag, fast_diag = {}, {}
    audit = _project(
        messages, policy=ThinContextPolicy(), plan=plan, state_inject=True, diagnostics=audit_diag,
    )
    disabled = _project(messages, policy=_fast(), plan=plan, state_inject=False, diagnostics=fast_diag)
    untouched = _project(messages, policy=ThinContextPolicy())
    assert audit == untouched
    assert "photo_plan" not in audit_diag
    assert "Photo plan from" not in json.dumps(disabled)
    assert "photo_plan" not in fast_diag


def test_missing_file_skips_injection_and_logs(caplog):
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "build"}]
    with caplog.at_level(logging.WARNING, logger="app.photo_plan"):
        snapshot = evaluate_photo_plan(text=None, mtime=None, read_error=None, job_id="job-1")
    diagnostics = {}
    projected = _project(messages, policy=_fast(), plan=snapshot, state_inject=True, diagnostics=diagnostics)
    assert snapshot["validation"] == "missing"
    assert "Photo plan from" not in json.dumps(projected)
    assert diagnostics["photo_plan"]["validation"] == "missing"
    assert diagnostics["photo_plan"]["injected_tokens"] == 0
    assert diagnostics["photo_plan"]["file_mtime"] is None
    assert any(
        record.levelno == logging.WARNING and "validation=missing" in record.message
        for record in caplog.records
    )


def test_invalid_schema_skips_injection_and_keeps_mtime(caplog):
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "build"}]
    cases = [
        "{",
        json.dumps({"photos": [], "assignments": []}),
        json.dumps({"photos": [{"image_id": "image_001", "path": "/workspace/a.jpg"}], "assignments": {}}),
        json.dumps({
            "photos": [
                {"image_id": "image_001", "path": "/workspace/a.jpg", "source": "upload"},
                {"image_id": "image_001", "path": "/workspace/b.jpg", "source": "upload"},
            ],
            "assignments": {},
        }),
        json.dumps({"version": "1", "photos": [], "assignments": {}}),
        json.dumps({
            "photos": [{"image_id": "image_001", "path": "/workspace/../secret.jpg", "source": "upload"}],
            "assignments": {},
        }),
    ]
    with caplog.at_level(logging.WARNING, logger="app.photo_plan"):
        for text in cases:
            snapshot = evaluate_photo_plan(text=text, mtime=12.0, read_error=None)
            assert snapshot["validation"] == "invalid"
            assert snapshot["document"] is None
            assert snapshot["file_mtime"] == 12.0
            diagnostics = {}
            projected = _project(
                messages, policy=_fast(), plan=snapshot, state_inject=True, diagnostics=diagnostics,
            )
            assert "Photo plan from" not in json.dumps(projected)
            assert diagnostics["photo_plan"]["validation"] == "invalid"
            assert diagnostics["photo_plan"]["injected_tokens"] == 0
            assert diagnostics["photo_plan"]["file_mtime"] == 12.0
    assert any(
        record.levelno == logging.WARNING and "validation=invalid" in record.message
        for record in caplog.records
    )


def test_duplicate_assignment_is_a_warning_not_a_schema_error():
    document, error = validate_photo_plan(_plan(assignments={
        "1": ["image_001"],
        "4": ["image_001", "image_002"],
        "9": ["image_999"],
    }))
    assert error is None and document is not None
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "build"}]
    snapshot = _snapshot(document)
    diagnostics = {}
    projected = _project(messages, policy=_fast(), plan=snapshot, state_inject=True, diagnostics=diagnostics)
    body = projected[-2]["content"]
    assert "WARNING: stale/duplicate assignment: image_001 is on pages 1, 4." in body
    assert "WARNING: stale assignment: image_999 on page 9 is not in the photo list." in body
    assert diagnostics["photo_plan"]["validation"] == "ok"
    assert diagnostics["photo_plan"]["duplicates"] == ["image_001"]


def test_photo_plan_respects_the_token_cap():
    photos = [
        {
            "image_id": f"image_{index:03d}",
            "path": f"/workspace/work/photos/{'p' * 80}_{index}.jpg",
            "source": "upload",
        }
        for index in range(1, 41)
    ]
    assignments = {str(index): [f"image_{index:03d}"] for index in range(1, 41)}
    assignments["2"] = ["image_001", "image_002"]
    document, error = validate_photo_plan({"photos": photos, "assignments": assignments})
    assert error is None and document is not None
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "build"}]
    diagnostics = {}
    cap = 320
    projected = _project(
        messages,
        policy=_fast(),
        plan=_snapshot(document),
        state_inject=True,
        diagnostics=diagnostics,
        token_cap=cap,
    )
    block = projected[-2]
    assert diagnostics["photo_plan"]["injected_tokens"] <= cap
    assert diagnostics["photo_plan"]["injected_tokens"] == estimate_tokens([block])
    assert diagnostics["photo_plan"]["injected_tokens"] > 0
    assert "truncated" in block["content"]
    assert "image_001" in block["content"]
    assert "image_040" not in block["content"]
    assert "WARNING: stale/duplicate assignment: image_001" in block["content"]


def test_load_photo_plan_reads_mtime_and_tolerates_a_missing_file(caplog):
    class Sandbox(MemorySandbox):
        def __init__(self, raw):
            super().__init__()
            if raw is not None:
                self.files[PHOTO_PLAN_PATH] = raw.encode()

        async def file_mtime(self, path):
            if path not in self.files:
                raise KeyError(path)
            return 42

    with caplog.at_level(logging.WARNING, logger="app.photo_plan"):
        missing = asyncio.run(load_photo_plan(Sandbox(None), job_id="job-2"))
        invalid = asyncio.run(load_photo_plan(Sandbox("{"), job_id="job-2"))
        loaded = asyncio.run(load_photo_plan(Sandbox(json.dumps(_plan())), job_id="job-2"))
    assert missing["validation"] == "missing"
    assert missing["file_mtime"] is None
    assert invalid["validation"] == "invalid"
    assert invalid["file_mtime"] == 42
    assert loaded["validation"] == "ok"
    assert loaded["file_mtime"] == 42
    assert loaded["document"]["photos"][0]["image_id"] == "image_001"
    assert sum("validation=missing" in record.message for record in caplog.records) == 1


def test_fast_loop_injects_plan_metrics_and_logs_timing(client, user_headers, fake_model_gateway, monkeypatch, caplog):
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
    _, version = create_version(client, user_headers, slug="photo-plan-inject", package=sandbox_skill_zip())
    created = client.post(
        "/api/v1/jobs", headers=user_headers, data={"version_id": version["id"], "instruction": "place photos"},
    ).json()

    class PlanSandbox(MemorySandbox):
        def __init__(self):
            super().__init__()
            self.files[PHOTO_PLAN_PATH] = json.dumps(_plan(assignments={
                "1": ["image_001"],
                "4": ["image_001"],
            })).encode()

        async def read_state_file(self, path, *, max_bytes):
            if path not in self.files:
                return {"exists": False}
            data = self.files[path]
            if len(data) > max_bytes:
                return {"exists": True, "mtime": 99.0, "error": "too_large"}
            return {"exists": True, "mtime": 99.0, "content": data.decode()}

    class Gateway:
        connection = SimpleNamespace(context_tokens=48000)

        def __init__(self):
            self.turn = 0
            self.seen = None

        async def agent_step(self, *, messages):
            self.turn += 1
            if self.turn > 1:
                raise ModelGatewayError("MODEL_RESPONSE_TIMEOUT", "stop after the photo plan round")
            self.seen = messages
            call = AgentToolCall(
                id="call_1",
                action={"action": "list_files", "path": "/workspace", "reason": "查看工作区"},
            )
            return ModelResult(
                output=dict(call.action), model_name="test-model", token_usage={}, tool_calls=(call,),
            )

    gateway = Gateway()
    with caplog.at_level(logging.INFO, logger="app.sandbox_agent_loop"):
        with SessionLocal() as db:
            job = db.get(WorkflowJob, created["id"])
            with pytest.raises(ModelGatewayError):
                asyncio.run(sandbox_agent_loop._run_agent_loop(
                    db, job, PlanSandbox(),
                    skill_contexts=[{
                        "name": "Test", "version": "1", "root": "/workspace/skill",
                        "skill_md": "# Guide", "runtime_requirements": {},
                    }],
                    gateway=gateway, job_cancelled=lambda: False,
                ))
            timing = job.memory.data["timing"]
            assert timing["rounds"] == 2
            assert timing["model_wait_ms"] >= 0
            assert timing["tool_exec_ms"] >= 0
    assert gateway.seen[-1]["content"].startswith("Execution state and active Skill instructions")
    assert gateway.seen[-2]["content"].startswith(f"Photo plan from {PHOTO_PLAN_PATH}")
    assert "WARNING: stale/duplicate assignment: image_001 is on pages 1, 4." in gateway.seen[-2]["content"]
    assert any("end timing" in record.message and "rounds=2" in record.message for record in caplog.records)
    with SessionLocal() as db:
        reasoning = db.scalars(select(JobEvent).where(
            JobEvent.job_id == created["id"], JobEvent.event_type == "reasoning",
        ).order_by(JobEvent.sequence)).all()
        tool = db.scalar(select(JobEvent).where(
            JobEvent.job_id == created["id"], JobEvent.event_type == "tool",
        ))
    assert reasoning[0].data["context_budget"]["photo_plan"]["validation"] == "ok"
    assert reasoning[0].data["context_budget"]["photo_plan"]["file_mtime"] == 99.0
    assert reasoning[0].data["context_budget"]["photo_plan"]["injected_tokens"] > 0
    assert reasoning[0].data["context_budget"]["photo_plan"]["duplicates"] == ["image_001"]
    assert reasoning[0].data["timing"]["rounds"] == 1
    assert tool.data["timing"]["tool_exec_ms"] >= 0
    assert tool.data["timing"]["model_wait_ms"] >= 0
    assert tool.data["timing"]["rounds"] == 1
