from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any


BINARY_DOCUMENT_SUFFIXES = frozenset(
    {
        ".doc",
        ".docx",
        ".pdf",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".zip",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
    }
)

TOOL_NAMES = frozenset(
    {
        "read_skill",
        "complete_skill",
        "update_plan",
        "record_validation",
        "list_files",
        "read_file",
        "write_file",
        "command",
        "run_python",
        "block",
        "finish",
        "run_verifier",
        "run_fixed_skill",
        "ask_user",
        "inspect_image",
    }
)

WORKSPACE_MUTATING_TOOLS = frozenset({"write_file", "command", "run_python"})
VERIFIER_TOOLS = frozenset({"command", "run_python"})


def validate_agent_action(action: dict[str, Any]) -> str | None:
    """Validate one model-authored tool request before trusted dispatch."""

    action_name = action.get("action")
    if not isinstance(action_name, str) or action_name not in TOOL_NAMES:
        return f"Unknown sandbox tool: {action_name}"
    if "reason" in action and not isinstance(action.get("reason"), str):
        return "reason must be text"
    if action_name == "run_verifier":
        return validate_agent_action({**action, "action": "command"})
    if action_name == "run_fixed_skill":
        return validate_agent_action({**action, "action": "read_skill"})
    if action_name == "ask_user":
        if not isinstance(action.get("question"), str) or not 1 <= len(action['question'].strip()) <= 2000:
            return "question must contain 1-2000 characters"
    if action_name == "inspect_image":
        if not isinstance(action.get("path"), str) or not isinstance(action.get("question"), str) or not action['question'].strip():
            return "inspect_image requires path and question"
    if action_name == "read_skill":
        index = action.get("skill_index")
        if not isinstance(index, int) or isinstance(index, bool) or index < 1:
            return "read_skill skill_index must be a positive integer"
    elif action_name == "complete_skill":
        index = action.get("skill_index")
        if not isinstance(index, int) or isinstance(index, bool) or index < 1:
            return "complete_skill skill_index must be a positive integer"
        if not isinstance(action.get("evidence"), str) or not action.get(
            "evidence", ""
        ).strip():
            return "complete_skill evidence must be non-empty text"
    elif action_name == "update_plan":
        if not isinstance(action.get("goal"), str):
            return "update_plan goal must be text"
        if not isinstance(action.get("steps"), list):
            return "update_plan steps must be an array"
        for step in action['steps']:
            if not isinstance(step, dict):
                return "update_plan steps must contain objects"
            for key in ('depends_on', 'input_refs', 'output_refs'):
                values = step.get(key, [])
                if not isinstance(values, list) or not all(isinstance(value, str) and value for value in values):
                    return f"update_plan {key} must be a string array"
        if not isinstance(action.get("success_criteria"), list):
            return "update_plan success_criteria must be an array"
        if not isinstance(action.get("validation_step_id"), str):
            return "update_plan validation_step_id must be text"
    elif action_name == "record_validation":
        if action.get("status") not in {"passed", "failed"}:
            return "record_validation status must be passed or failed"
        for field in ("summary", "evidence"):
            if not isinstance(action.get(field), str) or not action.get(field, "").strip():
                return f"record_validation {field} must be non-empty text"
        checks = action.get("checks")
        if not isinstance(checks, list) or not 1 <= len(checks) <= 200:
            return "record_validation checks must contain 1-200 items"
        if not all(isinstance(item, str) and item.strip() for item in checks):
            return "record_validation checks must contain non-empty text"
    elif action_name == "list_files":
        if "path" in action and not isinstance(action.get("path"), str):
            return "list_files path must be text"
    elif action_name == "read_file":
        if not isinstance(action.get("path"), str) or not action.get("path"):
            return "read_file path must be a non-empty string"
        for field in ("offset", "limit"):
            value = action.get(field)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool)
            ):
                return f"read_file {field} must be an integer"
        if isinstance(action.get("offset"), int) and action["offset"] < 0:
            return "read_file offset must be at least 0"
        if isinstance(action.get("limit"), int) and not 1 <= action["limit"] <= 30_000:
            return "read_file limit must be between 1 and 30000"
    elif action_name == "write_file":
        if not isinstance(action.get("path"), str) or not action.get("path"):
            return "write_file path must be a non-empty string"
        if not isinstance(action.get("content"), str):
            return "write_file content must be text"
    elif action_name == "command":
        argv = action.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(item, str) and item for item in argv)
        ):
            return "command argv must be a non-empty string array"
        if len(argv) > 64:
            return "command argv may contain at most 64 items"
        if any(len(item) > 4096 for item in argv):
            return (
                "one command argument exceeds 4096 characters; write long code or content "
                "to a workspace file first, then run the file"
            )
        program = PurePosixPath(argv[0]).name.casefold()
        command_words = [item.casefold() for item in argv[1:4]]
        system_package_install = (
            program in {"apt", "apt-get", "apk"}
            and bool(command_words)
            and command_words[0] in {"add", "install"}
        )
        if system_package_install:
            return (
                "system package installation is disabled; use task-local pip/npm dependencies "
                "inside /workspace or an approved Skill script"
            )
        shell_tokens = {
            "|",
            "||",
            "&&",
            ";",
            "&",
            ">",
            ">>",
            "<",
            "<<",
            "2>",
            "2>>",
            "2>&1",
        }
        if any(
            item in shell_tokens
            or item.startswith((">/", ">>/", "1>/", "1>>/", "2>/", "2>>/"))
            for item in argv[1:]
        ):
            return (
                "command argv is executed directly without a shell; remove pipes/redirections "
                "and use Python APIs or separate tool calls"
            )
        if "cwd" in action and not isinstance(action.get("cwd"), str):
            return "command cwd must be text"
        timeout = action.get("timeout_seconds")
        if timeout is not None and (
            not isinstance(timeout, int) or isinstance(timeout, bool)
        ):
            return "command timeout_seconds must be an integer"
        if isinstance(timeout, int) and not 1 <= timeout <= 300:
            return "command timeout_seconds must be between 1 and 300"
    elif action_name == "run_python":
        code = action.get("code")
        if not isinstance(code, str) or not code.strip():
            return "run_python code must be non-empty text"
        if len(code) > 60_000:
            return "run_python code may contain at most 60000 characters"
        args = action.get("args", [])
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            return "run_python args must be a string array"
        if len(args) > 16 or any(len(item) > 1000 for item in args):
            return "run_python args may contain at most 16 items of 1000 characters"
        if "cwd" in action and not isinstance(action.get("cwd"), str):
            return "run_python cwd must be text"
        timeout = action.get("timeout_seconds")
        if timeout is not None and (
            not isinstance(timeout, int) or isinstance(timeout, bool)
        ):
            return "run_python timeout_seconds must be an integer"
        if isinstance(timeout, int) and not 1 <= timeout <= 600:
            return "run_python timeout_seconds must be between 1 and 600"
    elif action_name == "block":
        for field in ("summary", "evidence"):
            if not isinstance(action.get(field), str) or not action.get(field, "").strip():
                return f"block {field} must be non-empty text"
    elif action_name == "finish":
        if not isinstance(action.get("summary"), str):
            return "finish summary must be text"
        artifacts = action.get("artifacts")
        if not isinstance(artifacts, list) or not all(
            isinstance(item, str) and item for item in artifacts
        ):
            return "finish artifacts must be a string array"
    return None


def normalize_agent_action(action: dict[str, Any]) -> dict[str, Any]:
    """Only unambiguous shape repairs; never discard checks or infer evidence."""
    normalized = dict(action)
    if action.get('action') == 'update_plan' and isinstance(action.get('success_criteria'), str):
        normalized['success_criteria'] = [action['success_criteria']]
    if action.get('action') == 'record_validation' and isinstance(action.get('checks'), str):
        normalized['checks'] = [action['checks']]
    return normalized


_validate_agent_action = validate_agent_action
