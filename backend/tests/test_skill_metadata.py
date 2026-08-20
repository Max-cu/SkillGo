import pytest

from app.skill_metadata import SkillFrontmatterError, parse_skill_frontmatter


def test_frontmatter_parser_has_strict_and_tolerant_modes():
    malformed = "---\nname: [not closed\n---\n# Skill"

    assert parse_skill_frontmatter(malformed) == {}
    with pytest.raises(SkillFrontmatterError, match="invalid YAML"):
        parse_skill_frontmatter(malformed, strict=True)


def test_frontmatter_parser_returns_only_mapping_metadata():
    scalar = "---\n- one\n- two\n---\n# Skill"

    assert parse_skill_frontmatter(scalar) == {}
    with pytest.raises(SkillFrontmatterError, match="must contain an object"):
        parse_skill_frontmatter(scalar, strict=True)
