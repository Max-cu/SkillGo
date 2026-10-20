#!/usr/bin/env python3
"""
apply_palette.py — 按配色方案替换 SVG 中的硬编码色值

🔴🔴 适用范围（2026-10-09 用户拍板）：
    **只服务创作页（009）**。门面页（001/002/003/010）的风格由「整套模板」承载 ——
    想给门面页换风格，正确做法是换一整套门面页模板（assets/templates/<风格名>/），
    不是给旧模板刷漆。build_svgs.py 已限定只对 009_content.svg 调用本脚本。
    本脚本自身仍可处理任意 SVG（--files/--src 手动指定），但**正常流程不会这么用**。

设计要点（重要）：
  * **不改模板**。模板 SVG 永远保持原生色值（= 默认蓝白方案），
    因此即使本脚本失败，模板本身仍可独立渲染出一份完整的默认配色稿。
  * 本脚本只读模板 / 写输出目录，**从不写回 assets/templates/**。

数据流：
    roles.json        「色值 → 角色」   （人写，一次）
        +                 ↓
    <palette>.json    「角色 → 色值」   （每套方案一份）
        ↓ 合成
    替换表 { "#003C96": "#E8703A", ... }
        ↓
    对 SVG 做全文色值替换

用法:
    python scripts/apply_palette.py --palette default              # 预览（dry-run）
    python scripts/apply_palette.py --palette default --write      # 实际写出
    python scripts/apply_palette.py --palette minimal --write \
        --src assets/templates/default --out assets/svg_output \
        --files 009_content.svg
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
PALETTE_DIR = PROJECT / "assets" / "palettes"
ROLES_PATH = PALETTE_DIR / "roles.json"
TEMPLATES_ROOT = PROJECT / "assets" / "templates"
DEFAULT_STYLE = "default"


def resolve_style_name(meta: dict | None,
                       palette_name: str | None = None,
                       templates_root: Path | None = None,
                       warn: bool = True) -> tuple[str, bool]:
    """解析门面页风格名 —— **三个消费方共用这一份**（避免三处逻辑漂移）。

    取值优先级（🔴 2026-10-09 方案 C-1，用户拍板：配色与门面页 **原子同步**）：
      1. `_meta.template_set` **显式声明** → 用它（供「不同名混搭」用，此时**不做原子回退**）。
      2. 未声明 → 从 palette 派生：`templates/<palette值>/` 存在则用同名风格。
      3. 不存在 / 都没有 → `default`。
         ⚠️ **此时调用方（build_svgs）必须把配色也一并回退** ——
         用户口径是「minimal 是一整套，**要么都换、要么都不换**」。
         本函数只负责给名字；**原子性由 `build_svgs.resolve_palette()` 的 `atomic_style` 参数实施**。

    Returns:
        (风格名, 是否由 palette 派生)

    ⚠️ **只在"显式声明的风格目录不存在"时才该报错** —— 那是真写错了，
       由调用方（build_svgs）自行决定是否 raise。本函数**只负责给出名字**。
    """
    root = Path(templates_root) if templates_root else TEMPLATES_ROOT
    meta = meta or {}
    declared = meta.get("template_set")          # 可能是 None / 空串

    if declared:
        return str(declared), False

    if palette_name and palette_name != DEFAULT_STYLE:
        if (root / palette_name).is_dir():
            return palette_name, True
        if warn:
            print()
            print("=" * 64)
            print(f"ℹ️  「{palette_name}」尚未就绪 —— 整套回退 default（商务风）")
            print(f"    门面页风格 {palette_name!r} 缺失: assets/templates/{palette_name}/")
            print(f"    ⇒ 配色也一并回退 default（配色与门面页**要么都换、要么都不换**）")
            print(f"    提示: 待该目录放齐同名门面页后，同一条提示词将自动整套升级为 {palette_name}")
            print("=" * 64)
            print()
    return DEFAULT_STYLE, False

# 色值匹配：只认 6 位十六进制
HEX_RE = re.compile(r"#[0-9A-Fa-f]{6}")

# 保留角色（不参与替换）
# ⚠️ `__IGNORE_PHOTO__` 自 2026-10-09 起已无登记来源（原 #6AB2E5 是门面页专属色，已移出 roles）。
#    保留在集合里是为**向后兼容**：若将来有方案重新登记它，行为与旧版一致。
RESERVED = {"__KEEP__", "__IGNORE_PHOTO__"}


def safe_print(text: str) -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        import io

        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
        print(text)


def load_json(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"未找到文件: {path}")
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def build_mapping(roles: dict, palette: dict) -> dict[str, str]:
    """合成「旧色值 → 新色值」替换表。

    roles:   { "#003C96": "ROLE_PRIMARY", ... }
    palette: { "tokens": { "ROLE_PRIMARY": "#1D9E75", ... } }
    """
    tokens = palette.get("tokens", {})
    mapping: dict[str, str] = {}
    unresolved: list[str] = []

    for old_hex, role in roles.items():
        old_up = old_hex.upper()

        if role in RESERVED:
            continue  # 中性色 / 照片取样色：不动

        new_hex = tokens.get(role)
        if not new_hex:
            unresolved.append(f"{old_hex} -> {role}（方案中缺少该角色）")
            continue

        mapping[old_up] = new_hex.upper()

    if unresolved:
        raise ValueError(
            "以下角色在方案中无对应色值，无法合成替换表：\n  - "
            + "\n  - ".join(unresolved)
        )
    return mapping


def apply_mapping(svg_text: str, mapping: dict[str, str]) -> tuple[str, dict[str, int]]:
    """对 SVG 文本做全文色值替换，返回 (新文本, 每个旧色被替换的次数)。"""
    counts: dict[str, int] = {}

    def _repl(m: re.Match) -> str:
        found = m.group(0).upper()
        if found in mapping:
            counts[found] = counts.get(found, 0) + 1
            return mapping[found]
        return m.group(0)

    return HEX_RE.sub(_repl, svg_text), counts


def main() -> int:
    parser = argparse.ArgumentParser(
        description="按配色方案替换 SVG 硬编码色值（不修改模板）"
    )
    parser.add_argument("--palette", default="default", metavar="NAME",
                        help="方案名（对应 assets/palettes/<NAME>.json，默认 default）")
    parser.add_argument("--src", default="assets/templates/default", metavar="DIR",
                        help="源 SVG 目录（默认 assets/templates/default）")
    parser.add_argument("--out", default="assets/svg_output", metavar="DIR",
                        help="输出目录（默认 assets/svg_output）")
    parser.add_argument("--write", action="store_true",
                        help="实际写出文件；不加则仅预览（dry-run）")
    parser.add_argument("--files", nargs="*", metavar="SVG",
                        help="只处理指定文件（默认目录下全部 .svg）")
    parser.add_argument("--verbose", action="store_true", help="打印每处替换明细")
    args = parser.parse_args()

    src_dir = (PROJECT / args.src) if not Path(args.src).is_absolute() else Path(args.src)
    out_dir = (PROJECT / args.out) if not Path(args.out).is_absolute() else Path(args.out)

    roles_doc = load_json(ROLES_PATH)
    roles = roles_doc["roles"]
    palette = load_json(PALETTE_DIR / f"{args.palette}.json")
    mapping = build_mapping(roles, palette)

    safe_print(f"配色方案：{palette.get('name', args.palette)}（id={args.palette}）")
    safe_print(f"角色表　：{ROLES_PATH.name}（{len(roles)} 个色值登记）")
    safe_print(f"替换表　：{len(mapping)} 条")
    for old, new in sorted(mapping.items()):
        flag = "" if old == new else "   ← 变色"
        safe_print(f"    {old} → {new}{flag}")

    if not src_dir.exists():
        safe_print(f"[ERROR] 源目录不存在: {src_dir}")
        return 2

    files = sorted(src_dir.glob("*.svg"))
    if args.files:
        keep = {f.lower() for f in args.files}
        files = [f for f in files if f.name.lower() in keep]

    if not files:
        safe_print(f"[ERROR] 未找到任何 .svg: {src_dir}")
        return 2

    if args.write:
        out_dir.mkdir(parents=True, exist_ok=True)

    safe_print("")
    total_repl = 0
    for svg_path in files:
        text = svg_path.read_text(encoding="utf-8")
        new_text, counts = apply_mapping(text, mapping)
        n = sum(counts.values())
        total_repl += n

        # 校验：替换后不应残留任何"本应在表内"的旧色值
        leftover = [m.group(0).upper() for m in HEX_RE.finditer(new_text)
                    if m.group(0).upper() in mapping and mapping[m.group(0).upper()] != m.group(0).upper()]
        # 注意：若新色恰等于旧色（默认方案），leftover 判定需放宽
        changed = {k: v for k, v in mapping.items() if k != v}
        residual = [m.group(0).upper() for m in HEX_RE.finditer(new_text)
                    if m.group(0).upper() in changed]

        status = "[OK]"
        if residual:
            status = "[WARN] 替换后仍残留旧色"

        if args.write:
            (out_dir / svg_path.name).write_text(new_text, encoding="utf-8")
            action = f"→ {out_dir.name}/{svg_path.name}"
        else:
            action = "(dry-run，未写出)"

        safe_print(f"  {status} {svg_path.name}: {n} 处色值  {action}")
        if args.verbose:
            for c, k in sorted(counts.items(), key=lambda x: -x[1]):
                safe_print(f"        {c} × {k} → {mapping[c]}")

    safe_print("")
    safe_print(f"合计替换 {total_repl} 处。")
    if not args.write:
        safe_print("（dry-run 模式，未写出任何文件；加 --write 实际写出）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
