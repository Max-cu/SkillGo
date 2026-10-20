#!/usr/bin/env python3
"""Step 5d: verify finalize_svg output — 文件数/图片内联/无外部引用."""
import json, os, re, sys

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG = os.path.join(SKILL_ROOT, "assets", "pipeline_config.json")
FINAL_DIR = os.path.join(SKILL_ROOT, "assets", "svg_final")

with open(CONFIG, "r", encoding="utf-8") as f:
    config = json.load(f)

errors = []
expected = len(config["pages"])

# 1. 文件数检查
svgs = sorted(f for f in os.listdir(FINAL_DIR) if f.endswith(".svg"))
actual = len(svgs)
print(f"文件数: 期望 {expected} / 实际 {actual}")
if actual != expected:
    errors.append(f"文件数不匹配: config={expected}, svg_final={actual}")

# 2. 逐个检查内联 + 外部引用
# 注意：006/007 等纯矢量模板没有 <image> 标签，属正常情况，跳过图片检查
for svg in svgs:
    fp = os.path.join(FINAL_DIR, svg)
    size_kb = os.path.getsize(fp) / 1024
    with open(fp, "r", encoding="utf-8") as f:
        content = f.read()

    has_image_tag = re.search(r'<image\b', content) is not None
    if not has_image_tag:
        # 纯矢量页：无 <image> 标签，无需内联检查
        print(f"  {svg}: ✅ {size_kb:.0f}KB, 纯矢量页（无图片）")
        continue

    has_inline = 'data:image/' in content
    has_external = '../sources/' in content or '../images/' in content

    if not has_inline:
        errors.append(f"{svg}: 有 <image> 标签但未内联 (data:image/ 不存在)")
    if has_external:
        errors.append(f"{svg}: 仍有外部图片引用 (../sources/ 或 ../images/)")

    status = "✅" if has_inline and not has_external else "❌"
    print(f"  {svg}: {status} {size_kb:.0f}KB, 内联={has_inline}, 外部引用={has_external}")

if not errors:
    print("\n✅ SVG 最终化验证全部通过")
    sys.exit(0)
else:
    print(f"\n❌ 发现 {len(errors)} 个问题:")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
