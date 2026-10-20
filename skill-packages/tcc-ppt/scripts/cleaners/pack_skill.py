#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pack_skill.py —— 把本 skill 打包成可分发的 zip（供平台挂载）。

🔴🔴 **为什么要写成脚本，而不是每次现写 `os.walk`**（2026-10-10 事故）：
  手写打包时把排除项写成了**裸目录名** `"icons"`，而 `os.walk` 是按**目录名**匹配的，
  于是 `assets/icons/`（全局图标库，8,352 个 SVG）被**一并误杀** ——
  打出的包只有 176 个文件（正常 8,557），缺了整个图标库。
  ⚠️ **同名的两个 `icons/` 性质完全相反**（这正是 `reset_project.py` 反复强调的点）：

    | 路径 | 性质 | 打包时 |
    |---|---|---|
    | `assets/icons/` | **全局图标库**（4 库 8,352 SVG，静态资产） | ✅ **必须打进包** |
    | `<skill根>/icons/` | 本项目图标池（`icon_sync.py` 运行期生成） | ❌ 排除 |

  ⇒ **判定口径：排除项一律写成「相对 skill 根的绝对子路径」，禁用裸目录名匹配。**
     本脚本用 `_rel(path)` 得到相对于 skill 根的子路径再比对，从根上杜绝这个坑。

用法：
    python scripts/cleaners/pack_skill.py                     # 打包到 ../<skill名>.zip
    python scripts/cleaners/pack_skill.py -o /tmp/x.zip       # 指定输出
    python scripts/cleaners/pack_skill.py --check             # 只核验既有 zip 的完整性
    python scripts/cleaners/pack_skill.py --list              # 只列出会打/会排除的路径
"""
from __future__ import annotations

import argparse
import os
import sys
import zipfile
from pathlib import Path

# ── 路径 ────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent          # <skill根>/scripts/cleaners
SKILL_ROOT = SCRIPT_DIR.parent.parent                 # <skill根>
SKILL_NAME = SKILL_ROOT.name                          # 目录名即 skill 名
DEFAULT_OUT = SKILL_ROOT.parent / f"{SKILL_NAME}.zip"  # 与 skill 目录同级

# ── 排除项：🔴 一律写成「相对 skill 根的 POSIX 子路径」，禁用裸目录名 ──
EXCLUDE_DIRS_REL = {
    # 运行期 / 中间产物（可清、不该进包）
    "exports",                    # 🔴 skill 根下的最终产物（保留一份在用户手上即可）
    "icons",                      # 🔴 skill 根下的运行期图标池 —— ⚠️ 不是 assets/icons！
    "assets/sources",             # 素材（文本 + 网图 + facts）
    "assets/svg_body",            # 009 正文片段
    "assets/svg_output",          # build 产物
    "assets/svg_final",           # finalize 产物
    ".runtime",                   # 🔴 运行期状态账本（§0d 检索预算 + 事实扩展计数）
    "__pycache__",                # 任意层级的字节码缓存（按名匹配，见下）
}

EXCLUDE_FILES_REL = {
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
    "assets/design_spec.md",      # 每轮任务的产物（用户数据，不进分发包）
    "assets/pipeline_config.json",  # 同上
}
EXCLUDE_EXT = {".pyc", ".pyo"}

# 🔑 按「目录名」而非「相对路径」排除的少数特例（这些目录名不会与静态资产重名）
EXCLUDE_BY_DIRNAME = {"__pycache__"}

# 打包后必须存在的关键路径（缺任何一个 ⇒ 包不可用）
MUST_EXIST = [
    "SKILL.md",
    "assets/icons/README.md",
    "assets/icons/tabler-filled",
    "assets/templates/default",
    "assets/templates/minimal",
    "assets/templates/vibrant",
    "assets/palettes/roles.json",
    "assets/palettes/vibrant.json",
    "assets/images",
    "references/pitfalls.md",
    "scripts/build_svgs.py",
    "scripts/export_flat.py",
]
# 图标库数量下限（4 个风格库，8,300+ SVG —— 少于这个数说明又没打进去）
MIN_ICON_SVGS = 8000


def _rel(p: Path) -> str:
    """相对 skill 根的 POSIX 子路径（不在其下则返回 ''）。"""
    try:
        return p.relative_to(SKILL_ROOT).as_posix()
    except ValueError:
        return ""


def _is_excluded_dir(d: Path) -> bool:
    rel = _rel(d)
    if rel in EXCLUDE_DIRS_REL or d.name in EXCLUDE_BY_DIRNAME:
        return True
    # 任意层级下的 __pycache__
    return d.name == "__pycache__"


def _is_excluded_file(f: Path) -> bool:
    if f.name in EXCLUDE_FILES_REL or f.suffix.lower() in EXCLUDE_EXT:
        return True
    return _rel(f) in EXCLUDE_FILES_REL


def collect() -> list[tuple[Path, str]]:
    """(磁盘路径, zip 内 arcname) 列表。arcname 含顶层目录名。"""
    out: list[tuple[Path, str]] = []
    for root, dirs, files in os.walk(SKILL_ROOT):
        rp = Path(root)
        dirs[:] = sorted(d for d in dirs if not _is_excluded_dir(rp / d))
        for name in sorted(files):
            p = rp / name
            if _is_excluded_file(p):
                continue
            # zip 内路径 = <skill名>/<相对路径>，保住顶层目录名
            arc = f"{SKILL_NAME}/{p.relative_to(SKILL_ROOT).as_posix()}"
            out.append((p, arc))
    return out


def verify(zip_path: Path, verbose: bool = True) -> bool:
    """核验 zip 完整性 —— 🔑 必须查「文件数 + 图标数」，别只查几个 md 关键词。"""
    ok = True
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        files = [n for n in names if not n.endswith("/")]
        icon_svgs = [n for n in files if n.startswith(f"{SKILL_NAME}/assets/icons/")
                     and n.endswith(".svg")]
        if verbose:
            print(f"▸ {zip_path.name}")
            print(f"    总条目 {len(names)}（文件 {len(files)}）")
            print(f"    图标 SVG {len(icon_svgs)} 个（下限 {MIN_ICON_SVGS}）")

        for rel in MUST_EXIST:
            key = f"{SKILL_NAME}/{rel}"
            hit = key in names or any(n.startswith(key + "/") for n in names)
            if not hit:
                ok = False
                print(f"    ❌ 缺关键路径 {rel}")

        if len(icon_svgs) < MIN_ICON_SVGS:
            ok = False
            print(f"    ❌ 图标库不完整（{len(icon_svgs)} < {MIN_ICON_SVGS}）"
                  f" —— 大概率又被裸目录名 `icons` 误杀了")

        # 🔑 风格配对一致性（2026-10-14 加）：每个配色方案都应有同名门面页目录。
        #    —— 只 `⚠️` 不 `❌`：pitfalls 明说"只有配色、还没建模板目录"是**正常中间态**
        #    （运行时原子回退 default 兜底）。但对**分发用的包**来说，漏掉一套门面页
        #    就意味着"选了某风格却拿不到它的门面页"，值得提示。
        _pp = f"{SKILL_NAME}/assets/palettes/"
        _pt = f"{SKILL_NAME}/assets/templates/"
        palette_ids = {n[len(_pp):-5] for n in files
                       if n.startswith(_pp) and n.endswith(".json")
                       and "/" not in n[len(_pp):] and n[len(_pp):] != "roles.json"}
        template_dirs = {n[len(_pt):].split("/")[0] for n in files
                         if n.startswith(_pt) and "/" in n[len(_pt):]}
        orphan = sorted(palette_ids - template_dirs)
        if orphan:
            print(f"    ⚠️ 配色方案缺同名门面页目录: {', '.join(orphan)}"
                  f" —— 选它时会整套原子回退 default（中间态正常，但分发包内建议补齐）")
        if verbose:
            print(f"    风格配对：配色 {sorted(palette_ids)} ↔ 门面页 {sorted(template_dirs)}")

        # 反查：运行期产物不该在包里
        for bad in ("exports/", "/icons/", "/.runtime/"):
            hit = [n for n in files if bad == "/icons/"
                   and n.startswith(f"{SKILL_NAME}/icons/")]
            if bad == "exports/":
                hit = [n for n in files if f"{SKILL_NAME}/exports/" in n]
            if bad == "/.runtime/":
                hit = [n for n in files if f"{SKILL_NAME}/.runtime/" in n]
            if hit:
                print(f"    ⚠️ 包含运行期产物 {bad}（{len(hit)} 项）—— 非致命，但建议排除")

    print("    ✅ 完整性通过" if ok else "    ❌ 完整性不通过")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="打包本 skill 为可分发的 zip")
    ap.add_argument("-o", "--output", type=Path, default=DEFAULT_OUT,
                    help=f"输出 zip 路径（默认 {DEFAULT_OUT}）")
    ap.add_argument("--check", action="store_true",
                    help="不打包，只核验既有 zip 的完整性")
    ap.add_argument("--list", action="store_true",
                    help="只列出会打进包的路径，不写文件")
    ap.add_argument("--no-verify", action="store_true", help="打包后跳过核验")
    args = ap.parse_args()

    if args.check:
        if not args.output.exists():
            print(f"❌ {args.output} 不存在")
            return 1
        return 0 if verify(args.output) else 1

    entries = collect()
    total = sum(p.stat().st_size for p, _ in entries)
    n_icons = sum(1 for _, a in entries
                  if a.startswith(f"{SKILL_NAME}/assets/icons/") and a.endswith(".svg"))

    if args.list:
        for _, a in entries:
            print(a)
        print(f"\n共 {len(entries)} 项 / {total/1024/1024:.1f} MB（其中图标 {n_icons} 个）")
        return 0

    out = args.output
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        bak = out.with_suffix(out.suffix + ".bak")
        bak.write_bytes(out.read_bytes())
        print(f"[BAK ] 旧包已备份 → {bak.name}（{bak.stat().st_size/1024/1024:.1f} MB）")

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p, arc in entries:
            z.write(p, arc)

    print(f"[ZIP ] {out}")
    print(f"       文件 {len(entries)} 个（图标 {n_icons}）"
          f"／原始 {total/1024/1024:.1f} MB／压缩后 {out.stat().st_size/1024/1024:.1f} MB")

    if args.no_verify:
        return 0
    return 0 if verify(out) else 1


if __name__ == "__main__":
    sys.exit(main())
