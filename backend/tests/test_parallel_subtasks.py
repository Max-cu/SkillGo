"""Model-driven spawn_subtasks: isolation, allowlists, shared reads, caps, and the handoff note."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import traceback
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from app.agent_policy import action_fingerprint
from app.agent_tool_specs import sandbox_tools_for_request, spawn_subtasks_tool
from app.config import settings as config_settings
from app.model_gateway import SANDBOX_AGENT_TOOLS, AgentToolCall, ModelGatewayError, ModelResult
from app.model_request_gate import active_lease_count, refresh_lease, release_lease, try_acquire
from app.models import ModelRequestGate, ModelRequestLease
from app import parallel_subtasks
from app.parallel_subtasks import (
    MAIN_MODEL_NEXT_STEP,
    SUBTASK_WORKER_SYSTEM_PROMPT,
    SUBTASK_WORKER_TOOLS,
    backoff_seconds,
    retryable_model_error,
    spawn_subtasks,
)
from app.sandbox_runtime import SandboxCommandResult, SandboxRuntimeError
from app.sandbox_tool_registry import validate_agent_action


VALIDATE = ["python3", "scripts/check.py"]


class FakeSandbox:
    def __init__(self, files=None, fail_argv=None):
        self.files = dict(files or {})
        self.calls = []
        self.fail_argv = [list(argv) for argv in (fail_argv or [])]

    async def write_text(self, path, content):
        self.files[path] = content.encode()

    async def read_text(self, path, offset=0, limit=30000):
        if path not in self.files:
            raise SandboxRuntimeError("SANDBOX_READ_FAILED", f"missing {path}")
        return self.files[path].decode()[offset:offset + limit]

    async def command(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        if argv in self.fail_argv:
            return SandboxCommandResult(1, "", "failed")
        return SandboxCommandResult(0, "ok", "")


def lease_engine(tmp_path: Path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'leases.db').as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    ModelRequestGate.__table__.create(engine)
    ModelRequestLease.__table__.create(engine)
    return engine


def enabled_settings(**overrides):
    values = dict(
        parallel_subtasks_enabled=True,
        parallel_subtasks_job_concurrency=5,
        parallel_subtasks_global_model_concurrency=10,
        parallel_subtasks_max_rounds=6,
        parallel_subtasks_timeout_seconds=30,
        parallel_subtasks_model_retries=3,
        parallel_subtasks_lease_ttl_seconds=30,
    )
    values.update(overrides)
    return replace(config_settings, **values)


def use_settings(monkeypatch, **overrides):
    current = enabled_settings(**overrides)
    monkeypatch.setattr("app.parallel_subtasks.settings", current)
    monkeypatch.setattr("app.sandbox_tool_registry.settings", current)
    monkeypatch.setattr("app.agent_tool_specs.settings", current)
    return current


def task(task_id, instruction, output_paths, **extra):
    item = {
        "task_id": task_id,
        "instruction": instruction,
        "output_paths": list(output_paths),
    }
    if "validation_argv" not in extra:
        item["validation_argv"] = VALIDATE
    item.update(extra)
    if item.get("validation_argv") is None:
        item.pop("validation_argv", None)
    return item


def batch(*tasks, **extra):
    action = {
        "action": "spawn_subtasks",
        "reason": "split the remaining work",
        "cwd": "/workspace/work",
        "tasks": list(tasks),
    }
    action.update(extra)
    return action


def calls_result(actions):
    parsed = []
    raw = []
    for index, action in enumerate(actions):
        call_id = f"c{index}"
        args = {key: value for key, value in action.items() if key != "action"}
        parsed.append(AgentToolCall(call_id, action))
        raw.append({
            "id": call_id,
            "type": "function",
            "function": {"name": action["action"], "arguments": json.dumps(args)},
        })
    return ModelResult(
        output=actions[0],
        model_name="scripted",
        token_usage={"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
        assistant_message={"role": "assistant", "content": None, "tool_calls": raw},
        tool_call_id=parsed[0].id,
        tool_calls=tuple(parsed),
    )


def user_context(messages):
    return json.loads(next(item["content"] for item in messages if item["role"] == "user"))


class ScriptedGateway:
    def __init__(self, handler):
        self.handler = handler
        self.seen = []
        self.active = 0
        self.max_active = 0

    async def agent_step(self, *, messages, tools=None):
        names = [item["function"]["name"] for item in tools]
        assert names == ["read_file", "write_file", "command", "finish_task"]
        assert "spawn_subtasks" not in names
        assert messages[0]["content"] == SUBTASK_WORKER_SYSTEM_PROMPT
        assert messages[0]["role"] == "system"
        self.seen.append(messages)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.02)
            return await self.handler(messages)
        finally:
            self.active -= 1


def successful_handler(content_for):
    async def handler(messages):
        context = user_context(messages)
        assistants = sum(item["role"] == "assistant" for item in messages)
        if assistants:
            return calls_result([{"action": "finish_task", "summary": context["task_id"]}])
        actions = [
            {"action": "write_file", "path": path, "content": content_for(context, path)}
            for path in context["output_paths"]
        ]
        if context.get("validation_argv"):
            actions.append({"action": "command", "argv": context["validation_argv"]})
        actions.append({"action": "finish_task", "summary": context["task_id"]})
        return calls_result(actions)

    return handler


def run(sandbox, action, gateway, job_id, engine):
    return asyncio.run(spawn_subtasks(
        sandbox, action, gateway=gateway, job_id=job_id, engine=engine,
    ))


def test_flag_off_leaves_the_model_tool_list_unchanged(monkeypatch):
    hidden = sandbox_tools_for_request(SANDBOX_AGENT_TOOLS)
    assert hidden is SANDBOX_AGENT_TOOLS
    assert "spawn_subtasks" not in {item["function"]["name"] for item in hidden}
    use_settings(monkeypatch)
    shown = sandbox_tools_for_request(SANDBOX_AGENT_TOOLS)
    assert shown is not SANDBOX_AGENT_TOOLS
    assert len(shown) == len(SANDBOX_AGENT_TOOLS) + 1
    assert shown[-1]["function"]["name"] == "spawn_subtasks"
    assert SANDBOX_AGENT_TOOLS[-1]["function"]["name"] != "spawn_subtasks"
    assert spawn_subtasks_tool()["function"]["parameters"]["required"] == ["tasks", "reason"]
    assert [item["function"]["name"] for item in SUBTASK_WORKER_TOOLS] == [
        "read_file", "write_file", "command", "finish_task",
    ]


def test_framework_source_has_no_skill_domain_vocabulary():
    source = Path(parallel_subtasks.__file__).read_text().lower()
    spec = json.dumps(spawn_subtasks_tool()).lower()
    for word in ("svg", "photo_plan", "image_id", "slide", "powerpoint", "aggregate_checks"):
        assert word not in source
        assert word not in spec


def test_validation_rejects_disabled_flag_bad_paths_and_overlapping_allowlists(monkeypatch):
    action = batch(task("chapter-1", "write the chapter", ["/workspace/out/chapter-1.md"]))
    assert "disabled" in (validate_agent_action(action) or "").lower()
    use_settings(monkeypatch)
    assert validate_agent_action(action) is None
    escaped = batch(task("chapter-1", "write the chapter", ["/workspace/out/../chapter-1.md"]))
    assert validate_agent_action(escaped)
    outside = batch(task("chapter-1", "write the chapter", ["/etc/chapter-1.md"]))
    assert validate_agent_action(outside)
    duplicated = batch(
        task("chapter-1", "one", ["/workspace/out/a.md"]),
        task("chapter-1", "two", ["/workspace/out/b.md"]),
    )
    assert "duplicate task_id" in (validate_agent_action(duplicated) or "")
    overlapped = batch(
        task("chapter-1", "one", ["/workspace/out/a.md", "/workspace/out/shared.md"]),
        task("chapter-2", "two", ["/workspace/out/shared.md", "/workspace/out/b.md"]),
    )
    assert "output allowlists overlap at /workspace/out/shared.md" in (validate_agent_action(overlapped) or "")
    shared_write = batch(
        task("chapter-1", "one", ["/workspace/spec/style.md"]),
        shared_paths=["/workspace/spec/style.md"],
    )
    assert "cannot include output path" in (validate_agent_action(shared_write) or "")
    no_command = batch(task("chapter-1", "one", ["/workspace/out/a.md"], validation_argv=None))
    assert validate_agent_action(no_command) is None
    declared = batch(task("chapter-1", "one", ["/workspace/out/a.md"]), aggregate_checks=[{"argv": VALIDATE}])
    assert "unsupported" in (validate_agent_action(declared) or "")


def test_fingerprint_does_not_store_instructions():
    action = batch(task("chapter-1", "x" * 5000, ["/workspace/out/chapter-1.md"]))
    fingerprint = action_fingerprint(action)
    assert "xxxxx" not in fingerprint
    assert len(fingerprint) < 800


def test_retryable_errors_cover_rate_limits_timeouts_and_5xx():
    limited = ModelGatewayError("MODEL_HTTP_ERROR", "slow down", {"http_status": 429, "retry_after_seconds": 9})
    assert retryable_model_error(limited)
    assert backoff_seconds(0, limited) == 9
    assert retryable_model_error(ModelGatewayError("MODEL_HTTP_ERROR", "down", {"http_status": 503}))
    assert retryable_model_error(ModelGatewayError("MODEL_HTTP_ERROR", "down", {"http_status": 500}))
    assert retryable_model_error(ModelGatewayError("MODEL_RESPONSE_TIMEOUT", "timed out"))
    assert retryable_model_error(ModelGatewayError("MODEL_STREAM_STALLED", "stalled"))
    assert not retryable_model_error(ModelGatewayError("MODEL_HTTP_ERROR", "bad args", {"http_status": 400}))
    assert backoff_seconds(3, ModelGatewayError("MODEL_TRANSPORT_ERROR", "reset")) <= 20


def test_workers_see_only_their_instruction_and_not_the_parent_transcript(monkeypatch, tmp_path):
    use_settings(monkeypatch, parallel_subtasks_job_concurrency=2)
    seen = []

    async def handler(messages):
        seen.append(messages)
        context = user_context(messages)
        blob = json.dumps(messages, ensure_ascii=False)
        assert "PARENT-TRANSCRIPT" not in blob
        if context["task_id"] == "chapter-1":
            assert context["instruction"] == "ONLY-ALPHA"
            assert "ONLY-BETA" not in blob
        else:
            assert context["instruction"] == "ONLY-BETA"
            assert "ONLY-ALPHA" not in blob
        path = context["output_paths"][0]
        return calls_result([
            {"action": "write_file", "path": path, "content": context["instruction"]},
            {"action": "command", "argv": context["validation_argv"]},
            {"action": "finish_task", "summary": context["task_id"]},
        ])

    sandbox = FakeSandbox()
    action = batch(
        task("chapter-1", "ONLY-ALPHA", ["/workspace/out/chapter-1.md"]),
        task("chapter-2", "ONLY-BETA", ["/workspace/out/chapter-2.md"]),
    )
    result = run(sandbox, action, ScriptedGateway(handler), "job-1", lease_engine(tmp_path))
    assert result["ok"] is True
    assert result["tasks_ok"] == ["chapter-1", "chapter-2"]
    assert result["tasks_failed"] == []
    assert sandbox.files["/workspace/out/chapter-1.md"] == b"ONLY-ALPHA"
    assert sandbox.files["/workspace/out/chapter-2.md"] == b"ONLY-BETA"
    assert len(seen) == 2
    assert all(item[1]["role"] == "user" and item[0]["role"] == "system" for item in seen)


def test_child_can_write_only_its_own_allowlist(monkeypatch, tmp_path):
    use_settings(monkeypatch)
    style = "/workspace/spec/style.md"
    sandbox = FakeSandbox({style: b"STYLE-SPEC"})

    async def handler(messages):
        context = user_context(messages)
        assistants = sum(item["role"] == "assistant" for item in messages)
        if assistants == 0:
            return calls_result([
                {"action": "command", "argv": ["python3", "-c", "print(1)"]},
                {"action": "write_file", "path": "/workspace/out/other.md", "content": "nope"},
                {"action": "write_file", "path": style, "content": "overwrite"},
                {"action": "write_file", "path": "/etc/passwd", "content": "nope"},
            ])
        blob = json.dumps(messages)
        assert "COMMAND_NOT_ALLOWED" in blob
        assert blob.count("PATH_NOT_ALLOWED") >= 2
        return calls_result([
            {"action": "write_file", "path": context["output_paths"][0], "content": "chapter"},
            {"action": "command", "argv": context["validation_argv"]},
            {"action": "finish_task", "summary": "done"},
        ])

    result = run(
        sandbox,
        batch(
            task("chapter-1", "write the chapter", ["/workspace/out/chapter-1.md"]),
            shared_paths=[style],
        ),
        ScriptedGateway(handler),
        "job-allow",
        lease_engine(tmp_path),
    )
    assert result["tasks_ok"] == ["chapter-1"]
    assert "/workspace/out/other.md" not in sandbox.files
    assert "/etc/passwd" not in sandbox.files
    assert sandbox.files[style] == b"STYLE-SPEC"
    assert sandbox.calls == [VALIDATE]


def test_shared_read_only_context_is_visible_to_every_child(monkeypatch, tmp_path):
    use_settings(monkeypatch, parallel_subtasks_job_concurrency=2)
    style = "/workspace/spec/style.md"
    sandbox = FakeSandbox({style: b"STYLE-SPEC"})
    seen = []

    async def handler(messages):
        context = user_context(messages)
        assistants = sum(item["role"] == "assistant" for item in messages)
        if assistants == 0:
            assert context["shared_paths"] == [style]
            return calls_result([{"action": "read_file", "path": style}])
        blob = json.dumps(messages, ensure_ascii=False)
        seen.append((context["task_id"], blob))
        assert "STYLE-SPEC" in blob
        if context["task_id"] == "chapter-1":
            assert "ONLY-BETA" not in blob
        else:
            assert "ONLY-ALPHA" not in blob
        return calls_result([
            {"action": "write_file", "path": context["output_paths"][0], "content": context["instruction"]},
            {"action": "command", "argv": context["validation_argv"]},
            {"action": "finish_task", "summary": context["task_id"]},
        ])

    result = run(
        sandbox,
        batch(
            task("chapter-1", "ONLY-ALPHA", ["/workspace/out/chapter-1.md"]),
            task("chapter-2", "ONLY-BETA", ["/workspace/out/chapter-2.md"]),
            shared_paths=[style],
        ),
        ScriptedGateway(handler),
        "job-shared",
        lease_engine(tmp_path),
    )
    assert result["ok"] is True
    assert {task_id for task_id, _blob in seen} == {"chapter-1", "chapter-2"}
    assert sandbox.files[style] == b"STYLE-SPEC"


def test_finished_batch_returns_control_with_a_cross_check_note(monkeypatch, tmp_path):
    use_settings(monkeypatch)
    sandbox = FakeSandbox()
    result = run(
        sandbox,
        batch(
            task("chapter-1", "one", ["/workspace/out/chapter-1.md"]),
            task("chapter-2", "two", ["/workspace/out/chapter-2.md"]),
        ),
        ScriptedGateway(successful_handler(lambda context, path: context["instruction"])),
        "job-return",
        lease_engine(tmp_path),
    )
    assert result["ok"] is True
    assert result["next_step"] == MAIN_MODEL_NEXT_STEP
    assert "cross-subtask checks" in result["hint"]
    assert "before continuing" in result["message"]
    assert "aggregate" not in result
    assert sandbox.calls == [VALIDATE, VALIDATE]
    assert [item["summary"] for item in result["results"]] == ["chapter-1", "chapter-2"]


def test_children_cannot_spawn_further_subtasks(monkeypatch, tmp_path):
    use_settings(monkeypatch)

    async def handler(messages):
        context = user_context(messages)
        assistants = sum(item["role"] == "assistant" for item in messages)
        if assistants == 0:
            return calls_result([{
                "action": "spawn_subtasks",
                "reason": "split again",
                "tasks": [task("nested", "more", ["/workspace/out/nested.md"])],
            }])
        assert "NESTED_SPAWN_FORBIDDEN" in json.dumps(messages)
        return calls_result([
            {"action": "write_file", "path": context["output_paths"][0], "content": "done"},
            {"action": "command", "argv": context["validation_argv"]},
            {"action": "finish_task", "summary": "done"},
        ])

    sandbox = FakeSandbox()
    result = run(
        sandbox,
        batch(task("chapter-1", "write one file", ["/workspace/out/chapter-1.md"])),
        ScriptedGateway(handler),
        "job-nested",
        lease_engine(tmp_path),
    )
    assert result["ok"] is True
    assert "/workspace/out/nested.md" not in sandbox.files


def test_validation_command_is_optional(monkeypatch, tmp_path):
    use_settings(monkeypatch)
    sandbox = FakeSandbox()
    result = run(
        sandbox,
        batch(task("chapter-1", "write without a checker", ["/workspace/out/chapter-1.md"], validation_argv=None)),
        ScriptedGateway(successful_handler(lambda context, path: "plain")),
        "job-optional",
        lease_engine(tmp_path),
    )
    assert result["ok"] is True
    assert sandbox.calls == []
    assert sandbox.files["/workspace/out/chapter-1.md"] == b"plain"
    assert "before continuing" in result["next_step"]


def test_every_declared_output_must_pass_validation(monkeypatch, tmp_path):
    use_settings(monkeypatch)
    sandbox = FakeSandbox()
    paths = ["/workspace/out/chapter-1.md", "/workspace/out/chapter-1.meta.json"]

    async def handler(messages):
        context = user_context(messages)
        assistants = sum(item["role"] == "assistant" for item in messages)
        if assistants == 0:
            return calls_result([
                {"action": "write_file", "path": paths[0], "content": "body"},
                {"action": "command", "argv": context["validation_argv"]},
                {"action": "finish_task"},
            ])
        blob = json.dumps(messages)
        assert "OUTPUT_MISSING" in blob
        return calls_result([
            {"action": "write_file", "path": path, "content": path}
            for path in paths
        ] + [
            {"action": "command", "argv": context["validation_argv"]},
            {"action": "finish_task", "summary": "both"},
        ])

    result = run(
        sandbox,
        batch(task("chapter-1", "write both files", paths)),
        ScriptedGateway(handler),
        "job-outputs",
        lease_engine(tmp_path),
    )
    assert result["ok"] is True
    assert sandbox.files[paths[0]] == paths[0].encode()
    assert sandbox.files[paths[1]] == paths[1].encode()
    assert sandbox.calls == [VALIDATE, VALIDATE]


def test_job_cap_limits_active_subtasks(monkeypatch, tmp_path):
    use_settings(monkeypatch, parallel_subtasks_job_concurrency=2, parallel_subtasks_global_model_concurrency=10)
    gateway = ScriptedGateway(successful_handler(lambda context, path: context["task_id"]))
    tasks = [
        task(f"part-{index}", f"write part {index}", [f"/workspace/out/part-{index}.md"])
        for index in range(1, 5)
    ]
    result = run(FakeSandbox(), batch(*tasks), gateway, "job-cap", lease_engine(tmp_path))
    assert result["ok"] is True
    assert gateway.max_active == 2


def test_global_cap_is_shared_across_jobs_by_the_database(monkeypatch, tmp_path):
    use_settings(monkeypatch, parallel_subtasks_job_concurrency=5, parallel_subtasks_global_model_concurrency=1)
    engine = lease_engine(tmp_path)
    gateway = ScriptedGateway(successful_handler(lambda context, path: context["task_id"]))
    sandbox = FakeSandbox()

    async def two_jobs():
        first = spawn_subtasks(
            sandbox,
            batch(
                task("part-1", "one", ["/workspace/out/part-1.md"]),
                task("part-2", "two", ["/workspace/out/part-2.md"]),
            ),
            gateway=gateway, job_id="job-a", engine=engine,
        )
        second = spawn_subtasks(
            sandbox,
            batch(task("part-3", "three", ["/workspace/out/part-3.md"])),
            gateway=gateway, job_id="job-b", engine=engine,
        )
        return await asyncio.gather(first, second)

    results = asyncio.run(two_jobs())
    assert all(item["ok"] for item in results)
    assert gateway.max_active == 1
    assert active_lease_count(engine) == 0


def test_database_lease_excludes_other_connections(tmp_path):
    engine = lease_engine(tmp_path)
    first = try_acquire(engine, limit=1, ttl_seconds=30, job_id="job-a", holder="worker-a")
    assert first
    assert try_acquire(engine, limit=1, ttl_seconds=30, job_id="job-b", holder="worker-b") is None
    assert refresh_lease(engine, first, ttl_seconds=30) is True
    assert active_lease_count(engine) == 1
    release_lease(engine, first)
    second = try_acquire(engine, limit=1, ttl_seconds=30, job_id="job-b", holder="worker-b")
    assert second and second != first
    release_lease(engine, second)

    expired = try_acquire(engine, limit=1, ttl_seconds=-1, job_id="job-old", holder="worker-old")
    assert expired
    assert active_lease_count(engine) == 0
    reclaimed = try_acquire(engine, limit=1, ttl_seconds=30, job_id="job-new", holder="worker-new")
    assert reclaimed and reclaimed != expired
    release_lease(engine, reclaimed)

    ready = threading.Event()

    def hold():
        token = try_acquire(engine, limit=1, ttl_seconds=30, job_id="job-hold", holder="worker-hold")
        assert token
        ready.set()
        time.sleep(0.25)
        release_lease(engine, token)

    thread = threading.Thread(target=hold)
    thread.start()
    assert ready.wait(2)
    assert try_acquire(engine, limit=1, ttl_seconds=30, job_id="job-wait", holder="worker-wait") is None
    thread.join(2)
    assert try_acquire(engine, limit=1, ttl_seconds=30, job_id="job-wait", holder="worker-wait")


def test_rate_limit_backoff_releases_the_slot_and_retries(monkeypatch, tmp_path):
    use_settings(monkeypatch, parallel_subtasks_model_retries=2)
    engine = lease_engine(tmp_path)
    sleeps = []
    real_sleep = asyncio.sleep

    async def fast_sleep(seconds):
        stack = "".join(traceback.format_stack())
        if "_sleep_backoff" in stack:
            sleeps.append((seconds, active_lease_count(engine)))
        await real_sleep(0)

    monkeypatch.setattr("app.parallel_subtasks.asyncio.sleep", fast_sleep)
    calls = {"count": 0}

    async def handler(messages):
        calls["count"] += 1
        if calls["count"] == 1:
            raise ModelGatewayError("MODEL_HTTP_ERROR", "limited", {"http_status": 429, "retry_after_seconds": 1})
        context = user_context(messages)
        path = context["output_paths"][0]
        return calls_result([
            {"action": "write_file", "path": path, "content": "done"},
            {"action": "command", "argv": context["validation_argv"]},
            {"action": "finish_task", "summary": "done"},
        ])

    result = run(
        FakeSandbox(),
        batch(task("chapter-1", "write it", ["/workspace/out/chapter-1.md"])),
        ScriptedGateway(handler),
        "job-backoff",
        engine,
    )
    assert result["tasks_ok"] == ["chapter-1"]
    assert calls["count"] == 2
    assert sleeps
    assert all(held == 0 for _seconds, held in sleeps)
    assert active_lease_count(engine) == 0


def test_model_retries_belong_to_each_subtask(monkeypatch, tmp_path):
    use_settings(monkeypatch, parallel_subtasks_model_retries=1, parallel_subtasks_job_concurrency=2)
    counts = {"alpha": 0, "beta": 0}

    async def handler(messages):
        context = user_context(messages)
        counts[context["task_id"]] += 1
        if context["task_id"] == "alpha" and counts["alpha"] == 1:
            raise ModelGatewayError("MODEL_HTTP_ERROR", "limited", {"http_status": 429})
        path = context["output_paths"][0]
        return calls_result([
            {"action": "write_file", "path": path, "content": context["task_id"]},
            {"action": "command", "argv": context["validation_argv"]},
            {"action": "finish_task", "summary": context["task_id"]},
        ])

    result = run(
        FakeSandbox(),
        batch(
            task("alpha", "write alpha", ["/workspace/out/alpha.md"]),
            task("beta", "write beta", ["/workspace/out/beta.md"]),
        ),
        ScriptedGateway(handler),
        "job-retry",
        lease_engine(tmp_path),
    )
    assert result["ok"] is True
    assert counts == {"alpha": 2, "beta": 1}


def test_failed_subtask_does_not_cancel_the_others(monkeypatch, tmp_path):
    use_settings(monkeypatch, parallel_subtasks_model_retries=0, parallel_subtasks_job_concurrency=2)

    async def handler(messages):
        context = user_context(messages)
        if context["task_id"] == "alpha":
            raise ModelGatewayError("MODEL_HTTP_ERROR", "bad request", {"http_status": 400})
        path = context["output_paths"][0]
        return calls_result([
            {"action": "write_file", "path": path, "content": "beta"},
            {"action": "command", "argv": context["validation_argv"]},
            {"action": "finish_task", "summary": "beta"},
        ])

    sandbox = FakeSandbox()
    result = run(
        sandbox,
        batch(
            task("alpha", "write alpha", ["/workspace/out/alpha.md"]),
            task("beta", "write beta", ["/workspace/out/beta.md"]),
        ),
        ScriptedGateway(handler),
        "job-partial",
        lease_engine(tmp_path),
    )
    assert result["ok"] is False
    assert result["error_code"] == "SUBTASKS_PARTIAL"
    assert result["tasks_ok"] == ["beta"]
    assert result["tasks_failed"] == [{
        "task_id": "alpha",
        "error_code": "MODEL_HTTP_ERROR",
        "reason": "bad request",
    }]
    assert "/workspace/out/alpha.md" not in sandbox.files
    assert sandbox.files["/workspace/out/beta.md"] == b"beta"
    assert result["metrics"]["attempted"] is True
    assert "before continuing" in result["next_step"]
    assert sandbox.calls == [VALIDATE]


def test_timeout_is_isolated_to_the_slow_subtask(monkeypatch, tmp_path):
    use_settings(monkeypatch, parallel_subtasks_timeout_seconds=1, parallel_subtasks_job_concurrency=2)
    real_sleep = asyncio.sleep

    async def handler(messages):
        context = user_context(messages)
        if context["task_id"] == "slow":
            await real_sleep(5)
        path = context["output_paths"][0]
        return calls_result([
            {"action": "write_file", "path": path, "content": context["task_id"]},
            {"action": "command", "argv": context["validation_argv"]},
            {"action": "finish_task", "summary": context["task_id"]},
        ])

    result = run(
        FakeSandbox(),
        batch(
            task("slow", "take too long", ["/workspace/out/slow.md"]),
            task("fast", "finish quickly", ["/workspace/out/fast.md"]),
        ),
        ScriptedGateway(handler),
        "job-timeout",
        lease_engine(tmp_path),
    )
    assert result["tasks_ok"] == ["fast"]
    assert result["tasks_failed"][0]["task_id"] == "slow"
    assert result["tasks_failed"][0]["error_code"] == "TASK_TIMEOUT"


def test_missing_shared_path_fails_before_model_calls(monkeypatch, tmp_path):
    use_settings(monkeypatch)

    async def handler(messages):
        raise AssertionError("model should not be called")

    result = run(
        FakeSandbox(),
        batch(
            task("chapter-1", "write it", ["/workspace/out/chapter-1.md"]),
            shared_paths=["/workspace/spec/style.md"],
        ),
        ScriptedGateway(handler),
        "job-missing",
        lease_engine(tmp_path),
    )
    assert result["error_code"] == "INPUT_NOT_FOUND"
    assert result["metrics"]["attempted"] is False
    assert "next_step" not in result


def test_missing_task_read_path_does_not_cancel_the_other_subtask(monkeypatch, tmp_path):
    use_settings(monkeypatch, parallel_subtasks_job_concurrency=2)
    calls = {"count": 0}

    async def handler(messages):
        calls["count"] += 1
        context = user_context(messages)
        path = context["output_paths"][0]
        return calls_result([
            {"action": "write_file", "path": path, "content": "beta"},
            {"action": "command", "argv": context["validation_argv"]},
            {"action": "finish_task", "summary": "beta"},
        ])

    result = run(
        FakeSandbox(),
        batch(
            task("alpha", "read notes", ["/workspace/out/alpha.md"], read_paths=["/workspace/work/missing.md"]),
            task("beta", "write beta", ["/workspace/out/beta.md"]),
        ),
        ScriptedGateway(handler),
        "job-input",
        lease_engine(tmp_path),
    )
    assert calls["count"] == 1
    assert result["tasks_ok"] == ["beta"]
    assert result["tasks_failed"][0]["task_id"] == "alpha"
    assert result["tasks_failed"][0]["error_code"] == "INPUT_NOT_FOUND"


def test_agent_loop_dispatches_spawn_subtasks_and_records_job_metrics(client, user_headers, monkeypatch):
    from app.database import SessionLocal
    from app.models import WorkflowJob
    from app.sandbox_agent_loop import _run_agent_loop
    from test_orchestration import MemorySandbox
    from test_workflow_jobs import create_version, sandbox_skill_zip

    use_settings(monkeypatch, parallel_subtasks_model_retries=0, parallel_subtasks_timeout_seconds=30)
    _, version = create_version(client, user_headers, slug="spawn-subtasks", package=sandbox_skill_zip())
    created = client.post(
        "/api/v1/jobs",
        headers=user_headers,
        data={"version_id": version["id"], "instruction": "Build the document PARENT-TRANSCRIPT"},
    ).json()
    sandbox = MemorySandbox()
    sandbox.files["/workspace/skills/deck/brief.md"] = b"shared brief"
    action = batch(
        task("chapter-1", "Write CHAPTER-ONE-ONLY", ["/workspace/out/chapter-1.md"]),
        shared_paths=["/workspace/skills/deck/brief.md"],
        cwd="/workspace/skills/deck",
    )

    class Done(Exception):
        pass

    class Gateway:
        connection = type("Conn", (), {"context_tokens": 48000})()

        def __init__(self):
            self.phase = 0

        async def agent_step(self, *, messages, tools=None):
            if tools is not None:
                blob = json.dumps(messages, ensure_ascii=False)
                assert "PARENT-TRANSCRIPT" not in blob
                context = user_context(messages)
                assert context["shared_paths"] == ["/workspace/skills/deck/brief.md"]
                path = context["output_paths"][0]
                return calls_result([
                    {"action": "write_file", "path": path, "content": context["instruction"]},
                    {"action": "command", "argv": context["validation_argv"]},
                    {"action": "finish_task", "summary": "chapter"},
                ])
            self.phase += 1
            if self.phase == 1:
                return ModelResult({
                    "action": "update_plan",
                    "goal": "write the chapters",
                    "steps": [
                        {"id": "draw", "title": "Draw", "status": "in_progress"},
                        {"id": "verify", "title": "Verify", "status": "pending", "depends_on": ["draw"]},
                    ],
                    "success_criteria": ["chapters exist"],
                    "validation_step_id": "verify",
                }, "scripted", {})
            if self.phase == 2:
                return ModelResult(action, "scripted", {})
            raise Done()

    with SessionLocal() as db:
        job = db.get(WorkflowJob, created["id"])
        with pytest.raises(Done):
            asyncio.run(_run_agent_loop(
                db, job, sandbox,
                skill_contexts=[{
                    "name": "Notes",
                    "version": "1",
                    "root": "/workspace/skills/deck",
                    "skill_md": "# Notes",
                    "runtime_requirements": {},
                }],
                gateway=Gateway(),
                job_cancelled=lambda: False,
            ))
        metrics = job.memory.data["spawn_subtask_metrics"]
        assert metrics["calls"] == 1
        assert metrics["rounds"] >= 1
        assert metrics["model_wait_ms"] >= 0
        assert metrics["tool_exec_ms"] >= 0
        assert sandbox.files["/workspace/out/chapter-1.md"] == b"Write CHAPTER-ONE-ONLY"
