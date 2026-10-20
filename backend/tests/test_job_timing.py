"""Per-job model-wait and tool-run totals."""
import asyncio
import inspect
import logging
import re
from types import SimpleNamespace

import pytest

from app.sandbox_agent_loop import _JobTiming, _run_agent_loop, _run_agent_loop_body


def test_job_timing_sums_model_wait_and_tool_run_and_logs_once(monkeypatch, caplog):
    ticks = iter([1.25, 5.0, 5.5, 8.0, 8.25])
    monkeypatch.setattr("app.sandbox_agent_loop.time.perf_counter", lambda: next(ticks))
    timing = _JobTiming()
    timing.add_model_wait(0.0)
    timing.begin_tool()
    timing.end_tool()
    timing.begin_tool()
    assert timing.end_tool() == 250
    assert timing.end_tool() == 0
    assert timing.model_wait_ms == 1250
    assert timing.tool_run_ms == 750

    with caplog.at_level(logging.INFO, logger="app.sandbox_agent_loop"):
        timing.close("job-7")
    assert "job timing job_id=job-7 model_wait_ms=1250 tool_run_ms=750" in caplog.text


def test_job_timing_folds_an_open_tool_into_the_total(monkeypatch, caplog):
    ticks = iter([2.0, 2.3])
    monkeypatch.setattr("app.sandbox_agent_loop.time.perf_counter", lambda: next(ticks))
    timing = _JobTiming()
    timing.begin_tool()
    with caplog.at_level(logging.INFO, logger="app.sandbox_agent_loop"):
        timing.close("job-9")
    assert timing.tool_run_ms == 300
    assert "job timing job_id=job-9 model_wait_ms=0 tool_run_ms=300" in caplog.text


def test_agent_loop_wires_model_wait_and_tool_run_totals():
    wrapper = inspect.getsource(_run_agent_loop)
    body = inspect.getsource(_run_agent_loop_body)
    assert "job_timing.close" in wrapper
    assert "add_model_wait" in body
    assert "begin_tool" in body
    assert "end_tool" in body


def test_agent_loop_logs_totals_when_the_model_call_fails(client, user_headers, caplog):
    from app.database import SessionLocal
    from app.model_gateway import ModelGatewayError
    from app.models import WorkflowJob
    from test_orchestration import MemorySandbox
    from test_workflow_jobs import create_version, sandbox_skill_zip

    _, version = create_version(client, user_headers, slug="job-timing-log", package=sandbox_skill_zip())
    created = client.post(
        "/api/v1/jobs",
        headers=user_headers,
        data={"version_id": version["id"], "instruction": "report"},
    ).json()

    class Gateway:
        connection = SimpleNamespace(context_tokens=48000)

        async def agent_step(self, *, messages):
            raise ModelGatewayError("MODEL_HTTP_ERROR", "stop", {"http_status": 400})

    with SessionLocal() as db:
        job = db.get(WorkflowJob, created["id"])
        job_id = job.id
        with caplog.at_level(logging.INFO, logger="app.sandbox_agent_loop"):
            with pytest.raises(ModelGatewayError):
                asyncio.run(_run_agent_loop(
                    db,
                    job,
                    MemorySandbox(),
                    skill_contexts=[{
                        "name": "Test",
                        "version": "1",
                        "root": "/workspace/skills/test",
                        "skill_md": "# Report",
                        "runtime_requirements": {},
                    }],
                    gateway=Gateway(),
                    job_cancelled=lambda: False,
                ))
    matched = re.search(
        rf"job timing job_id={job_id} model_wait_ms=(\d+) tool_run_ms=0\b",
        caplog.text,
    )
    assert matched, caplog.text
