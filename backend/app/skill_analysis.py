from __future__ import annotations

import re

from .model_gateway import ModelGatewayError, OpenAICompatibleGateway
from .skill_package import ValidatedPackage


CATEGORIES = {"productivity", "writing", "document", "development", "data", "other"}


def _text(value: object, *, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()[:limit]


def _slug(value: object, fallback: str) -> str:
    source = value if isinstance(value, str) else ""
    slug = re.sub(r"[^a-z0-9]+", "-", source.lower()).strip("-")[:80].rstrip("-")
    if len(slug) < 3:
        slug = re.sub(r"[^a-z0-9]+", "-", fallback.lower()).strip("-")[:80].rstrip("-")
    return slug if len(slug) >= 3 else f"skill-{fallback[:8].lower()}"


def _body_preview(skill_md: str) -> str:
    body = skill_md
    if body.startswith("---"):
        parts = body.split("---", 2)
        if len(parts) == 3:
            body = parts[2]
    body = re.sub(r"```.*?```", " ", body, flags=re.DOTALL)
    body = re.sub(r"[#>*_`|\[\]()]", " ", body)
    return re.sub(r"\s+", " ", body).strip()


def package_fallback(validated: ValidatedPackage) -> dict[str, str]:
    manifest_metadata = validated.manifest.get("metadata") or {}
    raw_name = (
        validated.metadata_name
        or str(manifest_metadata.get("displayName") or manifest_metadata.get("name") or "")
    )
    slug = _slug(manifest_metadata.get("name") or raw_name, validated.sha256)
    name = _text(raw_name, limit=120) or slug
    if len(name) < 2:
        name = f"Skill {validated.sha256[:6]}"

    declared_description = _text(validated.metadata_description, limit=20_000)
    preview = _body_preview(validated.skill_md)
    summary = declared_description or _text(preview, limit=160)
    if len(summary) < 10:
        summary = f"{name}：提供可复用的智能工作流能力。"
    summary = summary[:280]
    description = declared_description or _text(preview, limit=4_000) or summary
    return {
        "name": name,
        "slug": slug,
        "summary": summary,
        "description": description,
        "category": "other",
    }


def _merge_suggestion(fallback: dict[str, str], suggestion: dict) -> dict[str, str]:
    name = _text(suggestion.get("name"), limit=120)
    if len(name) < 2:
        name = fallback["name"]

    summary = _text(suggestion.get("summary"), limit=280)
    if len(summary) < 10:
        summary = fallback["summary"]

    description = _text(suggestion.get("description"), limit=20_000) or fallback["description"]
    category = suggestion.get("category")
    if category not in CATEGORIES:
        category = fallback["category"]

    return {
        "name": name,
        "slug": _slug(suggestion.get("slug"), fallback["slug"]),
        "summary": summary,
        "description": description,
        "category": category,
    }


async def analyze_package(
    validated: ValidatedPackage,
    gateway: OpenAICompatibleGateway,
) -> dict:
    fallback = package_fallback(validated)
    warnings: list[str] = []
    if validated.package_format == "agent-skill":
        warnings.append("这是标准 Agent Skill 包；SkillGo 已生成默认运行配置和 0.1.0 版本号。")

    if not gateway.configured:
        warnings.append("私有模型尚未配置，当前信息来自 SKILL.md 本地解析，仍可手动修改。")
        return {
            **fallback,
            "source": "package",
            "model_name": None,
            "warnings": warnings,
        }

    try:
        result = await gateway.analyze_skill(
            skill_md=validated.skill_md,
            package_metadata={
                "name": validated.metadata_name,
                "description": validated.metadata_description,
                "version": validated.version,
                "format": validated.package_format,
                "type": validated.skill_type.value,
                "permissions": validated.permissions,
            },
        )
    except ModelGatewayError:
        warnings.append("大模型分析暂时不可用，已回退到包内元数据，你可以继续创建。")
        return {
            **fallback,
            "source": "package",
            "model_name": None,
            "warnings": warnings,
        }

    return {
        **_merge_suggestion(fallback, result.output),
        "source": "ai",
        "model_name": result.model_name,
        "warnings": warnings,
    }
