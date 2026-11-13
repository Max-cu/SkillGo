"""In-container command deadline: slow scripts are stopped, not the sandbox.

Regression for job 181841cc: a run_task.py batch exceeded the 300s the model
passed, the deadline killed the whole sandbox and the task FAILED. The
deadline is now enforced by coreutils timeout(1) inside the container, so
the agent receives a recoverable SANDBOX_COMMAND_TIMEOUT with /workspace
intact and resumes from the script's saved batch state.
"""
from __future__ import annotations
import asyncio
from dataclasses import replace
from types import SimpleNamespace
import pytest

from app.sandbox_agent_loop import _command_result_payload
from app.sandbox_runtime import (
    SandboxCommandResult,
    TIMEOUT_KILL_EXIT_CODE,
    TIMEOUT_TERM_EXIT_CODE,
    exit_means_timeout,
    wrap_argv_with_timeout,
)


def test_wrap_argv_prepends_coreutils_timeout_deadline():
    wrapped = wrap_argv_with_timeout(["python3", "run_task.py", "run"], 300)
    assert wrapped[0] == "timeout"
    assert "300" in wrapped[1:3]
    assert wrapped[-3:] == ["python3", "run_task.py", "run"]


def test_zero_timeout_keeps_command_unwrapped_and_exit_codes_ordinary():
    argv = ["python3", "long_task.py"]
    assert wrap_argv_with_timeout(argv, 0) == argv
    assert not exit_means_timeout(124, 9000, 0)
    assert not exit_means_timeout(137, 9000, 0)


@pytest.mark.parametrize("budget,requested,effective", [
    (0, None, 0), (0, 0, 0), (0, 9000, 9000), (3000, 9000, 3000),
])
def test_runtime_supports_unlimited_default_and_explicit_deadlines(monkeypatch, budget, requested, effective):
    from app import sandbox_runtime as runtime
    monkeypatch.setattr(runtime, 'settings', replace(runtime.settings, sandbox_command_timeout_seconds=budget))
    calls, waits = [], []
    sandbox = runtime.DockerSandbox.__new__(runtime.DockerSandbox)
    sandbox._ensure_container = lambda: None
    def execute(argv, **kwargs):
        import base64
        import json
        calls.append(argv)
        captured = dict(exit_code=0, stdout=base64.b64encode(b'ok').decode(), stderr='',
                        output_files=None, output_bytes={'stdout': 2, 'stderr': 0}, inline_excerpt=False)
        return SimpleNamespace(exit_code=0, output=(json.dumps(captured).encode(), b''))
    sandbox.container = SimpleNamespace(exec_run=execute)
    async def wait(operation, timeout):
        waits.append(timeout)
        return await operation
    monkeypatch.setattr(runtime.asyncio, 'wait_for', wait)
    result = asyncio.run(sandbox.command(['python3', 'long_task.py'], timeout_seconds=requested))
    assert result.exit_code == 0
    assert calls[0][5:] == wrap_argv_with_timeout(['python3', 'long_task.py'], effective)
    assert waits == [effective + runtime.COMMAND_TRANSPORT_GRACE_SECONDS if effective else None]


def test_pending_command_can_be_cancelled_and_sandbox_is_closed():
    from app.sandbox_agent_loop import _await_model_with_cancellation, AgentJobCancelled
    closed = []
    async def run():
        started = asyncio.Event()
        cancelled = False
        async def operation():
            started.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(_await_model_with_cancellation(
            operation(), lambda: cancelled, poll_seconds=0.001, on_cancel=lambda: closed.append(True)))
        await started.wait()
        cancelled = True
        with pytest.raises(AgentJobCancelled):
            await asyncio.wait_for(task, 1)
    asyncio.run(run())
    assert closed == [True]


def test_unlimited_schema_and_validation_in_fresh_process():
    import os
    import subprocess
    import sys
    from pathlib import Path
    source = """
from app.model_gateway import SANDBOX_AGENT_TOOLS
from app.sandbox_tool_registry import validate_agent_action
from app.agent_protocol import build_execution_protocol
from types import SimpleNamespace
from jsonschema import validate
tool=next(t['function'] for t in SANDBOX_AGENT_TOOLS if t['function']['name']=='command')
action=dict(action='command',argv=['python3','long_task.py'],cwd='/workspace',timeout_seconds=0,reason='run')
validate({k:v for k,v in action.items() if k!='action'},tool['parameters'])
assert validate_agent_action(action) is None
assert validate_agent_action({**action,'timeout_seconds':9000}) is None
assert validate_agent_action({**action,'timeout_seconds':-1}) is not None
protocol=build_execution_protocol(SimpleNamespace(),[dict(name='test',version='1',root='/workspace',skill_md='Run')],delivery_only=True)
assert 'no platform wall-clock deadline' in protocol
"""
    subprocess.run([sys.executable, '-c', source], check=True,
                   cwd=Path(__file__).resolve().parents[1],
                   env={**os.environ, 'SKILLGO_SANDBOX_COMMAND_TIMEOUT_SECONDS': '0'})


def test_exit_124_is_timeout_but_plain_nonzero_is_not():
    assert exit_means_timeout(TIMEOUT_TERM_EXIT_CODE, 12.0, 300)
    assert not exit_means_timeout(1, 12.0, 300)
    assert not exit_means_timeout(0, 300.0, 300)


def test_exit_137_counts_only_when_process_reached_deadline():
    # SIGKILL after the grace period at the deadline.
    assert exit_means_timeout(TIMEOUT_KILL_EXIT_CODE, 300.2, 300)
    # An early 137 is an external/OOM kill, not a timeout.
    assert not exit_means_timeout(TIMEOUT_KILL_EXIT_CODE, 4.5, 300)


def test_timed_out_result_maps_to_recoverable_payload_with_resume_hint():
    result = SandboxCommandResult(
        exit_code=TIMEOUT_TERM_EXIT_CODE,
        stdout="done 3/11 files",
        stderr="",
        timed_out=True,
    )
    payload = _command_result_payload(result, timeout_seconds=300)
    assert payload["ok"] is False
    assert payload["error_code"] == "SANDBOX_COMMAND_TIMEOUT"
    assert "300" in payload["message"] and "intact" in payload["message"]
    assert "resume" in payload["hint"].lower()
    assert payload["stdout"] == "done 3/11 files"


def test_normal_payload_keeps_exit_code_and_no_error_marker():
    ok = _command_result_payload(SandboxCommandResult(0, "ok", ""), timeout_seconds=300)
    assert ok == {"exit_code": 0, "stdout": "ok", "stderr": ""}


def test_silent_nonzero_exit_reports_measured_facts():
    # Facts only: exit code, elapsed vs budget, no-output note. No cause
    # attribution — the model interprets, the platform reports.
    result = SandboxCommandResult(137, "", "", elapsed_seconds=4.5)
    payload = _command_result_payload(result, timeout_seconds=300)
    assert payload["ok"] is False
    assert payload["error_code"] == "SANDBOX_COMMAND_NONZERO_EXIT"
    assert "137" in payload["message"] and "4.5s" in payload["message"]
    assert "not a timeout" in payload["message"]
    assert "no stdout/stderr output" in payload["message"]
    assert "intact" in payload["message"]
    assert payload["elapsed_seconds"] == 4.5
    assert payload["timeout_seconds"] == 300


def test_nonzero_exit_with_output_does_not_claim_silence():
    result = SandboxCommandResult(2, "", "boom", elapsed_seconds=1.2)
    payload = _command_result_payload(result, timeout_seconds=300)
    assert payload["ok"] is False
    assert "no stdout/stderr output" not in payload["message"]
    assert payload["stderr"] == "boom"
