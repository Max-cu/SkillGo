"""list_files must survive one transient invalid-output glitch.

Regression for job 4b5fed78 resume: a single gofer/exec hiccup returned
exit 0 with non-JSON stdout and killed the whole recovered task.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.sandbox_runtime import DockerSandbox, SandboxCommandResult, SandboxRuntimeError


def _result(stdout, exit_code=0, stderr=""):
    return SandboxCommandResult(exit_code=exit_code, stdout=stdout, stderr=stderr)


async def _no_sleep(*_a, **_k):
    return None


def _sandbox_with(scripted):
    sb = DockerSandbox.__new__(DockerSandbox)

    async def fake_command(argv, timeout_seconds):
        return scripted.pop(0)

    sb.command = fake_command
    return sb


def test_list_files_retries_after_invalid_json_then_succeeds(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    good = json.dumps([{"path": "/workspace/a.txt", "size": 1, "type": "file"}])
    sb = _sandbox_with([_result(""), _result(good)])
    tree = asyncio.run(sb.list_files("/workspace"))
    assert tree[0]["path"] == "/workspace/a.txt"


def test_list_files_retries_after_nonzero_exit(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    sb = _sandbox_with([
        _result("", exit_code=1, stderr="gofer glitch"),
        _result("", exit_code=1, stderr="gofer glitch"),
        _result("[]"),
    ])
    assert asyncio.run(sb.list_files("/workspace")) == []


def test_list_files_raises_after_three_persistent_failures(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    sb = _sandbox_with([_result("not-json-at-all")] * 3)
    with pytest.raises(SandboxRuntimeError) as exc:
        asyncio.run(sb.list_files("/workspace"))
    assert exc.value.code == "SANDBOX_LIST_FAILED"
    assert "not-json-at-all" in str(exc.value)

