from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from app.agent_kernel import AgentSession, ToolCallContext, ToolPipeline


def test_agent_session_is_append_only_and_redacts_large_model_payloads(monkeypatch):
    persisted = []

    def fake_append(db, run, event_type, *, status="running", data=None):
        event = SimpleNamespace(
            sequence=len(persisted) + 1,
            event_type=event_type,
            status=status,
            created_at=datetime.now(UTC),
        )
        persisted.append((event, data))
        return event

    monkeypatch.setattr("app.agent_kernel.append_run_event", fake_append)
    session = AgentSession(object(), SimpleNamespace(id="run-1"))

    session.append(
        "tool.call",
        data={
            "tool": "run_python",
            "code": "print('must not be persisted')",
            "stdout": "private output",
            "path": "/workspace/output/result.txt",
        },
    )
    session.start_turn(2)
    session.finish_turn(2)

    assert session.sequence == 3
    assert [item.session_sequence for item in session.events] == [1, 2, 3]
    assert [item.event_type for item in session.events] == [
        "agent.tool.call",
        "agent.turn.started",
        "agent.turn.finished",
    ]
    assert "code" not in persisted[0][1]
    assert "stdout" not in persisted[0][1]
    assert persisted[0][1]["path"] == "/workspace/output/result.txt"
    assert session.is_idle


def test_tool_pipeline_supports_ordered_before_and_after_hooks():
    pipeline = ToolPipeline()
    calls: list[str] = []

    pipeline.add_before(lambda context: calls.append("before") or {**context.action, "policy": "ok"})
    pipeline.add_after(lambda context, payload: calls.append("after") or {**payload, "observed": True})

    async def run():
        context, error = await pipeline.before(
            ToolCallContext(
                turn=1,
                step=1,
                operation=1,
                name="read_file",
                action={"action": "read_file"},
            )
        )
        assert error is None
        result = await pipeline.after(context, {"ok": True})
        return context, result

    context, result = asyncio.run(run())
    assert context.action["policy"] == "ok"
    assert result == {"ok": True, "observed": True}
    assert calls == ["before", "after"]


def test_tool_pipeline_can_fail_closed_from_a_before_hook():
    pipeline = ToolPipeline()
    pipeline.add_before(lambda context: "blocked by policy")

    async def run():
        return await pipeline.before(
            ToolCallContext(
                turn=1,
                step=1,
                operation=1,
                name="command",
                action={"action": "command"},
            )
        )

    _, error = asyncio.run(run())
    assert error == "blocked by policy"
