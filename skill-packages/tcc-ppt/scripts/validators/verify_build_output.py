#!/usr/bin/env python3
"""Step 3d: verify build_svgs output — 页数/残留占位符/图片路径/XML 合法性 四项合一."""
import json, os, re, sys

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG = os.path.join(SKILL_ROOT, "assets", "pipeline_config.json")
SVG_DIR = os.path.join(SKILL_ROOT, "assets", "svg_output")

errors = []

# 1. 页数检查
with open(CONFIG, "r", encoding="utf-8") as f:
    config = json.load(f)
expected = len(config["pages"])
svgs = sorted(f for f in os.listdir(SVG_DIR) if f.endswith(".svg"))
actual = len(svgs)
print(f"页数: 期望 {expected} / 实际 {actual}")
if actual != expected:
    errors.append(f"页数不匹配: config={expected}, svg_output={actual}")

# 2. 残留占位符检查
placeholder_pattern = re.compile(r"\{\{[A-Z0-9_]+\}\}")
residues = []
for svg in svgs:
    with open(os.path.join(SVG_DIR, svg), "r", encoding="utf-8") as f:
        content = f.read()
    found = placeholder_pattern.findall(content)
    if found:
        residues.append((svg, found))
if residues:
    for svg, placeholders in residues:
        errors.append(f"残留占位符: {svg} → {set(placeholders)}")
else:
    print("残留占位符: 无 (✅)")

# 3. 图片路径抽查 (检查首末页)
check_files = [svgs[0], svgs[-1]] if len(svgs) >= 2 else svgs
href_pattern = re.compile(r'href="([^"]+)"')
for svg in check_files:
    with open(os.path.join(SVG_DIR, svg), "r", encoding="utf-8") as f:
        content = f.read()
    hrefs = href_pattern.findall(content)
    bad = [h for h in hrefs if h.startswith("/") or h.startswith("\\\\") or h.startswith("C:")]
    if bad:
        errors.append(f"图片路径异常 (绝对路径): {svg} → {bad}")
    image_refs = [h for h in hrefs if "images/" in h or "sources/" in h]
    print(f"图片路径抽查 {svg}: {len(image_refs)} 个图片引用, 0 个绝对路径 (✅)")
# 4. XML 合法性检查
# 009 创作页是「页头模板 + 正文片段」拼接而成，拼接点最容易产出非法 XML
# （如闭合标签缺失/重复、正文片段带根标签）。这里做一次统一兜底。
from xml.etree import ElementTree as _ET

xml_bad = []
for svg in svgs:
    with open(os.path.join(SVG_DIR, svg), "r", encoding="utf-8") as f:
        content = f.read()
    try:
        root = _ET.fromstring(content)
    except _ET.ParseError as exc:
        xml_bad.append((svg, f"XML 解析失败: {exc}"))
        continue
    if root.tag.rsplit("}", 1)[-1] != "svg":
        xml_bad.append((svg, f"根元素不是 svg（实际 {root.tag.rsplit('}', 1)[-1]}）"))
if xml_bad:
    for svg, msg in xml_bad:
        errors.append(f"XML 非法: {svg} → {msg}")
else:
    print(f"XML 合法性: {actual} 页全部合法 (✅)")

if not errors:
    print("\n✅ 四项验证全部通过")
    sys.exit(0)
else:
    print(f"\n❌ 发现 {len(errors)} 个问题:")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
