"""Regression: finish must accept more than 10 declared artifacts.

Job 4b5fed78 produced 13 deliverables; the platform silently sliced the
artifacts array to 10 before the undeclared-file check, so the last 3
files were always reported undeclared and the model was forced to block.
"""
from __future__ import annotations

from app.model_gateway import SANDBOX_AGENT_TOOLS
from app.sandbox_tool_registry import validate_agent_action


def test_finish_schema_allows_50_artifacts():
    by_name = {t["function"]["name"]: t["function"] for t in SANDBOX_AGENT_TOOLS}
    arts = by_name["finish"]["parameters"]["properties"]["artifacts"]
    assert arts["maxItems"] == 50
    assert arts["minItems"] == 1


def test_finish_action_accepts_13_artifact_paths():
    files = [
        "/workspace/output/执行状态.json",
        "/workspace/output/校验摘要.md",
        "/workspace/output/结果索引.md",
        "/workspace/output/输入与配置快照.json",
        "/workspace/output/目录明细/catalog.xlsx",
        "/workspace/output/目录明细/extraction.json",
        "/workspace/output/目录明细/report.md",
        "/workspace/output/目录校验报告/report.json",
        "/workspace/output/目录校验报告/report.md",
        "/workspace/output/目录校验报告/report.xlsx",
        "/workspace/output/综合报告/report.json",
        "/workspace/output/综合报告/report.md",
        "/workspace/output/综合报告/report.xlsx",
    ]
    action = {"action": "finish", "summary": "六步流程全部完成", "artifacts": files,
              "reason": "deliver all 13 files"}
    assert validate_agent_action(action) is None
