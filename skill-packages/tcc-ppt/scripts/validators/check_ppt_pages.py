#!/usr/bin/env python3
"""Page checks owned by this skill.

Checks one image per page, page/spec consistency, contiguous page split,
SVG master files, and cross-page title/image reuse. Framework code does
not interpret these rules.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import write_photo_plan as photo_plan  # noqa: E402

MASTER_FILES = {
    "001_cover": "001_cover.svg",
    "002_toc": "002_toc.svg",
    "002_toc_3ch": "002_toc_3ch.svg",
    "003_toc": "003_toc.svg",
    "009_creative": "009_content.svg",
    "009_content": "009_content.svg",
    "010_ending": "010_ending.svg",
}
PHOTO_HREF = re.compile(
    r"(?:xlink:)?href\s*=\s*([\"'])([^\"']*image_\d+[^\"']*)\1",
    re.I,
)
SVG_ROOT = re.compile(r"<\s*svg\b", re.I)


def style_name(root: Path) -> str:
    config = root / "assets" / "pipeline_config.json"
    templates = root / "assets" / "templates"
    if not config.is_file():
        return "default"
    try:
        meta = json.loads(config.read_text(encoding="utf-8")).get("_meta") or {}
    except (OSError, json.JSONDecodeError):
        return "default"
    explicit = meta.get("template_set") if isinstance(meta, dict) else None
    palette = meta.get("palette") if isinstance(meta, dict) else None
    if isinstance(explicit, str) and (templates / explicit).is_dir():
        return explicit
    if isinstance(palette, str) and (templates / palette).is_dir():
        return palette
    return "default"


def master_file(root: Path, style: str, token: str) -> Path | None:
    filename = MASTER_FILES.get(token)
    if filename is None:
        return None
    if token in {"009_creative", "009_content"}:
        return root / "assets" / "templates" / filename
    return root / "assets" / "templates" / style / filename


def expected_page_ids(count: int) -> list[str]:
    return [f"page{index:02d}" for index in range(1, count + 1)]


def split_errors(view: photo_plan.SpecView) -> list[str]:
    expected = expected_page_ids(len(view.pages))
    if view.pages != expected:
        errors = [
            "页码拆分必须是连续的 page01 起、无缺号无重号: "
            + ", ".join(view.pages)
        ]
        return errors
    return []


def master_errors(root: Path, view: photo_plan.SpecView) -> list[str]:
    style = style_name(root)
    errors: list[str] = []
    for page_id in view.pages:
        token = view.templates.get(page_id)
        if not token:
            errors.append(f"{page_id} 未标明 SVG 母版")
            continue
        path = master_file(root, style, token)
        if path is None or not path.is_file():
            errors.append(f"{page_id} 的 SVG 母版不存在: {token}")
    return errors


def cross_page_errors(view: photo_plan.SpecView) -> list[str]:
    errors: list[str] = []
    seen: dict[str, str] = {}
    for page_id, title in view.titles.items():
        cleaned = title.strip()
        if not cleaned or cleaned.startswith("<"):
            continue
        previous = seen.get(cleaned)
        if previous:
            errors.append(f"跨页标题重复: {previous} 与 {page_id} 都是「{cleaned}」")
        seen[cleaned] = page_id
    return errors


def pipeline_errors(root: Path, view: photo_plan.SpecView) -> list[str]:
    config = root / "assets" / "pipeline_config.json"
    if not config.is_file():
        return []
    try:
        payload = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"pipeline_config.json 无法读取: {exc}"]
    pages = payload.get("pages") if isinstance(payload, dict) else None
    if not isinstance(pages, list):
        return []
    ids = [
        item.get("id")
        for item in pages
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    if ids != view.pages:
        return [
            "pipeline 页面与 design_spec 大纲不一致: "
            + ", ".join(str(item) for item in ids)
        ]
    return []


def fragment_errors(root: Path) -> list[str]:
    body = root / "assets" / "svg_body"
    if not body.is_dir():
        return []
    errors: list[str] = []
    for path in sorted(body.glob("page*.svg")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"无法读取 {path.name}: {exc}")
            continue
        if SVG_ROOT.search(text):
            errors.append(f"{path.name} 含有 svg 根节点，母版才拥有 svg 文档")
        image_ids = []
        for match in PHOTO_HREF.finditer(text):
            number = photo_plan.IMAGE_TOKEN.search(match.group(2))
            if not number:
                continue
            image_id = photo_plan.image_id_from_number(number.group(1))
            if image_id not in image_ids:
                image_ids.append(image_id)
        if len(image_ids) > 1:
            errors.append(f"{path.name} 含有多张照片: {', '.join(image_ids)}")
    return errors


def check(root: Path, spec_path: Path | None = None) -> list[str]:
    spec = spec_path or (root / "assets" / "design_spec.md")
    if not spec.is_file():
        return [f"design_spec.md 不存在: {spec}"]
    try:
        view = photo_plan.parse_design_spec(spec.read_text(encoding="utf-8"))
    except OSError as exc:
        return [f"无法读取 design_spec: {exc}"]
    photos, photo_errors = photo_plan.collect_photos(root / "assets" / "sources")
    errors = list(photo_errors)
    errors.extend(photo_plan.assignment_errors(view, {item["image_id"] for item in photos}))
    errors.extend(split_errors(view))
    errors.extend(master_errors(root, view))
    errors.extend(cross_page_errors(view))
    errors.extend(pipeline_errors(root, view))
    errors.extend(fragment_errors(root))
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--spec", type=Path, default=None)
    args = parser.parse_args(argv)
    errors = check(args.root.resolve(), args.spec)
    if errors:
        for message in errors:
            print(f"❌ {message}", file=sys.stderr)
        return 1
    print("✅ 页级检查通过（一页一图、页数一致、连续拆页、SVG 母版、跨页）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
