#!/usr/bin/env python3
"""
reset_project.py — 重置 skill 到刚安装时的干净状态

删除所有用户数据和中间/最终产物：
  - assets/sources/ 里的全部内容（素材文件和 tmp/ 子目录）
  - assets/svg_body/ 整个目录（009 创作页正文片段）
  - assets/svg_output/ 整个目录
  - assets/svg_final/ 整个目录
  - assets/design_spec.md 和 assets/pipeline_config.json
  - icons/ 整个目录（运行期图标池，由 icon_sync.py 生成；在 skill 根目录）
  - .runtime/ 整个目录（§0d 检索通道预算账本；在 skill 根目录）
  - exports/ 整个目录（可用 --keep-exports 保留）
  - 各类中断残留（svg_final_bak*、.svg_final.candidate-* / publish-*）

保留不动：
  - assets/icons/ — 全局图标库（静态资产，勿删！）
  - assets/templates/ — 模板（根上的 009_content.svg + <风格名>/ 子目录里的门面页）
  - assets/images/ — 模板底图
  - scripts/ — 自动化脚本
  - references/ — 参考文档
  - SKILL.md — 顶层引导

用法:
  python scripts/cleaners/reset_project.py                # 全清（含 exports/）
  python scripts/cleaners/reset_project.py --keep-exports # 保留最终 pptx
  python scripts/cleaners/reset_project.py --dry-run      # 只预览，不删
  python scripts/cleaners/reset_project.py --help

⚠️ 本脚本是**破坏性操作**：无参数即执行删除。
   不确定时先跑 `--dry-run` 看清单；未知参数会被 argparse 拒绝并终止（不会误删）。
"""

import argparse
import os
import shutil
import sys

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ASSETS = os.path.join(SKILL_ROOT, "assets")

# 由 parse_args() 填充
KEEP_EXPORTS = False
DRY_RUN = False


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="reset_project.py",
        description="重置 skill 到刚安装时的干净状态（删除素材与全部中间/最终产物）",
        epilog="⚠️ 破坏性操作，无参数即执行删除。不确定时先加 --dry-run 预览。",
    )
    p.add_argument(
        "--keep-exports",
        action="store_true",
        help="保留 exports/ 目录（含最终 pptx），其余照清",
    )
    p.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="只列出将要删除的内容，不实际删除",
    )
    return p


def remove(target: str, label: str) -> bool:
    """删除文件或目录；DRY_RUN 时只打印计划。返回是否命中目标。"""
    if not os.path.exists(target):
        return False
    if DRY_RUN:
        kind = "目录" if os.path.isdir(target) and not os.path.islink(target) else "文件"
        print(f"  [预览] 将删除{kind} {label}")
        return True
    if os.path.isdir(target) and not os.path.islink(target):
        shutil.rmtree(target)
    else:
        os.remove(target)
    print(f"  ✓ {label} 已删除")
    return True


def main() -> int:
    global KEEP_EXPORTS, DRY_RUN
    args = build_parser().parse_args()
    KEEP_EXPORTS = args.keep_exports
    DRY_RUN = args.dry_run

    print("=== 重置项目到干净状态 ===")
    if DRY_RUN:
        print("（--dry-run 预览模式：不会删除任何文件）")
    print()

    # ── 1. sources/（素材与提取产物） ──
    remove(os.path.join(ASSETS, "sources"), "sources/")

    # ── 2. svg_body/（009 创作页正文片段） ──
    remove(os.path.join(ASSETS, "svg_body"), "svg_body/")

    # ── 3. svg_output/ ──
    remove(os.path.join(ASSETS, "svg_output"), "svg_output/")

    # ── 4. svg_final/ ──
    remove(os.path.join(ASSETS, "svg_final"), "svg_final/")

    # ── 5. exports/（在 skill 根目录，不在 assets 下） ──
    exports = os.path.join(SKILL_ROOT, "exports")
    if os.path.exists(exports):
        if KEEP_EXPORTS:
            print("  ⊘ exports/ 保留（--keep-exports）")
        else:
            remove(exports, "exports/")

    # ── 6. design_spec.md ──
    remove(os.path.join(ASSETS, "design_spec.md"), "assets/design_spec.md")

    # ── 7. pipeline_config.json ──
    remove(os.path.join(ASSETS, "pipeline_config.json"), "assets/pipeline_config.json")

    # ── 8. icons/（运行期图标池，由 icon_sync.py 生成；⚠️ 在 skill 根目录）
    #        🔴 别与 assets/icons/（全局图标库，静态资产）搞混 —— 只删根目录这个。
    remove(os.path.join(SKILL_ROOT, "icons"), "icons/（运行期图标池）")

    # ── 9. .runtime/（运行期状态账本：§0d 的检索通道预算计数） ──
    #        不清的话，上一轮花掉的必应 / 栏目页预算会带到下一轮 ⇒ 一开跑就被拒。
    remove(os.path.join(SKILL_ROOT, ".runtime"), ".runtime/（运行期状态）")

    # ── 10. svg_final_bak / svg_final_bak.tmp 等残留备份 ──
    for bak_name in ["svg_final_bak", "svg_final_bak.tmp"]:
        remove(os.path.join(SKILL_ROOT, bak_name), f"{bak_name}/（残留备份）")

    # ── 11. finalize_svg 硬中断残留的临时目录 ──
    if os.path.isdir(ASSETS):
        for name in sorted(os.listdir(ASSETS)):
            if name.startswith(".svg_final.candidate-") or name.startswith(".svg_final.publish-"):
                remove(os.path.join(ASSETS, name), f"assets/{name}/（残留临时目录）")

    if DRY_RUN:
        print()
        print("（预览结束：未做任何改动。去掉 --dry-run 才会真正删除。）")
    else:
        print()
        print("✅ 项目已重置到干净状态。")
        print("   保留不动：assets/icons/、assets/templates/、assets/images/、"              "scripts/、references/、SKILL.md"
              + ("、exports/（--keep-exports）" if KEEP_EXPORTS else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
