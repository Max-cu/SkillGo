#!/usr/bin/env python3
"""抓取阶段的「素材够了吗」正向量尺 —— 🔵 只读，无副作用。

为什么存在
----------
§0d 原来的停止判据是**负向**的：「连续 2 次无新增可用料」。当每次抓取都有新增时，
它永远不触发 ⇒ 模型就一路「我觉得还不够」地抓下去。

2026-10-12 实测（中国新能源汽车）：**14 次检索 / 12 次抓取**，素材准备里约 **7 分 45 秒**
花在「失败 → 重新决策」上；而最终 6 篇里，**第 4 篇时材料其实已经够用**
（§0e 报正文体量 116%）。耗时不在抓取（脚本总计 40 秒），在「反复判断够不够」。

本脚本给抓取阶段一个**正向量尺**：抓 3~4 篇后跑一次，够了就收手。

口径
----
* 只扫 ``assets/sources/_research/*.md``（抓取件），**不碰 `文本.md`**、不写任何文件。
* **净正文** = 去掉爬虫注释头与**存盘追加的 meta 两行**、再剔除链接列表段之后剩下的字符数
  （正文内容行**一个不滤** —— 见 `strip_stored_meta()` 与 pitfalls 的「输入同源」教训）。
* 壳页判定与 ``scripts/source_to_md/web_to_md.py`` 的 ``_prose_ratio_warning`` **同源**
  （``PROSE_*`` 常量与判据都复制于此 —— 🔴 **改一边必须同步另一边**）。
  ⚠️ **输入也要同源**：`web_to_md` 判的是「追加 meta 两行之前」的正文，本脚本读的却是
  存盘文件 ⇒ 必须先经 `strip_stored_meta()` 剥掉那两行（2026-10-15 补）。
* 🔵 **经验阈值来自两次实测，不是理论值**：

  | 实测 | 抓取 | 提炼后净正文 | 结果 |
  |---|---|---|---|
  | 中国大运河 | 3 篇 | 2,032 字符 | ✅ 通过 |
  | 中国新能源汽车 | 6 篇 | 1,856 字符 | ✅ 通过（其实 4 篇就够了）|

  ⇒ 抓取件净正文 **≥ 5,000 字符** 且 **可用 ≥ 3 篇**，通常就撑得起 15 页。
* ⚠️ 本脚本给的是**抓取阶段的粗判**；最终判据永远是 §0e 的「正文体量评估」——
  那是**提炼后**的真实体量，两者不要互相替代。

用法::

    python scripts/validators/check_research_volume.py --pages 15 --facade 7
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from console_encoding import configure_utf8_stdio  # noqa: E402

SKILL_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RESEARCH_DIR = SKILL_ROOT / "assets" / "sources" / "_research"

# ── 壳页判据（🔴 与 scripts/source_to_md/web_to_md.py 同源）──
BODY_ABSOLUTE_MIN_CHARS = 200    # 比这还短 ⇒ 直接判壳页
PROSE_MIN_BLOCK_CHARS = 120      # 多长算「像正文的一段」
PROSE_RATIO_MIN = 0.30           # 正文占比低于此值 ⇒ 疑似栏目壳页
PROSE_MIN_BLOCKS = 3             # 长段少于这么多，必判可疑
PROSE_MIN_TOTAL_CHARS = 800      # 太短的页面交给 BODY_ABSOLUTE_MIN_CHARS，比例判据不掺和
PROSE_CHARS_MAX = 800            # 正文绝对量超过此值 ⇒ 不判壳页（真文章特征）
# 🔴 2026-10-15：1000 → 800（与 web_to_md.py **同源，改一边必须同步另一边**）。
#    实测两篇被误杀的真文章（d1ev 净正文 942 字 / 新华网 858 字）绝对量都落在
#    800~1000 之间 ⇒ 既跨不过占比线、也够不着豁免线，被双重误判。下调后两篇均放行；
#    栏目页的正文多在几百字，仍拦得住（WAF 页另有 WAF_CJK_RATIO_MIN 兜底）。

# ── 反爬页判据（2026-10-13 新增 · 🔴 与 web_to_md.py 的 WAF_CJK_RATIO_MIN 同源）──
# WAF 挑战页：HTTP 200、正文 3,711 字符、零告警，内容是混淆 JS；"正文占比"甚至
# 99%（JS 里没有 Markdown 链接）⇒ 上面三条判据全部放行。唯一稳定的分界是**中文占比**
# （实测 WAF 0.3% vs 真文章 71.4%，差 200 倍）。
WAF_CJK_RATIO_MIN = 0.05

# ── 抓取阶段的经验线（🔵 来自两次实测，见模块 docstring）──
EXPECT_NET_CHARS = 5000
EXPECT_FILES = 3

# ── 目标正文口径（🔴 与 verify_extraction.py 的 TEXT_PER_PAGE 同源）──
TEXT_PER_PAGE = 200


def strip_meta(md: str) -> str:
    """Drop the crawl header comment."""
    return re.sub(r"<!--.*?-->", "", md, flags=re.S)


# 🔴 web_to_md **存盘时**会在注释头之后追加 `# 标题` / `> 描述` 两行（见 web_to_md.py
#    里 `final_output` 的构造）；而它的 `_prose_ratio_warning` 判的是**追加之前**的
#    `markdown_text` ⇒ 两边**输入不同源**（2026-10-15 实测暴露）。
_STORED_META_RE = re.compile(r"\A\s*#[^\n]*\n(?:\s*>[^\n]*\n)?")


def strip_stored_meta(raw: str, body: str) -> str:
    """剥掉 web_to_md 存盘时追加的 `# 标题` / `> 描述` 两行（只剥文件开头这两行）。

    🔴 目的：让本脚本的判定输入与 ``web_to_md._prose_ratio_warning`` **完全相同**。
    2026-10-15 实测：d1ev《8月终端车市TOP20》抓取时占比 **30.20%**（放行），存盘后
    因这两行变成 **29.04%（判壳）** ⇒ 只差 0.96 个点跨过 30% 阈值；同一轮另一篇
    真文章（新华网，正文 858 字）也被判壳 ⇒ 报「可用 0 篇」、模型据此**终止**。

    ⚠️ 只剥「注释头之后**紧跟**的 1 行 `# …` + 最多 1 行 `> …`」——那是 web_to_md
       固定追加的位置；**绝不滤正文里的内容行**：在正文中挑行丢掉会让 total 变小、
       ratio 抬高，把边界壳页**误放行**（见 pitfalls「预处理不同 ⇒ 边界样本翻面」，
       2026-10-12 实测）。判据 = **剥掉的行是否由爬虫自己追加**。
    """
    if "<!--" not in raw:      # 不是爬虫存盘件（手工写的 md）⇒ 原样返回
        return body
    return _STORED_META_RE.sub("", body, count=1)


def analyse(md_path: Path) -> dict:
    """Return net prose chars / paragraph count / shell-page verdict."""
    raw = md_path.read_text(encoding="utf-8", errors="replace")
    # 🔴 与 web_to_md._prose_ratio_warning 的口径**逐字一致**：
    #    只去 HTML 注释 + **存盘追加的 meta 两行**，其余**不做任何行过滤**。
    #    ⚠️ 若在正文里滤掉标题行 / 图片行，total 会变小、ratio 被抬高
    #    ⇒ 把「22% 的边界壳页」误判成可用（2026-10-12 实测踩到）。
    body = strip_stored_meta(raw, strip_meta(raw))

    total = len(re.sub(r"\s+", "", body))

    prose_chars = 0
    prose_blocks = 0
    for block in (b.strip() for b in re.split(r"\n\s*\n", body)):
        if not block:
            continue
        # 整段是不是「链接列表」—— 剥掉链接后剩不下多少字 ⇒ 不算正文
        stripped = re.sub(r"!?\[[^\]]*\]\([^)]*\)", "", block)
        stripped = re.sub(r"https?://\S+", "", stripped)
        stripped = re.sub(r"[\s\-*|>#·\[\]()【】]+", "", stripped)
        if len(stripped) < 80:
            continue
        text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", block)
        text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
        text = re.sub(r"https?://\S+", "", text)
        text = re.sub(r"^[>#|\-*\s]+", "", text).strip()
        chars = len(re.sub(r"\s+", "", text))
        if chars >= PROSE_MIN_BLOCK_CHARS:
            prose_chars += chars
            prose_blocks += 1

    ratio = prose_chars / total if total else 0.0
    if total < BODY_ABSOLUTE_MIN_CHARS:
        shell = True
    elif total < PROSE_MIN_TOTAL_CHARS:
        shell = False
    elif prose_blocks >= PROSE_MIN_BLOCKS and ratio >= PROSE_RATIO_MIN:
        shell = False
    elif prose_chars >= PROSE_CHARS_MAX:
        shell = False
    else:
        shell = True

    # 反爬页（2026-10-13）：字符数够、"正文占比"甚至高于真文章 ⇒ 上面全放行。
    cjk_ratio = (
        len(re.findall(r"[\u4e00-\u9fff]", body)) / total if total else 1.0
    )
    waf = total >= BODY_ABSOLUTE_MIN_CHARS and cjk_ratio < WAF_CJK_RATIO_MIN

    return {
        "name": md_path.name,
        "net_chars": prose_chars,
        "blocks": prose_blocks,
        "shell": shell,
        "waf": waf,
    }


def main(argv: list[str] | None = None) -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(
        description="抓取阶段的素材量估算（只读）—— 够了就停。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
例：
  python scripts/validators/check_research_volume.py --pages 15 --facade 7
""",
    )
    parser.add_argument("--pages", type=int, default=15, help="总页数（默认 15）")
    parser.add_argument("--facade", type=int, default=7, help="门面页数（默认 7）")
    parser.add_argument(
        "--research-dir",
        default=str(DEFAULT_RESEARCH_DIR),
        help="抓取件目录（默认 assets/sources/_research）",
    )
    args = parser.parse_args(argv)

    rdir = Path(args.research_dir)
    print("🔍 抓取阶段素材估算（只读 · 不碰 文本.md）")
    print(f"   目录：{rdir}")

    if not rdir.is_dir():
        print("   ⚠️ 目录不存在 —— 还没有抓取件？先跑 §0d C 段抓正文。")
        return 0

    files = sorted(p for p in rdir.glob("*.md"))
    if not files:
        print("   ⚠️ 目录里没有 .md —— 先跑 §0d C 段抓正文。")
        return 0

    rows = [analyse(p) for p in files]
    usable = [r for r in rows if not r["shell"] and not r["waf"]]
    net_total = sum(r["net_chars"] for r in usable)

    print()
    print("   %-34s %9s %6s   %s" % ("文件", "净正文", "段落", "判定"))
    for r in rows:
        if r["waf"]:
            mark = "❌ 反爬页（弃用）"
        elif r["shell"]:
            mark = "❌ 壳页（弃用）"
        else:
            mark = "✅ 可用"
        print("   %-34s %9s %6d   %s"
              % (r["name"][:34], f"{r['net_chars']:,}", r["blocks"], mark))

    target = max(args.pages - args.facade, 0) * TEXT_PER_PAGE
    print()
    all_net = sum(r["net_chars"] for r in rows)
    print(f"   可用 {len(usable)} 篇 / {len(rows)} 个文件 ｜ 可用篇净正文合计 {net_total:,} 字符"
          f"（全部文件合计 {all_net:,} 字符）")
    print()
    print(f"   目标（{args.pages} 页 / 门面 {args.facade} → 内容页 {max(args.pages - args.facade, 0)}）："
          f"期望**提炼后**正文 {target:,} 字符")
    print(f"   经验线（抓取件净正文）：≥ {EXPECT_NET_CHARS:,} 字符 且 可用 ≥ {EXPECT_FILES} 篇")
    print()

    short_files = max(EXPECT_FILES - len(usable), 0)
    short_chars = max(EXPECT_NET_CHARS - net_total, 0)
    if not short_files and not short_chars:
        print("   ✅ 够了 —— 停止抓取，进入 D 段「写 facts.txt → 落盘」。")
    else:
        if short_files:
            print(f"   ⚠️ 篇数不足：{len(usable)} < {EXPECT_FILES} ⇒ 建议再抓 {short_files} 篇")
        if short_chars:
            print(f"   ⚠️ 净正文不足：{net_total:,} < {EXPECT_NET_CHARS:,} ⇒ 还差约 {short_chars:,} 字符")
        print("   （若已换过通道、确实搜不到更好的源 ⇒ 直接进 D 段，用 §0e 的真实体量再判一次）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
