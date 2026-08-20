from __future__ import annotations

from typing import Any

import yaml


class SkillFrontmatterError(ValueError):
    """Raised when strict parsing finds malformed Skill YAML metadata."""


def parse_skill_frontmatter(skill_md: str, *, strict: bool = False) -> dict[str, Any]:
    """Parse the optional YAML block at the beginning of ``SKILL.md``.

    Package ingestion uses strict mode so authors receive an actionable error.
    Runtime capability detection uses tolerant mode because malformed metadata
    must never grant a permission or prevent deterministic body inspection.
    """

    if not skill_md.startswith("---"):
        return {}
    lines = skill_md.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    try:
        closing = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        if strict:
            raise SkillFrontmatterError("SKILL.md YAML frontmatter is not closed") from None
        return {}
    try:
        parsed = yaml.safe_load("\n".join(lines[1:closing])) or {}
    except yaml.YAMLError as exc:
        if strict:
            raise SkillFrontmatterError("SKILL.md frontmatter is invalid YAML") from exc
        return {}
    if not isinstance(parsed, dict):
        if strict:
            raise SkillFrontmatterError("SKILL.md frontmatter must contain an object")
        return {}
    return parsed
