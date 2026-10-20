#!/usr/bin/env python3
"""Write the photo-plan state file from sources and design_spec.

The file is the authoritative record of which photo sits on which page.
Run it after photos are fetched and again whenever design_spec assignment
changes. Do not hand-write the JSON.

Default output is ``/workspace/_state/photo_plan.json``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

PHOTO_NAME = re.compile(r"^image_(\d+)\.(jpeg|jpg|png|webp|gif|bmp)$", re.I)
PAGE_HEADING = re.compile(r"^###\s+(page\d+)\b[^\n]*", re.M)
PAGE_COUNT = re.compile(r"^\|\s*Page Count\s*\|\s*`?(\d+)`?\s*\|", re.M | re.I)
SECTION = re.compile(r"^##\s+([IVXLC]+)\b[^\n]*", re.M)
IMAGE_TOKEN = re.compile(r"image_(\d+)", re.I)
PAGE_TOKEN = re.compile(r"\b(page\d+)\b", re.I)
MASTER_TOKEN = re.compile(
    r"(002_toc_3ch|001_cover|002_toc|003_toc|010_ending|009_creative|009_content)"
)
PAGE_TITLE = re.compile(r"^\|\s*`?PAGE_TITLE`?\s*\|\s*(.*?)\s*\|", re.M)
STATE_PATH = Path("/workspace/_state/photo_plan.json")
PLAN_VERSION = 1


@dataclass
class SpecView:
    pages: list[str] = field(default_factory=list)
    page_count: int | None = None
    assignments: dict[str, list[str]] = field(default_factory=dict)
    blocks: dict[str, str] = field(default_factory=dict)
    templates: dict[str, str] = field(default_factory=dict)
    titles: dict[str, str] = field(default_factory=dict)
    present: bool = False


def image_id_from_number(number: str) -> str:
    return f"image_{int(number):03d}"


def section_body(text: str, numeral: str) -> str:
    matches = list(SECTION.finditer(text))
    for index, match in enumerate(matches):
        if match.group(1) != numeral:
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        return text[start:end]
    return ""


def parse_design_spec(text: str) -> SpecView:
    """Read page headings from section IX and photo rows from section VIII."""
    view = SpecView(present=True)
    count = PAGE_COUNT.search(text)
    if count:
        view.page_count = int(count.group(1))
    outline = section_body(text, "IX")
    for match in PAGE_HEADING.finditer(outline):
        page_id = match.group(1)
        if page_id not in view.pages:
            view.pages.append(page_id)
    blocks = re.split(r"\n(?=### )", outline)
    for block in blocks:
        heading = PAGE_HEADING.search(block)
        if not heading:
            continue
        page_id = heading.group(1)
        view.blocks[page_id] = block
        master = MASTER_TOKEN.search(heading.group(0))
        if master:
            view.templates[page_id] = master.group(1)
        title = PAGE_TITLE.search(block)
        if title:
            view.titles[page_id] = title.group(1).strip().strip("`")
    for line in section_body(text, "VIII").splitlines():
        images = [image_id_from_number(item) for item in IMAGE_TOKEN.findall(line)]
        pages = []
        for page_id in PAGE_TOKEN.findall(line):
            if page_id not in pages:
                pages.append(page_id)
        if not images or not pages:
            continue
        for page_id in pages:
            bucket = view.assignments.setdefault(page_id, [])
            for image_id in images:
                if image_id not in bucket:
                    bucket.append(image_id)
    return view


def web_filenames(sources: Path) -> set[str]:
    names: set[str] = set()
    manifest = sources / "_web_images.json"
    if manifest.is_file():
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
        images = payload.get("images") if isinstance(payload, dict) else None
        for item in images or []:
            if isinstance(item, dict) and isinstance(item.get("file"), str):
                names.add(Path(item["file"]).name)
            elif isinstance(item, str):
                names.add(Path(item).name)
    notes = sources / "文本.md"
    if notes.is_file():
        for line in notes.read_text(encoding="utf-8").splitlines():
            if "网图" not in line:
                continue
            for match in re.finditer(r"image_\d+\.[A-Za-z0-9]+", line, re.I):
                names.add(match.group(0))
    return names


def collect_photos(sources: Path) -> tuple[list[dict], list[str]]:
    errors: list[str] = []
    if not sources.is_dir():
        return [], [f"sources 目录不存在: {sources}"]
    web = {name.lower() for name in web_filenames(sources)}
    found: dict[str, dict] = {}
    for path in sorted(sources.iterdir()):
        if not path.is_file():
            continue
        match = PHOTO_NAME.match(path.name)
        if not match:
            continue
        if ".." in path.parts:
            errors.append(f"照片路径非法: {path.name}")
            continue
        try:
            resolved = path.resolve()
        except OSError as exc:
            errors.append(f"照片无法读取: {path.name} ({exc})")
            continue
        try:
            inside = resolved.is_relative_to(sources.resolve())
        except OSError:
            inside = False
        if not inside:
            errors.append(f"照片不在 sources 内: {path.name}")
            continue
        image_id = image_id_from_number(match.group(1))
        if image_id in found:
            errors.append(f"照片编号重复: {image_id}")
            continue
        source = "web" if path.name.lower() in web else "upload"
        found[image_id] = {
            "image_id": image_id,
            "path": resolved.as_posix(),
            "source": source,
        }
    photos = [found[key] for key in sorted(found)]
    return photos, errors


def assignment_errors(view: SpecView, photo_ids: set[str]) -> list[str]:
    errors: list[str] = []
    if view.page_count is not None and view.page_count != len(view.pages):
        errors.append(
            f"Page Count {view.page_count} 与大纲页数 {len(view.pages)} 不一致"
        )
    seen: dict[str, str] = {}
    for page_id, image_ids in view.assignments.items():
        if page_id not in view.pages:
            errors.append(f"分配页面不在大纲中: {page_id}")
        if len(image_ids) > 1:
            errors.append(f"{page_id} 分配了多张照片: {', '.join(image_ids)}")
        for image_id in image_ids:
            if image_id not in photo_ids:
                errors.append(f"分配了不存在的照片: {image_id}")
            previous = seen.get(image_id)
            if previous and previous != page_id:
                errors.append(f"{image_id} 同时分配给 {previous} 和 {page_id}")
            seen[image_id] = page_id
    return errors


def filled_assignments(view: SpecView) -> dict[str, list[str]]:
    filled: dict[str, list[str]] = {}
    for page_id in view.pages:
        filled[page_id] = list(view.assignments.get(page_id, []))
    return filled


def build_plan(photos: list[dict], view: SpecView | None) -> dict:
    assignments = filled_assignments(view) if view and view.pages else {}
    return {
        "version": PLAN_VERSION,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "photos": photos,
        "assignments": assignments,
    }


def signature(plan: dict) -> str:
    photos = [
        {
            "image_id": item.get("image_id"),
            "path": item.get("path"),
            "source": item.get("source"),
        }
        for item in plan.get("photos") or []
    ]
    assignments = plan.get("assignments") or {}
    ordered = {key: list(assignments[key]) for key in sorted(assignments)}
    return json.dumps(
        {"version": plan.get("version"), "photos": photos, "assignments": ordered},
        ensure_ascii=False,
        sort_keys=True,
    )


def write_plan(plan: dict, output: Path) -> bool:
    """Write atomically. Return False when the semantic content is unchanged."""
    if output.is_file():
        try:
            current = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = None
        if isinstance(current, dict) and signature(current) == signature(plan):
            return False
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, output)
    return True


def load_spec(path: Path) -> tuple[SpecView | None, list[str]]:
    if not path.is_file():
        return None, []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, [f"无法读取 design_spec: {exc}"]
    return parse_design_spec(text), []


def run(root: Path, output: Path, spec_path: Path | None = None) -> int:
    sources = root / "assets" / "sources"
    spec = spec_path or (root / "assets" / "design_spec.md")
    photos, errors = collect_photos(sources)
    view, spec_errors = load_spec(spec)
    errors.extend(spec_errors)
    if view is not None and view.present and (view.pages or view.page_count is not None):
        errors.extend(assignment_errors(view, {item["image_id"] for item in photos}))
    if errors:
        for message in errors:
            print(f"❌ {message}", file=sys.stderr)
        print("照片安排未写入。", file=sys.stderr)
        return 1
    plan = build_plan(photos, view)
    changed = write_plan(plan, output)
    if changed:
        print(f"✅ 已写入照片安排 {output}（{len(plan['photos'])} 张，{len(plan['assignments'])} 页）")
    else:
        print(f"✅ 照片安排无变化，保留 {output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, default=STATE_PATH)
    parser.add_argument("--spec", type=Path, default=None)
    args = parser.parse_args(argv)
    return run(args.root.resolve(), args.output, args.spec)


if __name__ == "__main__":
    raise SystemExit(main())
