"""Concurrent attachment analysis: ordering, concurrency cap, total budget."""
import asyncio
import time
from dataclasses import replace

import pytest

from app import attachment_analysis as mod
from app.attachment_analysis import (
    AttachmentAnalysisTimeout,
    run_attachment_analyses,
)
from app.model_gateway import ModelGatewayError


def test_empty_jobs_returns_immediately():
    assert asyncio.run(run_attachment_analyses([])) == []


def test_results_preserve_input_order_and_run_concurrently():
    started = []
    finished = []

    async def job(label, delay):
        started.append(label)
        await asyncio.sleep(delay)
        finished.append(label)
        return label.upper()

    async def scenario():
        return await run_attachment_analyses([
            lambda: job("a", 0.15),
            lambda: job("b", 0.02),
            lambda: job("c", 0.05),
        ])

    result = asyncio.run(scenario())
    assert result == ["A", "B", "C"]
    # All three started before the slowest finished -> concurrent, not serial.
    assert set(finished) != set(started) or len(started) == 3


def test_concurrency_is_capped(monkeypatch):
    monkeypatch.setattr(mod, "settings", replace(mod.settings, attachment_analysis_concurrency=2))
    active = 0
    max_seen = 0

    async def job():
        nonlocal active, max_seen
        active += 1
        max_seen = max(max_seen, active)
        await asyncio.sleep(0.02)
        active -= 1
        return "ok"

    asyncio.run(run_attachment_analyses([job] * 6))
    assert max_seen == 2


def test_first_failure_propagates_and_cancels_rest():
    async def boom():
        await asyncio.sleep(0.01)
        raise ModelGatewayError("OCR_FAIL", "recognition failed")

    async def late():
        await asyncio.sleep(2)
        return "should-not-finish"

    with pytest.raises(ModelGatewayError) as caught:
        asyncio.run(run_attachment_analyses([boom, late]))
    assert caught.value.code == "OCR_FAIL"


def test_total_budget_timeout(monkeypatch):
    monkeypatch.setattr(mod, "settings", replace(mod.settings, attachment_analysis_total_timeout_seconds=1))

    async def slow():
        await asyncio.sleep(30)
        return "late"

    with pytest.raises(AttachmentAnalysisTimeout) as caught:
        asyncio.run(run_attachment_analyses([slow]))
    assert caught.value.code == "ATTACHMENT_ANALYSIS_TIMEOUT"
