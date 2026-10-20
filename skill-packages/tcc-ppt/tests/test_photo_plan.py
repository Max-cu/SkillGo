"""Photo-plan writer and skill-owned page checks."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


photo_plan = load("write_photo_plan", "scripts/write_photo_plan.py")
page_checks = load("check_ppt_pages", "scripts/validators/check_ppt_pages.py")


SPEC = """# Design Spec

## I. Project Information

| Item | Value |
| --- | --- |
| Page Count | `2` |

## VIII. Image Resource List

| Filename | Source | Purpose | Assigned Page |
| --- | --- | --- | --- |
| `image_001.jpeg` | `[[image_001.jpeg]]` | 现场 | `page02` |

## IX. Content Outline

### page01 — 001_cover

封面

| Placeholder | Content | Check |
| --- | --- | --- |
| `PAGE_TITLE` | 封面标题 | `≤15字 ✓` |

### page02 — 009_creative（创作页）

内容

| Placeholder | Content | Check |
| --- | --- | --- |
| `PAGE_TITLE` | 现场照片 | `≤15字 ✓` |

## X. Speaker Notes
"""


def scaffold(tmp_path: Path, spec: str = SPEC, web: bool = False) -> Path:
    sources = tmp_path / "assets" / "sources"
    sources.mkdir(parents=True)
    (sources / "image_001.jpeg").write_bytes(b"jpeg")
    if web:
        (sources / "_web_images.json").write_text(
            json.dumps({"images": [{"file": "image_001.jpeg"}]}),
            encoding="utf-8",
        )
    (tmp_path / "assets" / "design_spec.md").write_text(spec, encoding="utf-8")
    templates = tmp_path / "assets" / "templates"
    (templates / "default").mkdir(parents=True)
    (templates / "default" / "001_cover.svg").write_text("<svg></svg>", encoding="utf-8")
    (templates / "009_content.svg").write_text("<svg></svg>", encoding="utf-8")
    return tmp_path


def test_writes_unique_assignment_matching_spec_pages(tmp_path: Path):
    root = scaffold(tmp_path)
    output = tmp_path / "_state" / "photo_plan.json"
    assert photo_plan.run(root, output) == 0
    plan = json.loads(output.read_text(encoding="utf-8"))
    assert plan["version"] == 1
    assert plan["updated_at"].endswith("Z")
    assert plan["photos"] == [
        {
            "image_id": "image_001",
            "path": (root / "assets" / "sources" / "image_001.jpeg").resolve().as_posix(),
            "source": "upload",
        }
    ]
    assert plan["assignments"] == {"page01": [], "page02": ["image_001"]}


def test_web_manifest_marks_source(tmp_path: Path):
    root = scaffold(tmp_path, web=True)
    output = tmp_path / "photo_plan.json"
    assert photo_plan.run(root, output) == 0
    plan = json.loads(output.read_text(encoding="utf-8"))
    assert plan["photos"][0]["source"] == "web"


def test_rejects_image_on_two_pages_without_clobbering(tmp_path: Path):
    root = scaffold(tmp_path)
    output = tmp_path / "photo_plan.json"
    assert photo_plan.run(root, output) == 0
    before = output.read_text(encoding="utf-8")
    spec = SPEC.replace("| `page02` |", "| `page01` |\n| `image_001.jpeg` | `[[image_001.jpeg]]` | 又 | `page02` |")
    (root / "assets" / "design_spec.md").write_text(spec, encoding="utf-8")
    assert photo_plan.run(root, output) == 1
    assert output.read_text(encoding="utf-8") == before


def test_rejects_two_images_on_one_page(tmp_path: Path):
    root = scaffold(tmp_path)
    (root / "assets" / "sources" / "image_002.png").write_bytes(b"png")
    spec = SPEC.replace(
        "| `image_001.jpeg` | `[[image_001.jpeg]]` | 现场 | `page02` |",
        "| `image_001.jpeg` | `[[image_001.jpeg]]` | 现场 | `page02` |\n"
        "| `image_002.png` | `[[image_002.png]]` | 另一张 | `page02` |",
    )
    (root / "assets" / "design_spec.md").write_text(spec, encoding="utf-8")
    assert photo_plan.run(root, tmp_path / "photo_plan.json") == 1
    assert not (tmp_path / "photo_plan.json").exists()


def test_page_count_must_match_outline(tmp_path: Path):
    root = scaffold(tmp_path, spec=SPEC.replace("| `2` |", "| `3` |"))
    assert photo_plan.run(root, tmp_path / "photo_plan.json") == 1


def test_missing_spec_records_photos_only(tmp_path: Path):
    root = scaffold(tmp_path)
    (root / "assets" / "design_spec.md").unlink()
    output = tmp_path / "photo_plan.json"
    assert photo_plan.run(root, output) == 0
    plan = json.loads(output.read_text(encoding="utf-8"))
    assert plan["assignments"] == {}
    assert len(plan["photos"]) == 1


def test_unchanged_plan_keeps_mtime(tmp_path: Path):
    root = scaffold(tmp_path)
    output = tmp_path / "photo_plan.json"
    assert photo_plan.run(root, output) == 0
    stamp = 1_700_000_000
    os.utime(output, (stamp, stamp))
    assert photo_plan.run(root, output) == 0
    assert output.stat().st_mtime == stamp


def test_page_checks_accept_consistent_deck(tmp_path: Path):
    root = scaffold(tmp_path)
    assert page_checks.main(["--root", str(root)]) == 0


def test_page_checks_cover_split_master_and_cross_page(tmp_path: Path):
    root = scaffold(tmp_path)
    spec = SPEC.replace("### page02 — 009_creative（创作页）", "### page03 — 009_missing")
    spec = spec.replace("| `PAGE_TITLE` | 现场照片 |", "| `PAGE_TITLE` | 封面标题 |")
    (root / "assets" / "design_spec.md").write_text(spec, encoding="utf-8")
    errors = page_checks.check(root)
    assert any("连续" in message for message in errors)
    assert any("SVG 母版" in message for message in errors)
    assert any("跨页标题重复" in message for message in errors)


def test_fragment_rejects_second_photo_and_svg_root(tmp_path: Path):
    root = scaffold(tmp_path)
    body = root / "assets" / "svg_body"
    body.mkdir()
    (body / "page02.svg").write_text(
        '<svg><image href="../sources/image_001.jpeg"/><image href="../sources/image_002.jpeg"/></svg>',
        encoding="utf-8",
    )
    errors = page_checks.check(root)
    assert any("svg 根节点" in message for message in errors)
    assert any("多张照片" in message for message in errors)


def test_pipeline_pages_must_match_spec(tmp_path: Path):
    root = scaffold(tmp_path)
    config = root / "assets" / "pipeline_config.json"
    config.write_text(
        json.dumps({"pages": [{"id": "page01"}, {"id": "page09"}]}),
        encoding="utf-8",
    )
    errors = page_checks.check(root)
    assert any("pipeline 页面" in message for message in errors)


def test_skill_declares_state_file_and_version():
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    front = text.split("---", 2)[1]
    assert 'workflow_version: "2026.10.20-joint-v1"' in front
    assert "_state/photo_plan.json" in front
    fetch = (ROOT / "references" / "step_0_extract.md").read_text(encoding="utf-8")
    assert "fetch_web_images.py" in fetch
    assert "write_photo_plan.py" in fetch
    assert "图量不足时的联网补图" in fetch
