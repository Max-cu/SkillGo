"""Regression: the agent must be told how to size per-command timeouts.

Job 4b5fed78 died at 120s while running a Skill script that declared
``--budget 240``: the model omitted timeout_seconds, so the platform default
killed the batch mid-step. The tool schemas and system prompt must make the
budget alignment rule impossible to miss.
"""
from __future__ import annotations

import inspect

from app.config import settings
from app.model_gateway import SANDBOX_AGENT_TOOLS
from app.sandbox_tool_registry import validate_agent_action

BUDGET = settings.sandbox_command_timeout_seconds


def _tool(name: str) -> dict:
    return next(
        item["function"]
        for item in SANDBOX_AGENT_TOOLS
        if item["function"]["name"] == name
    )


def test_command_schema_documents_budget_alignment():
    desc = _tool("command")["parameters"]["properties"]["timeout_seconds"]
    text = desc["description"].lower()
    assert desc["minimum"] == 1 and desc["maximum"] == BUDGET
    assert "--budget 240" in text
    assert str(BUDGET) in text and "preserved" in text
    assert "--workers" in text


def test_run_python_schema_documents_600_ceiling():
    desc = _tool("run_python")["parameters"]["properties"]["timeout_seconds"]
    text = desc["description"].lower()
    assert desc["minimum"] == 1 and desc["maximum"] == 600
    assert "600" in text and "preserved" in text


def test_system_prompt_contains_timeout_alignment_rule():
    from app import sandbox_agent_loop

    source = inspect.getsource(sandbox_agent_loop)
    assert "--budget 240" in source
    assert "budget + 30" in source
    # The ceiling is rendered from settings (no hardcoded 900 in the prompt).
    assert "command hard limit " in source
    assert "{settings.sandbox_command_timeout_seconds}" in source
    assert "up to 600" in source
    assert "--workers" in source
    assert "without" in source.lower() and "destroying the sandbox" in source.lower()


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
    assert validate_agent_action(base_command(BUDGET)) is None


def test_validator_enforces_configured_ceiling_on_command():
    assert validate_agent_action(base_command(BUDGET + 1)) is not None
