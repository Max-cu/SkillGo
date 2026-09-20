"""In-container command deadline: slow scripts are stopped, not the sandbox.

Regression for job 181841cc: a run_task.py batch exceeded the 300s the model
passed, the deadline killed the whole sandbox and the task FAILED. The
deadline is now enforced by coreutils timeout(1) inside the container, so
the agent receives a recoverable SANDBOX_COMMAND_TIMEOUT with /workspace
intact and resumes from the script's saved batch state.
"""
from __future__ import annotations

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
    bad = _command_result_payload(SandboxCommandResult(2, "", "boom"), timeout_seconds=300)
    assert bad == {"exit_code": 2, "stdout": "", "stderr": "boom"}
