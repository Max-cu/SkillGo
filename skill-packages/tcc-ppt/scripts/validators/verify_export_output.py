#!/usr/bin/env python3
"""Step 5d: verify export_flat output — PPTX存在/大小/幻灯片页数/svg_final恢复/svg_final_bak清理."""
import json, os, re, sys, zipfile

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXPORTS = os.path.join(SKILL_ROOT, "exports")
FINAL = os.path.join(SKILL_ROOT, "assets", "svg_final")
BAK = os.path.join(SKILL_ROOT, "assets", "svg_final_bak")
CONFIG = os.path.join(SKILL_ROOT, "assets", "pipeline_config.json")

# 页均大小的下界（KB/页）
# 实测：纯文字 15 页 ≈ 0.54~0.56 MB ⇒ 37 KB/页；带网图 15 页 ≈ 1.4~1.9 MB ⇒ 95~130 KB/页。
# 取 20 KB/页 作下界 —— 留了近一半余量，只有"空壳页"才会掉到线下。
MIN_KB_PER_PAGE = 20

errors = []
slide_count = None
pptx_path = None
media_count = 0

# 1. PPTX 文件存在（按 mtime 降序取最新，避免校验到历史版本）
#    ⚠️ 必须排除 Office 的 ~$ 锁文件：用户打开 pptx 时 Office 会生成
#    "~$<原名>.pptx"，它以 .pptx 结尾但只有几十字节，会被误判成「文件过小」。
pptx_files = sorted(
    [f for f in os.listdir(EXPORTS)
     if f.endswith(".pptx") and not f.startswith("~$")],
    key=lambda f: os.path.getmtime(os.path.join(EXPORTS, f)),
    reverse=True,
)
if not pptx_files:
    errors.append("exports/ 中无 .pptx 文件")
else:
    pptx_path = os.path.join(EXPORTS, pptx_files[0])
    size_mb = os.path.getsize(pptx_path) / (1024 * 1024)
    print(f"PPTX: {pptx_files[0]} ({size_mb:.2f} MB)")

    # 1b. 打开一次，数幻灯片与媒体（第 2、6 项共用，避免重复解包）
    try:
        with zipfile.ZipFile(pptx_path) as zf:
            names = zf.namelist()
        slide_count = len([n for n in names
                           if re.match(r"ppt/slides/slide\d+\.xml$", n)])
        media_count = len([n for n in names if n.startswith("ppt/media/")])
    except Exception as exc:                      # 文件损坏 / 非 zip / 加密
        errors.append(f"PPTX 打不开（损坏 / 非 zip / 加密）: {type(exc).__name__}: {exc}")

    # 2. 文件大小检查 —— **按页均，不按绝对大小**
    #    🔴 2026-10-18 改口径：原来写死 `< 1 MB ⇒ 报错`。但**无图（纯文字）**的 15 页 deck
    #    实测只有 0.54 MB（海上风电 v4.13 0.54 / 阴极保护 v4.9 0.55），于是最后一道闸
    #    **必然误报**，模型只能自己判断"这是正常的"再往下走 ——
    #    一条稳定的误报会把整份判据的可信度耗掉（模型此后会习惯性忽略报错）。
    #    另：**"页面导出失败" 由第 6 项（幻灯片页数 == pages[]）直接判定**，本项只管"是不是空壳"。
    if slide_count:
        per_page_kb = size_mb * 1024 / slide_count
        if per_page_kb < MIN_KB_PER_PAGE:
            errors.append(
                f"PPTX 页均仅 {per_page_kb:.0f} KB（< {MIN_KB_PER_PAGE} KB/页；"
                f"共 {slide_count} 页 / {size_mb:.2f} MB）—— 可能是空壳页：正文没嵌进去，"
                f"或导出时只写了占位符"
            )
        elif size_mb > 30:
            errors.append(f"PPTX 文件过大 ({size_mb:.1f} MB > 30 MB)，图片分辨率过高")
        else:
            print(f"  大小: {size_mb:.2f} MB（页均 {per_page_kb:.0f} KB / {slide_count} 页，"
                  f"媒体 {media_count} 个）(✅)")

# 3. svg_final 已恢复
with open(CONFIG, "r", encoding="utf-8") as f:
    config = json.load(f)
expected_pages = len(config["pages"])
final_svgs = sorted(f for f in os.listdir(FINAL) if f.endswith(".svg"))
actual_count = len(final_svgs)
if actual_count != expected_pages:
    errors.append(f"svg_final/ 文件数异常: 期望 {expected_pages}, 实际 {actual_count}")
else:
    print(f"svg_final/ 已恢复: {actual_count} 个文件 (✅)")

# 4. svg_final_bak 已清理
if os.path.exists(BAK):
    bak_count = len([f for f in os.listdir(BAK) if f.endswith(".svg")])
    errors.append(f"svg_final_bak/ 未清理 (残留 {bak_count} 个文件)")
else:
    print("svg_final_bak/ 已清理 (✅)")

# 5. PPTX 幻灯片页数 = pages[] 数组长度
#    🔴 这一项过去**只写在校验说明里、实现中并不存在**：
#    上面几项都只看 svg_final/ 的文件数，从不打开 pptx，
#    于是"导出时某页丢失"（幻灯片 14 页而 svg_final 仍是 15 个文件）
#    会让前面全部通过、并打印「✅ 导出验证全部通过」。
#    现在它也是"页面是否齐全"的**唯一**判据（大小那项只管空壳）。
if pptx_path and slide_count is not None:
    if slide_count != expected_pages:
        errors.append(
            f"PPTX 幻灯片页数异常: 期望 {expected_pages}, 实际 {slide_count}"
        )
    else:
        print(f"  幻灯片页数: {slide_count} 页 (✅ 与 pages[] 一致)")

if not errors:
    print("\n✅ 导出验证全部通过")
    sys.exit(0)
else:
    print(f"\n❌ 发现 {len(errors)} 个问题:")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
