"""Regression: the agent must be told how to size per-command timeouts.

Job 4b5fed78 died at 120s while running a Skill script that declared
``--budget 240``: the model omitted timeout_seconds, so the platform default
killed the batch mid-step. The tool schemas and system prompt must make the
budget alignment rule impossible to miss.
"""
from __future__ import annotations

import inspect

from app.model_gateway import SANDBOX_AGENT_TOOLS
from app.sandbox_tool_registry import validate_agent_action


def _tool(name: str) -> dict:
    return next(
        item["function"]
        for item in SANDBOX_AGENT_TOOLS
        if item["function"]["name"] == name
    )


def test_command_schema_documents_budget_alignment():
    desc = _tool("command")["parameters"]["properties"]["timeout_seconds"]
    text = desc["description"].lower()
    assert desc["minimum"] == 1 and desc["maximum"] == 900
    assert "--budget 240" in text
    assert "900" in text and "destroys" in text
    assert "--workers" in text


def test_run_python_schema_documents_600_ceiling():
    desc = _tool("run_python")["parameters"]["properties"]["timeout_seconds"]
    text = desc["description"].lower()
    assert desc["minimum"] == 1 and desc["maximum"] == 600
    assert "600" in text


def test_system_prompt_contains_timeout_alignment_rule():
    from app import sandbox_agent_loop

    source = inspect.getsource(sandbox_agent_loop)
    assert "--budget 240" in source
    assert "budget + 30" in source
    assert "hard limit 900" in source and "hard limit 600" in source
    assert "--workers" in source


def base_command(timeout):
    return {
        "action": "command",
        "argv": ["python3", "scripts/run_task.py", "run", "--budget", "240"],
        "cwd": "/workspace/skills/s",
        "timeout_seconds": timeout,
        "reason": "advance batch",
    }


def test_validator_accepts_budget_aligned_timeout():
    assert validate_agent_action(base_command(270)) is None
    assert validate_agent_action(base_command(900)) is None


def test_validator_enforces_900_ceiling_on_command():
    assert validate_agent_action(base_command(901)) is not None
