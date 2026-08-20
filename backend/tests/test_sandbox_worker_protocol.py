from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.model_gateway import ModelResult
from app.sandbox_runtime import SandboxRuntimeError
from app.sandbox_worker import (
    _action_skill_context,
    _agent_messages,
    _append_tool_result,
    _finish_tool_event,
    _normalize_artifact_paths,
    _preflight_sandbox_binaries,
    _required_sandbox_binaries,
    _safe_tool_event,
    _trim_messages,
    _validate_artifact_content,
    _validate_agent_action,
)


def test_required_sandbox_binaries_are_vendor_neutral_and_normalized():
    contexts = [
        {"runtime_requirements": {"binaries": ["curl", "/usr/bin/JQ", "curl"]}},
        {"runtime_requirements": {"binaries": ["custom-cli"]}},
    ]

    assert _required_sandbox_binaries(contexts) == ["curl", "custom-cli", "jq"]


def test_sandbox_binary_preflight_reports_missing_commands_before_agent_turns():
    class FakeSandbox:
        async def command(self, argv, **kwargs):
            assert argv[0] == "python3"
            assert json.loads(argv[-1]) == ["curl", "vendor-cli"]
            return SimpleNamespace(
                exit_code=2,
                stdout='{"required":["curl","vendor-cli"],"missing":["vendor-cli"]}',
                stderr="",
            )

    contexts = [{"runtime_requirements": {"binaries": ["vendor-cli", "curl"]}}]
    with pytest.raises(SandboxRuntimeError) as caught:
        asyncio.run(_preflight_sandbox_binaries(FakeSandbox(), contexts))

    assert caught.value.code == "SANDBOX_DEPENDENCY_MISSING"
    assert "vendor-cli" in str(caught.value)


def test_block_action_requires_summary_and_evidence():
    assert _validate_agent_action(
        {"action": "block", "summary": "service unavailable", "evidence": "curl exit 6"}
    ) is None
    assert "evidence" in (
        _validate_agent_action({"action": "block", "summary": "service unavailable"}) or ""
    )


def test_tool_event_is_attributed_to_the_skill_workspace_it_touches():
    contexts = [
        {
            "name": "内容校审",
            "version": "0.1.0",
            "root": "/workspace/skills/01-reviewer/reviewer",
            "extract_root": "/workspace/skills/01-reviewer",
        },
        {
            "name": "格式规范化",
            "version": "1.2.0",
            "root": "/workspace/skills/02-format/formatter",
            "extract_root": "/workspace/skills/02-format",
        },
    ]

    assert _action_skill_context(
        {
            "action": "command",
            "argv": ["python3", "scripts/format.py"],
            "cwd": "/workspace/skills/02-format/formatter",
        },
        contexts,
    ) == {"skill_name": "格式规范化", "skill_version": "1.2.0"}


def test_native_tool_result_uses_tool_role_and_call_id():
    messages: list[dict] = []
    result = ModelResult(
        output={"action": "list_files"},
        model_name="test",
        token_usage={},
        assistant_message={"role": "assistant", "content": None, "tool_calls": []},
        tool_call_id="call_123",
    )

    _append_tool_result(messages, result, "list_files", {"ok": True})

    assert messages == [
        {
            "role": "tool",
            "tool_call_id": "call_123",
            "content": '{"tool_result": "list_files", "payload": {"ok": true}}',
        }
    ]


def test_message_compaction_keeps_assistant_tool_pair_together():
    messages: list[dict] = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "job"},
    ]
    for index in range(20):
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": f"reason-{index}",
                    "tool_calls": [{"id": f"call-{index}"}],
                },
                {"role": "tool", "tool_call_id": f"call-{index}", "content": "result"},
            ]
        )

    compacted = _trim_messages(messages)
    tail = compacted[3:]

    assert tail[0]["role"] == "assistant"
    assert tail[1]["role"] == "tool"
    for index, message in enumerate(tail):
        if message["role"] == "tool":
            assert index > 0
            assert tail[index - 1]["role"] == "assistant"
            assert tail[index - 1]["tool_calls"][0]["id"] == message["tool_call_id"]


def test_message_compaction_keeps_all_results_from_one_multi_tool_turn():
    messages: list[dict] = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "job"},
    ]
    for index in range(12):
        call_ids = [f"call-{index}-a", f"call-{index}-b", f"call-{index}-c"]
        messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": call_id} for call_id in call_ids],
            }
        )
        messages.extend(
            {"role": "tool", "tool_call_id": call_id, "content": "result"}
            for call_id in call_ids
        )

    compacted = _trim_messages(messages)
    first_assistant = compacted[3]
    assert first_assistant["role"] == "assistant"
    expected_ids = {item["id"] for item in first_assistant["tool_calls"]}
    actual_ids = {
        compacted[index]["tool_call_id"]
        for index in range(4, min(7, len(compacted)))
        if compacted[index]["role"] == "tool"
    }
    assert actual_ids == expected_ids


def test_message_compaction_includes_trusted_execution_checkpoint():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "job"},
    ]
    for index in range(20):
        messages.extend(
            [
                {"role": "assistant", "content": f"action {index}"},
                {"role": "user", "content": f'{{"tool_result":"x","payload":{index}}}'},
            ]
        )

    compacted = _trim_messages(messages, '{"loaded_skill_indexes":[1]}')

    assert "Trusted execution checkpoint" in compacted[2]["content"]
    assert '"loaded_skill_indexes":[1]' in compacted[2]["content"]


def test_message_compaction_prunes_old_python_source_without_mutating_history():
    old_code = "print('work')\n" * 300
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "job"},
        {
            "role": "assistant",
            "reasoning_content": "private reasoning",
            "tool_calls": [
                {
                    "id": "old-call",
                    "type": "function",
                    "function": {
                        "name": "run_python",
                        "arguments": json.dumps({"code": old_code}),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "old-call", "content": "result"},
        {"role": "assistant", "content": "next"},
        {"role": "user", "content": "next-result"},
        {"role": "assistant", "content": "latest"},
    ]

    compacted = _trim_messages(messages)

    old_assistant = compacted[2]
    assert "reasoning_content" not in old_assistant
    assert "executed code compacted" in old_assistant["tool_calls"][0]["function"]["arguments"]
    assert messages[2]["reasoning_content"] == "private reasoning"
    assert json.loads(messages[2]["tool_calls"][0]["function"]["arguments"])["code"] == old_code


def test_invalid_native_tool_arguments_are_recoverable_before_execution():
    assert (
        _validate_agent_action(
            {
                "action": "read_file",
                "path": "/workspace/input/test.docx",
                "offset": "zero",
                "limit": 30_000,
            }
        )
        == "read_file offset must be an integer"
    )
    assert (
        _validate_agent_action(
            {
                "action": "command",
                "argv": "python3 script.py",
                "cwd": "/workspace/skill",
            }
        )
        == "command argv must be a non-empty string array"
    )


def test_valid_command_action_passes_validation():
    assert (
        _validate_agent_action(
            {
                "action": "command",
                "argv": ["python3", "scripts/extract.py"],
                "cwd": "/workspace/skill",
                "timeout_seconds": 120,
                "reason": "Extract the document",
            }
        )
        is None
    )


def test_valid_run_python_action_passes_validation_and_hides_source_from_event():
    action = {
        "action": "run_python",
        "code": "print('document processed')",
        "args": ["/workspace/input/test.docx"],
        "cwd": "/workspace/skill",
        "timeout_seconds": 180,
        "reason": "一次完成文档校审与验证",
    }

    assert _validate_agent_action(action) is None
    title, detail, data = _safe_tool_event("run_python", action)
    assert title == "执行 Python 工作流"
    assert detail == action["reason"]
    assert data == {"tool": "run_python"}
    assert "document processed" not in str(data)


def test_record_validation_event_does_not_expose_evidence_text():
    action = {
        "action": "record_validation",
        "status": "passed",
        "summary": "Verified",
        "evidence": "private validation detail",
        "checks": ["PARAGRAPHS=4"],
        "reason": "Record verification",
    }

    title, detail, data = _safe_tool_event("record_validation", action)

    assert title == "验证 Skill 结果"
    assert detail == "集中验证通过"
    assert data == {"tool": "record_validation", "status": "passed", "check_count": 1}
    assert "private validation detail" not in str(data)


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("code", "", "non-empty"),
        ("code", "x" * 60_001, "60000"),
        ("args", ["x"] * 17, "at most 16"),
        ("timeout_seconds", 601, "between 1 and 600"),
    ],
    ids=["empty-code", "oversized-code", "too-many-args", "timeout-too-large"],
)
def test_invalid_run_python_action_is_rejected(field, value, expected):
    action = {
        "action": "run_python",
        "code": "print('ok')",
        "args": [],
        "cwd": "/workspace/skill",
        "timeout_seconds": 180,
    }
    action[field] = value

    error = _validate_agent_action(action)

    assert error is not None
    assert expected in error


@pytest.mark.parametrize(
    "argv",
    [
        ["npm", "install", "docx"],
        ["pnpm", "add", "docx"],
        ["pip3", "install", "python-docx"],
        ["python3", "-m", "pip", "install", "python-docx"],
    ],
)
def test_task_local_runtime_dependency_install_is_allowed(argv):
    assert (
        _validate_agent_action(
            {"action": "command", "argv": argv, "cwd": "/workspace/skill"}
        )
        is None
    )


@pytest.mark.parametrize("argv", [["apt-get", "install", "curl"], ["apk", "add", "curl"]])
def test_system_package_install_remains_rejected(argv):
    error = _validate_agent_action(
        {"action": "command", "argv": argv, "cwd": "/workspace/skill"}
    )

    assert error is not None
    assert "system package" in error


@pytest.mark.parametrize("token", ["2>/dev/null", "|", "&&", ">>"])
def test_shell_syntax_in_direct_argv_is_rejected(token):
    error = _validate_agent_action(
        {
            "action": "command",
            "argv": ["find", "/workspace", token],
            "cwd": "/workspace/skill",
        }
    )

    assert error is not None
    assert "without a shell" in error


def test_recoverable_tool_error_keeps_diagnostic_out_of_primary_detail():
    event = SimpleNamespace(status="running", detail="", data={"tool": "command"})

    _finish_tool_event(
        event,
        {
            "exit_code": 1,
            "stderr": "Traceback (most recent call last): hidden diagnostic",
        },
    )

    assert event.status == "failed"
    assert "Traceback" not in event.detail
    assert event.data["recoverable"] is True
    assert "Traceback" in event.data["diagnostic"]


def test_long_inline_command_is_recoverable_before_sandbox_execution():
    error = _validate_agent_action(
        {
            "action": "command",
            "argv": ["python3", "-c", "x" * 4097],
            "cwd": "/workspace/skill",
            "timeout_seconds": 120,
        }
    )

    assert error is not None
    assert "workspace file" in error


def test_artifact_paths_are_normalized_under_output_only():
    assert _normalize_artifact_paths(
        ["output/report.docx", "/workspace/output/work/findings.json"]
    ) == [
        "/workspace/output/report.docx",
        "/workspace/output/work/findings.json",
    ]


@pytest.mark.parametrize(
    "path",
    ["/workspace/input/test.docx", "/workspace/output", "../output/report.docx"],
)
def test_artifact_paths_reject_outside_or_directory(path):
    with pytest.raises(SandboxRuntimeError) as caught:
        _normalize_artifact_paths([path])
    assert caught.value.code == "SANDBOX_ARTIFACT_DENIED"


def test_artifact_content_rejects_invalid_structured_files():
    with pytest.raises(SandboxRuntimeError) as caught:
        _validate_artifact_content("report.docx", b"not a zip")
    assert caught.value.code == "ARTIFACT_CONTENT_INVALID"

    with pytest.raises(SandboxRuntimeError) as caught:
        _validate_artifact_content("issues.json", b"not json")
    assert caught.value.code == "ARTIFACT_CONTENT_INVALID"


def test_multi_skill_agent_prompt_includes_every_root_and_coordination_rules():
    job = SimpleNamespace(
        instruction="审查内容并统一版式",
        input_files=[SimpleNamespace(filename="input.docx", size_bytes=1234)],
    )
    contexts = [
        {
            "name": "内容审查",
            "version": "1.0.0",
            "root": "/workspace/skills/01-review/review",
            "skill_md": "# Review\nFind contradictions.",
            "runtime_requirements": {"runtimes": ["python"]},
        },
        {
            "name": "格式规范",
            "version": "2.0.0",
            "root": "/workspace/skills/02-format/format",
            "skill_md": "# Format\nNormalize Word styles.",
            "runtime_requirements": {"runtimes": ["node"]},
        },
    ]

    messages = _agent_messages(job, contexts, [{"path": "/workspace/input/input.docx"}])

    system = messages[0]["content"]
    assert "内容审查" in system and "格式规范" in system
    assert contexts[0]["root"] in system and contexts[1]["root"] in system
    assert "Do not silently ignore a selected Skill" in system
    assert "one coherent execution plan" in system
    assert "Find contradictions." not in system
    assert "Normalize Word styles." not in system
    assert "call read_skill before using this Skill" in system
    assert "one concentrated verification" in system
    assert "record_validation" in system
    assert "acceptance contract" not in system.casefold()
    assert '"selected_skills"' in messages[1]["content"]


def test_single_skill_agent_prompt_keeps_full_instructions_without_plan_overhead():
    job = SimpleNamespace(
        instruction="Review",
        input_files=[SimpleNamespace(filename="input.docx", size_bytes=1234)],
    )
    context = {
        "name": "Review",
        "version": "1.0.0",
        "root": "/workspace/skills/01-review/review",
        "skill_md": "# Review\nUnique approved procedure.",
        "runtime_requirements": {"runtimes": ["python"]},
    }

    system = _agent_messages(job, [context], [])[0]["content"]

    assert "Unique approved procedure." in system
    assert "In the first reasoning turn call update_plan" in system
    assert "There is no fixed turn target" in system
    assert "A file that merely exists or opens proves only existence" in system
    assert "tool names from another Agent platform" in system
    assert "Word/DOCX generation to run_python with python-docx" in system


def test_record_validation_action_validation_is_capability_neutral():
    assert _validate_agent_action(
        {
            "action": "record_validation",
            "status": "passed",
            "summary": "The task-specific verifier passed",
            "evidence": "pytest: 24 passed and primary-source audit count=8",
            "checks": ["pytest: 24 passed", "primary sources checked: 8"],
            "reason": "Record verified outcomes",
        }
    ) is None
