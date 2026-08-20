from __future__ import annotations

import re
import shlex
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from .skill_metadata import parse_skill_frontmatter


SCRIPT_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".sh", ".ps1", ".bat", ".cmd", ".jar"}
ARTIFACT_SUFFIXES = ("docx", "xlsx", "pdf", "pptx", "txt", "json", "csv")
SANDBOX_DOCUMENT_ARTIFACTS = frozenset({"docx", "xlsx", "pdf", "pptx"})
SANDBOX_TOOL_ADAPTERS = {
    "listfiles": "list_files",
    "listdirectory": "list_files",
    "listdir": "list_files",
    "readdirectory": "list_files",
    "readfile": "read_file",
    "writetextfile": "write_file",
    "writefile": "write_file",
    "command": "command",
    "executecommand": "command",
    "runcommand": "command",
    "runpython": "run_python",
    "python": "run_python",
    "generateword": "run_python + python-docx",
    "createword": "run_python + python-docx",
    "generatedocx": "run_python + python-docx",
    "createdocx": "run_python + python-docx",
    "generateexcel": "run_python + openpyxl",
    "createexcel": "run_python + openpyxl",
    "generatexlsx": "run_python + openpyxl",
    "createxlsx": "run_python + openpyxl",
    "generatespreadsheet": "run_python + openpyxl",
    "createspreadsheet": "run_python + openpyxl",
    "generatepdf": "run_python + reportlab",
    "createpdf": "run_python + reportlab",
    "generatepowerpoint": "run_python + python-pptx",
    "createpowerpoint": "run_python + python-pptx",
    "generatepptx": "run_python + python-pptx",
    "createpptx": "run_python + python-pptx",
}
EXECUTABLE_KEYS = frozenset({
    "bin", "bins", "binary", "binaries", "command", "commands", "executable", "executables",
})
NETWORK_KEYS = frozenset({
    "network", "networks", "host", "hosts", "domain", "domains", "alloweddomains",
    "allowedhosts", "networkaccess",
})
NETWORK_CLIENTS = frozenset({
    "curl", "wget", "http", "https", "httpie", "aria2c", "ftp", "sftp", "ssh", "scp",
})
SHELL_BUILTINS = frozenset({
    ".", "[", "alias", "break", "case", "cd", "continue", "do", "done", "echo", "elif",
    "else", "esac", "eval", "exec", "exit", "export", "false", "fi", "for", "function",
    "if", "in", "local", "printf", "pwd", "read", "readonly", "return", "set", "shift",
    "source", "test", "then", "time", "trap", "true", "typeset", "ulimit", "umask", "unalias",
    "unset", "until", "wait", "while",
})
SHELL_FENCE_RE = re.compile(
    r"```(?:bash|sh|shell|zsh|fish|powershell|pwsh|cmd|bat)\s*\n(?P<body>.*?)```",
    re.IGNORECASE | re.DOTALL,
)
HTTP_URL_RE = re.compile(r"https?://[^\s<>'\"`]+", re.IGNORECASE)
BARE_HOST_RE = re.compile(
    r"(?<![@\w.-])(?:[a-z0-9](?:[a-z0-9-]{0,62})\.)+[a-z]{2,63}(?::\d{1,5})?(?:/[^\s<>'\"`]*)?",
    re.IGNORECASE,
)
EXECUTABLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,79}$")
TOOL_REFERENCE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?P<name>[A-Za-z][A-Za-z0-9]*(?:[._:/-][A-Za-z0-9]+)+)"
    r"(?![A-Za-z0-9])"
)


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _normalized_key(value: object) -> str:
    return re.sub(r"[^a-z]", "", str(value).casefold())


def _sandbox_tool_adapter(value: object) -> str | None:
    """Map common vendor tool names onto capability-equivalent sandbox primitives."""

    raw = str(value).strip().casefold()
    candidates = [raw, *re.split(r"[.:/]", raw)]
    for candidate in reversed(candidates):
        normalized = re.sub(r"[^a-z0-9]", "", candidate)
        if normalized in SANDBOX_TOOL_ADAPTERS:
            return SANDBOX_TOOL_ADAPTERS[normalized]
    return None


def _mentioned_sandbox_tool_adapters(skill_md: str) -> dict[str, str]:
    """Find only known capability aliases in prose without treating arbitrary text as permissions."""

    adapters: dict[str, str] = {}
    for match in TOOL_REFERENCE_RE.finditer(skill_md):
        tool = match.group("name")
        adapter = _sandbox_tool_adapter(tool)
        if adapter is not None:
            adapters[tool] = adapter
    return adapters


def _flatten_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [item for child in value for item in _flatten_strings(child)]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _flatten_strings(child)]
    return []


def _collect_declared_values(value: object, accepted_keys: frozenset[str]) -> list[str]:
    """Read common dependency shapes recursively without binding to one vendor."""

    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if _normalized_key(key) in accepted_keys:
                found.extend(_flatten_strings(child))
            if isinstance(child, (dict, list, tuple)):
                found.extend(_collect_declared_values(child, accepted_keys))
    elif isinstance(value, (list, tuple)):
        for child in value:
            found.extend(_collect_declared_values(child, accepted_keys))
    return found


def _has_declared_requirement(value: object, accepted_keys: frozenset[str]) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if _normalized_key(key) in accepted_keys:
                if child is True or isinstance(child, (list, tuple, dict)) and bool(child):
                    return True
                if isinstance(child, str) and child.strip().casefold() not in {
                    "", "0", "false", "no", "none", "off", "disabled",
                }:
                    return True
            if isinstance(child, (dict, list, tuple)) and _has_declared_requirement(
                child, accepted_keys
            ):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_has_declared_requirement(child, accepted_keys) for child in value)
    return False


def _clean_binary(value: str) -> str | None:
    token = value.strip().replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if token in SHELL_BUILTINS or not EXECUTABLE_RE.fullmatch(token):
        return None
    return token


def _shell_blocks(skill_md: str) -> list[str]:
    return [match.group("body") for match in SHELL_FENCE_RE.finditer(skill_md)]


def _command_binaries(blocks: list[str]) -> list[str]:
    binaries: set[str] = set()
    separators = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")
    for block in blocks:
        for raw_line in block.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            line = re.sub(r"^(?:\$|>)\s+", "", line)
            for segment in separators.split(line):
                try:
                    words = shlex.split(segment, posix=True)
                except ValueError:
                    words = segment.split()
                while words and ("=" in words[0] and not words[0].startswith(("=", "-"))):
                    words.pop(0)
                while words and words[0].casefold() in {"command", "env", "nohup", "sudo"}:
                    words.pop(0)
                if not words:
                    continue
                binary = _clean_binary(words[0])
                if binary:
                    binaries.add(binary)
    return sorted(binaries)


def _network_targets(blocks: list[str], declared: list[str]) -> list[str]:
    targets: set[str] = set()
    candidates = [*declared]
    for block in blocks:
        candidates.extend(match.group(0) for match in HTTP_URL_RE.finditer(block))
        candidates.extend(match.group(0) for match in BARE_HOST_RE.finditer(block))
    for raw in candidates:
        value = raw.strip().strip(".,;:()[]{}")
        if not value or value.casefold() in {"true", "false", "required", "enabled", "disabled"}:
            continue
        parsed = urlsplit(value if "://" in value else f"//{value}")
        host = (parsed.hostname or "").casefold().rstrip(".")
        if host and ("." in host or host == "localhost"):
            targets.add(host)
    return sorted(targets)


def detect_runtime_profile(
    *,
    skill_md: str,
    manifest: dict,
    file_names: tuple[str, ...] | list[str] = (),
) -> dict:
    """Classify execution requirements using deterministic package evidence.

    The result controls whether a version may run. Model-generated catalog
    metadata is intentionally not trusted for this decision.
    """
    spec = manifest.get("spec") if isinstance(manifest.get("spec"), dict) else {}
    permissions = spec.get("permissions") if isinstance(spec.get("permissions"), dict) else {}
    frontmatter = parse_skill_frontmatter(skill_md)
    raw_type = str(spec.get("type", "instruction")).lower()
    normalized_files = sorted({name.replace("\\", "/") for name in file_names})
    script_files = [
        name
        for name in normalized_files
        if PurePosixPath(name).suffix.lower() in SCRIPT_SUFFIXES
        and not name.lower().endswith("agents/openai.yaml")
    ]

    command_patterns = {
        "python": r"(?im)(?:^|[`\s])(python3?|pip3?)\s+[^\n`]+",
        "node": r"(?im)(?:^|[`\s])(node|npm|npx|pnpm|yarn)\s+[^\n`]+",
        "shell": r"(?im)(?:^|[`\s])(bash|sh|powershell|pwsh)\s+[^\n`]+",
    }
    runtimes = sorted(
        runtime for runtime, pattern in command_patterns.items() if re.search(pattern, skill_md)
    )
    if any(PurePosixPath(name).suffix.lower() == ".py" for name in script_files):
        runtimes.append("python")
    if any(PurePosixPath(name).suffix.lower() in {".js", ".mjs", ".cjs"} for name in script_files):
        runtimes.append("node")
    runtimes = sorted(set(runtimes))

    tools = _string_list(permissions.get("tools"))
    blocks = _shell_blocks(skill_md)
    declared_binaries = _collect_declared_values(frontmatter, EXECUTABLE_KEYS)
    declared_binaries.extend(_collect_declared_values(manifest, EXECUTABLE_KEYS))
    binaries = sorted(
        {
            binary
            for value in [*declared_binaries, *_command_binaries(blocks)]
            if (binary := _clean_binary(value))
        }
    )[:100]
    declared_network = _string_list(permissions.get("network"))
    declared_network.extend(_collect_declared_values(frontmatter, NETWORK_KEYS))
    declared_network.extend(_collect_declared_values(manifest, NETWORK_KEYS))
    network_targets = _network_targets(blocks, declared_network)
    network_rules = sorted({*declared_network, *network_targets})[:100]
    dependency_files = [
        name
        for name in normalized_files
        if PurePosixPath(name).name.casefold()
        in {
            "requirements.txt",
            "pyproject.toml",
            "package.json",
            "package-lock.json",
            "pnpm-lock.yaml",
            "yarn.lock",
        }
    ]
    dependency_download = bool(dependency_files) or bool(
        re.search(
            r"(?im)(?:pip3?|python3?\s+-m\s+pip)\s+install\b|"
            r"(?:npm|pnpm|yarn|bun)\s+(?:install|add|i)\b",
            skill_md,
        )
    )
    network_required = (
        bool(network_rules)
        or _has_declared_requirement(frontmatter, NETWORK_KEYS)
        or _has_declared_requirement(manifest, NETWORK_KEYS)
        or dependency_download
        or any(binary in NETWORK_CLIENTS for binary in binaries)
        or bool(
        re.search(r"联网|网络访问|online lookup|internet access|web search", skill_md, re.IGNORECASE)
        )
    )
    expected_artifacts = sorted(
        {
            suffix
            for suffix in ARTIFACT_SUFFIXES
            if re.search(rf"\.{suffix}\b|\b{suffix}\b", skill_md, re.IGNORECASE)
        }
    )
    document_artifacts = sorted(set(expected_artifacts) & SANDBOX_DOCUMENT_ARTIFACTS)
    tool_adapters = _mentioned_sandbox_tool_adapters(skill_md)
    tool_adapters.update({
        tool: adapter
        for tool in tools
        if (adapter := _sandbox_tool_adapter(tool)) is not None
    })
    platform_tools = sorted(tool for tool in tools if tool not in tool_adapters)

    reasons: list[str] = []
    package_requires_sandbox = (
        raw_type == "code" or bool(script_files) or bool(runtimes) or bool(binaries)
    )
    compatible_instruction_requires_sandbox = (
        not platform_tools and (bool(document_artifacts) or bool(tool_adapters))
    )
    requires_sandbox = package_requires_sandbox or compatible_instruction_requires_sandbox
    if requires_sandbox:
        execution_mode = "sandbox_required"
        runtime_status = "awaiting_sandbox"
        if script_files:
            reasons.append(f"包含 {len(script_files)} 个可执行脚本")
        if runtimes:
            reasons.append("需要运行环境：" + "、".join(runtimes))
        if binaries:
            reasons.append("需要命令：" + "、".join(binaries))
        if document_artifacts:
            reasons.append("需要在隔离沙箱中生成文档：" + "、".join(document_artifacts))
        if tool_adapters:
            reasons.append("可由沙箱兼容工具适配：" + "、".join(sorted(tool_adapters)))
        if network_required:
            reasons.append("部分步骤需要受控网络访问")
        block_reason = "需要 Linux 沙箱 Worker 执行脚本、工具或文档处理"
    elif platform_tools:
        execution_mode = "platform_tools"
        runtime_status = "awaiting_platform_tools"
        reasons.append("需要尚未适配的平台工具：" + "、".join(platform_tools))
        block_reason = "所需平台工具尚未接入当前执行器"
    else:
        execution_mode = "instruction_only"
        runtime_status = "available"
        block_reason = None

    return {
        "execution_mode": execution_mode,
        "runtime_status": runtime_status,
        "runnable": runtime_status == "available",
        "block_reason": block_reason,
        "requirements": {
            "runtimes": runtimes,
            "scripts": script_files[:100],
            "tools": tools,
            "tool_adapters": tool_adapters,
            "platform_tools": platform_tools,
            "binaries": binaries,
            "network": network_required,
            "network_rules": network_rules,
            "network_targets": network_targets,
            "dependency_download": dependency_download,
            "dependency_files": dependency_files[:50],
            "expected_artifacts": expected_artifacts,
        },
        "reasons": reasons,
    }


def version_runtime_profile(version: object) -> dict:
    manifest = getattr(version, "manifest", None) or {}
    extension = manifest.get("x-skillgo") if isinstance(manifest, dict) else None
    stored = extension.get("runtime") if isinstance(extension, dict) else None
    stored_profile = dict(stored) if isinstance(stored, dict) else {}
    stored_requirements = (
        dict(stored_profile.get("requirements") or {})
        if isinstance(stored_profile.get("requirements"), dict)
        else {}
    )
    known_files = [
        *list(stored_requirements.get("scripts") or []),
        *list(stored_requirements.get("dependency_files") or []),
    ]
    # Re-detect on read so Skills imported before a compatibility adapter was
    # added gain the new behavior without requiring a re-upload or migration.
    profile = detect_runtime_profile(
        skill_md=str(getattr(version, "skill_md", "")),
        manifest=manifest,
        file_names=known_files,
    )
    requirements = dict(profile.get("requirements") or {})
    for key in (
        "runtimes", "scripts", "tools", "binaries", "network_rules",
        "network_targets", "dependency_files", "expected_artifacts",
    ):
        requirements[key] = sorted(
            {
                str(item)
                for item in [
                    *list(stored_requirements.get(key) or []),
                    *list(requirements.get(key) or []),
                ]
                if str(item)
            }
        )
    requirements["tool_adapters"] = {
        **dict(stored_requirements.get("tool_adapters") or {}),
        **dict(requirements.get("tool_adapters") or {}),
    }
    requirements["network"] = bool(stored_requirements.get("network")) or bool(
        requirements.get("network")
    )
    requirements["dependency_download"] = bool(
        stored_requirements.get("dependency_download")
    ) or bool(requirements.get("dependency_download"))
    profile["requirements"] = requirements
    obsolete_reasons = {"需要平台文件或文档生成工具"}
    profile["reasons"] = list(
        dict.fromkeys(
            [
                *[
                    str(item)
                    for item in (stored_profile.get("reasons") or [])
                    if str(item) not in obsolete_reasons
                ],
                *[str(item) for item in (profile.get("reasons") or [])],
            ]
        )
    )
    # Availability is an environment property, not immutable package metadata.
    # This lets an existing version become runnable when a Worker is connected.
    from .config import settings

    mode = profile.get("execution_mode")
    if mode == "sandbox_required" and settings.sandbox_worker_enabled:
        profile.update(runtime_status="available", runnable=True, block_reason=None)
    elif mode == "platform_tools" and settings.platform_document_tools_enabled:
        profile.update(runtime_status="available", runnable=True, block_reason=None)
    return profile
