from dataclasses import replace
from types import SimpleNamespace

from app import config
from app.runtime_profile import detect_runtime_profile, version_runtime_profile


def test_runtime_profile_enables_network_for_declared_dependency_download():
    profile = detect_runtime_profile(
        skill_md="# Skill\nRun `pip install custom-parser` before scripts/review.py.",
        manifest={"spec": {"type": "code"}},
        file_names=["scripts/review.py"],
    )

    requirements = profile["requirements"]
    assert requirements["dependency_download"] is True
    assert requirements["network"] is True


def test_runtime_profile_keeps_offline_skill_without_network():
    profile = detect_runtime_profile(
        skill_md="# Skill\nRun `python3 scripts/review.py input.docx`.",
        manifest={"spec": {"type": "code"}},
        file_names=["scripts/review.py"],
    )

    requirements = profile["requirements"]
    assert requirements["dependency_download"] is False
    assert requirements["network"] is False


def test_runtime_profile_enables_network_for_dependency_manifest():
    profile = detect_runtime_profile(
        skill_md="# Skill\nRun the bundled processor.",
        manifest={"spec": {"type": "code"}},
        file_names=["scripts/review.py", "requirements.txt"],
    )

    requirements = profile["requirements"]
    assert requirements["dependency_files"] == ["requirements.txt"]
    assert requirements["network"] is True


def test_runtime_profile_adapts_nested_third_party_binary_metadata_generically():
    profile = detect_runtime_profile(
        skill_md="""---
name: remote-status
description: Query a remote status endpoint.
metadata:
  any-vendor:
    requires:
      bins: [curl]
---
# Status
```bash
curl -s "status.example.net/api/current"
```
""",
        manifest={"spec": {"type": "instruction", "permissions": {}}},
    )

    requirements = profile["requirements"]
    assert profile["execution_mode"] == "sandbox_required"
    assert requirements["binaries"] == ["curl"]
    assert requirements["network"] is True
    assert requirements["network_targets"] == ["status.example.net"]


def test_runtime_profile_detects_network_command_without_vendor_metadata():
    profile = detect_runtime_profile(
        skill_md="""# Lookup
```sh
wget -q https://downloads.example.org/data.json
```
""",
        manifest={"spec": {"type": "instruction"}},
    )

    requirements = profile["requirements"]
    assert requirements["binaries"] == ["wget"]
    assert requirements["network"] is True
    assert requirements["network_targets"] == ["downloads.example.org"]


def test_runtime_profile_accepts_generic_nested_network_flag():
    profile = detect_runtime_profile(
        skill_md="# Remote lookup\nRun the bundled client.",
        manifest={
            "spec": {"type": "code"},
            "vendor-extension": {"requirements": {"networkAccess": True}},
        },
    )

    assert profile["requirements"]["network"] is True


def test_existing_version_is_redetected_after_compatibility_adapter_upgrade():
    version = SimpleNamespace(
        skill_md="""---
name: remote-status
description: Query remote status.
metadata: {\"legacy-tool\": {\"requires\": {\"bins\": [\"curl\"]}}}
---
```bash
curl -s https://status.example.com/current
```
""",
        manifest={
            "spec": {"type": "instruction", "permissions": {}},
            "x-skillgo": {
                "runtime": {
                    "execution_mode": "sandbox_required",
                    "requirements": {"network": False, "runtimes": ["shell"]},
                }
            },
        },
    )

    requirements = version_runtime_profile(version)["requirements"]
    assert requirements["network"] is True
    assert requirements["binaries"] == ["curl"]
    assert requirements["network_targets"] == ["status.example.com"]


def test_instruction_only_word_skill_is_routed_to_document_capable_sandbox():
    profile = detect_runtime_profile(
        skill_md="""---
name: proofreading
description: Review a document and produce a Word report.
---
# Available tools
- list_directory
- read_file
- write_file
- generate_word

Save the final result as `/workspace/output/review-report.docx`.
""",
        manifest={"spec": {"type": "instruction", "permissions": {}}},
    )

    assert profile["execution_mode"] == "sandbox_required"
    assert profile["runtime_status"] == "awaiting_sandbox"
    assert profile["requirements"]["expected_artifacts"] == ["docx"]
    assert profile["requirements"]["tool_adapters"] == {
        "generate_word": "run_python + python-docx",
        "list_directory": "list_files",
        "read_file": "read_file",
        "write_file": "write_file",
    }
    assert "docx" in " ".join(profile["reasons"])


def test_common_vendor_tool_names_are_adapted_without_private_skillgo_format():
    profile = detect_runtime_profile(
        skill_md="# Document task\nProduce the requested deliverable.",
        manifest={
            "spec": {
                "type": "instruction",
                "permissions": {
                    "tools": ["vendor.fs.list_directory", "generate_word"],
                },
            }
        },
    )

    assert profile["execution_mode"] == "sandbox_required"
    assert profile["requirements"]["tool_adapters"] == {
        "generate_word": "run_python + python-docx",
        "vendor.fs.list_directory": "list_files",
    }
    assert profile["requirements"]["platform_tools"] == []


def test_unknown_external_platform_tool_remains_explicitly_blocked():
    profile = detect_runtime_profile(
        skill_md="# CRM task\nUpdate the remote record.",
        manifest={
            "spec": {
                "type": "instruction",
                "permissions": {"tools": ["vendor.crm.update_record"]},
            }
        },
    )

    assert profile["execution_mode"] == "platform_tools"
    assert profile["runtime_status"] == "awaiting_platform_tools"
    assert profile["requirements"]["platform_tools"] == ["vendor.crm.update_record"]


def test_existing_document_skill_becomes_runnable_without_reimport(monkeypatch):
    monkeypatch.setattr(
        config,
        "settings",
        replace(config.settings, sandbox_worker_enabled=True),
    )
    version = SimpleNamespace(
        skill_md="# Word report\nGenerate `/workspace/output/report.docx`.",
        manifest={
            "spec": {"type": "instruction", "permissions": {}},
            "x-skillgo": {
                "runtime": {
                    "execution_mode": "platform_tools",
                    "reasons": ["需要平台文件或文档生成工具"],
                    "requirements": {"expected_artifacts": ["docx"]},
                }
            },
        },
    )

    profile = version_runtime_profile(version)

    assert profile["execution_mode"] == "sandbox_required"
    assert profile["runtime_status"] == "available"
    assert profile["runnable"] is True
    assert "需要平台文件或文档生成工具" not in profile["reasons"]
