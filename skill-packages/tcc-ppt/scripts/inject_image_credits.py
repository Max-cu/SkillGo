#!/usr/bin/env python3
"""Step 2f：给 009 创作页自动注入来源标注（**图片来源行 + 内容来源行**，契约 §1.6）。

职责边界（用户 2026-09-23 定；2026-10-08 扩到两类）：
    · Step 1（design_spec §VIII）—— 标注每张图是「实拍」还是「网图」
    · §0d（联网取材）—— 产出 `_web_text_facts.json`（网文事实 + 来源）
    · Step 2（本脚本）—— 正文片段写完后，按**实际引用**自动插入来源标注
    · Step 3~5 —— 只负责生成；`export_flat.py` **不碰内容**

判定逻辑（两类，各自的锚点不同）：
    ① **图片来源行**：扫 `svg_body/*.svg` 里**实际引用**的 `image_NNN`
       → 与 `文本.md` 中标记为「网图」的编号求**交集** → 非空才加。
    ② **内容来源行**：扫片段里的 **`<!--FACTS: F001,F003-->` 锚点**
       → 到 `_web_text_facts.json` 查站点 → 非空才加。
       🔑 文字没有 `image_NNN` 那种天然编号，**只能靠这个显式锚点**。

上下顺序（契约 §1.6，用户 2026-10-08 拍板「图片在上、文字在下」）：
    只有一类 → 该行基线 `y=690`
    两类都有 → 图片来源 `y=668`（上）／ 内容来源 `y=690`（下）
    🔑 **"最底那一行"永远钉在 690** ⇒ 距画布底恒定 ~25px，原值不必改。

幂等机制：
    两个标注块分别用 `<!--IMG_CREDIT_BEGIN-->`/`<!--IMG_CREDIT_END-->` 与
    `<!--TEXT_CREDIT_BEGIN-->`/`<!--TEXT_CREDIT_END-->` 包裹，重跑**先删后插**。
    `check_creative_svg.py` 校验前会剥掉 XML 注释，所以标记本身不影响合规检查。
    ⚠️ **`<!--FACTS: …-->` 锚点不属于标注块，不会被删** —— 它是「这页用了哪几条外部事实」
       的唯一凭据，要一直留在片段里。

用法：
    python scripts/inject_image_credits.py                # 全量注入
    python scripts/inject_image_credits.py --dry-run      # 只报告不落盘
    python scripts/inject_image_credits.py --page page04.svg   # 只处理某页
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT = SCRIPT_DIR.parent
SOURCES = PROJECT / "assets" / "sources"
BODY_DIR = PROJECT / "assets" / "svg_body"
MD_PATH = SOURCES / "文本.md"
FACTS_PATH = SOURCES / "_web_text_facts.json"

# ── 标注规格（creative_contract.md §1.6，改动须同步契约）──
CREDIT_X = 40
CREDIT_Y_SINGLE = 690        # 只有一类时：该行基线（字形顶≈676、底≈695；画布 720 → 距底约 25px）
CREDIT_Y_IMG_BOTH = 668      # 两类都有时：图片来源行（在**上**）
CREDIT_Y_TEXT_BOTH = 690     # 两类都有时：内容来源行（在**下**，永远占最底）
CREDIT_FONT_SIZE = 18.67
CREDIT_FILL = "#808080"      # 来源标注专用浅灰（例外色，不在 9 色语义体系内，见契约 §1.6）
IMG_ROLE = "img-credit"      # ⚠️ 与 check_creative_svg.py 的 CREDIT_ROLES 必须一致
TEXT_ROLE = "text-credit"

IMG_LABEL = "图片来源：网络 · 仅作示意"
TEXT_LABEL = "内容来源：网络 · 仅供参考"

IMG_BEGIN = "<!--IMG_CREDIT_BEGIN-->"
IMG_END = "<!--IMG_CREDIT_END-->"
TEXT_BEGIN = "<!--TEXT_CREDIT_BEGIN-->"
TEXT_END = "<!--TEXT_CREDIT_END-->"

# 匹配 文本.md 里的网图登记行：[[image_015.jpg]]（网图 | 主题：… | 来源：xxx | …）
# 🔴 行锚定 + 贪婪，**不要**用 `[^）]*`：图片标题里常有全角括号（如
# 「莫高窟（九层楼）」），非贪婪字符类会在第一个 `）` 处截断，把后面的
# 「来源：」整段丢掉 —— 表现为图片标注只剩标签、连站点都没有。
WEB_ENTRY_RE = re.compile(
    r"^\[\[(image_(\d+)\.\w+)\]\]\s*（网图\s*\|(?P<fields>.*)）\s*$",
    re.MULTILINE,
)
SOURCE_FIELD_RE = re.compile(r"来源：([^|）]+)")

# svg 里实际的图片引用（只认 href，避免误判普通文字）
HREF_RE = re.compile(r'(?:xlink:)?href="[^"]*?(image_(\d+)\.\w+)"')

# 事实锚点（契约 §1.6 / step_0 §0d）：<!--FACTS: F001,F003-->
FACTS_RE = re.compile(r"<!--\s*FACTS\s*:\s*(?P<ids>[^>]*?)\s*-->")
FACT_ID_RE = re.compile(r"F\d+")


def load_web_images() -> dict[int, dict]:
    """解析 文本.md，返回 {图片编号: {name, host}}（仅「网图」）。"""
    if not MD_PATH.is_file():
        print("❌ 文本.md 不存在 —— 请先执行素材准备")
        sys.exit(1)

    md = MD_PATH.read_text(encoding="utf-8")
    out: dict[int, dict] = {}
    for m in WEB_ENTRY_RE.finditer(md):
        num = int(m.group(2))
        host_m = SOURCE_FIELD_RE.search(m.group("fields"))
        out[num] = {
            "name": m.group(1),
            "host": host_m.group(1).strip() if host_m else "",
        }
    return out


def load_web_facts() -> dict[str, dict]:
    """读 `_web_text_facts.json` → `{fact_id: {host, claim, url}}`。

    ⚠️ 文件不存在 = 这份 deck 没用网文（**不是错误** —— 绝大多数 deck 都没有）→ 返回空 dict。
    """
    if not FACTS_PATH.is_file():
        return {}
    raw_text = FACTS_PATH.read_text(encoding="utf-8")
    try:
        data = json.loads(raw_text)
    except Exception as e:
        # 🔑 带行号定位：手写 JSON 最常见的是 claim 里裸引号未转义（2026-10-09 实测）
        line_no = getattr(e, "lineno", None)
        col_no = getattr(e, "colno", None)
        detail = f"第 {line_no} 行第 {col_no} 列" if line_no else "位置未知"
        print(f"⚠️ {FACTS_PATH.name} 解析失败（{type(e).__name__}: {getattr(e, 'msg', e)}）@ {detail} —— 按「无网文」处理")
        if line_no:
            src = raw_text.splitlines()
            if 0 < line_no <= len(src):
                bad_line = src[line_no - 1]
                print(f"   该行内容：{bad_line.strip()}")
                # 按列号判定是否为「字符串内裸引号」（比数引号个数可靠）
                if '"' in bad_line and col_no:
                    before = bad_line[: col_no - 1]
                    if before.count('"') % 2 == 0:
                        print('   💡 疑似字符串内双引号未转义 —— 须写成 \\"（如 "claim": "俗称\\"安全带\\"的装置"）')
        print("   ❗ 因此本页的网文来源标注会缺失，修好 JSON 后须重跑本脚本。")
        return {}
    out: dict[str, dict] = {}
    for item in data.get("facts") or []:
        if not isinstance(item, dict):
            continue
        fid = str(item.get("fact_id") or "").strip()
        if not fid:
            continue
        url = str(item.get("source_url") or "").strip()
        out[fid] = {
            "host": urlparse(url).netloc.lower() if url else "",
            "claim": str(item.get("claim") or ""),
            "url": url,
        }
    return out


def declared_facts(text: str) -> list[str]:
    """返回片段里 `<!--FACTS: …-->` 声明的 fact_id（保序去重）。

    🔑 这是「这页用了哪几条外部事实」的**唯一凭据** —— 文字没有 `image_NNN`
    那种天然编号，只能靠模型在正文里显式声明（契约 §1.6）。
    """
    out: list[str] = []
    for m in FACTS_RE.finditer(text):
        for fid in FACT_ID_RE.findall(m.group("ids")):
            if fid not in out:
                out.append(fid)
    return out


def referenced_ids(text: str) -> set[int]:
    """返回片段中实际引用的图片编号集合。"""
    return {int(n) for _, n in HREF_RE.findall(text)}


def strip_credit(text: str) -> str:
    """移除**两个**已有标注块（幂等基础）。

    ⚠️ 只删标注块；`<!--FACTS: …-->` 锚点**不动** —— 它是内容凭据，不属于标注。
    """
    for b, e in ((IMG_BEGIN, IMG_END), (TEXT_BEGIN, TEXT_END)):
        text = re.sub(
            rf"\n*{re.escape(b)}.*?{re.escape(e)}\n*", "\n", text, flags=re.DOTALL
        )
    return text


def _display_host(host: str) -> str:
    """把 host 转成**人类可读**形式：Punycode 标签解码、去掉 `www.`、过长则丢弃。

    ⚠️ 只影响**印在页面上的那行文字**；`_web_images.json` 里保存的仍是原始 host（审计真值）。
    百度返回的 `replaceUrl` 偶有 Punycode 中文域名（如 `www.xn--blqqdv68hqdiyvmnobx96a.com`），
    直接印上去是乱码，观众读不出是什么站。
    """
    host = (host or "").strip()
    if not host:
        return ""
    labels = []
    for label in host.split("."):
        if label.startswith("xn--"):
            try:
                label = label.encode("ascii").decode("idna")
            except Exception:
                return ""          # 解不出 → 不显示，避免页面出现乱码域名
        labels.append(label)
    disp = ".".join(labels)
    if disp.startswith("www."):
        disp = disp[4:]
    return disp if len(disp) <= 30 else ""


# 来源后缀最多列出几个站点，超出的以「等」收尾（契约 §1.6）
_MAX_LISTED_HOSTS = 3


def _hosts_tail(hosts: list[str]) -> str:
    """来源后缀：≤3 个全列站点、>3 个列前 3 个 + 「等」（契约 §1.6）。

    🔑 阈值 3 是实测定的：一行「标签 + 3 个域名」约 640px < 画布 1200px ⇒ 放得下。
    旧阈值 2 的问题在于**两类标注的来源数分布不同** —— 图片行 = 该页放的图张数
    （一页 2~3 张），文字行 = 该页声明的事实条数（一页 1~2 条）⇒ 图片标注几乎
    永远退化成「（网络）」，等于没标（2026-10-08 实测：5 个内容页里 4 页如此）。
    """
    uniq = sorted({d for d in (_display_host(h) for h in hosts) if d})
    if not uniq:
        return ""
    if len(uniq) <= _MAX_LISTED_HOSTS:
        return f"（{'、'.join(uniq)}）"
    return f"（{'、'.join(uniq[:_MAX_LISTED_HOSTS])} 等）"


def build_block(begin: str, end: str, role: str, y: int,
                label: str, hosts: list[str]) -> str:
    """按契约 §1.6 生成**一个**标注块（图片行或文字行）。"""
    return (
        f"\n{begin}\n"
        f'<text x="{CREDIT_X}" y="{y}" font-size="{CREDIT_FONT_SIZE}" '
        f'fill="{CREDIT_FILL}" data-role="{role}">{label}{_hosts_tail(hosts)}</text>\n'
        f"{end}\n"
    )


def build_credits(img_hosts: list[str], text_hosts: list[str]) -> str:
    """按契约 §1.6 的「位置与上下顺序」生成标注块。

    只有一类 → 该行 `y=690`；
    两类都有 → 图片 `y=668`（上）／ 文字 `y=690`（下，永远占最底）。
    """
    if img_hosts and text_hosts:
        return (
            build_block(IMG_BEGIN, IMG_END, IMG_ROLE,
                        CREDIT_Y_IMG_BOTH, IMG_LABEL, img_hosts)
            + build_block(TEXT_BEGIN, TEXT_END, TEXT_ROLE,
                          CREDIT_Y_TEXT_BOTH, TEXT_LABEL, text_hosts)
        )
    if img_hosts:
        return build_block(IMG_BEGIN, IMG_END, IMG_ROLE,
                           CREDIT_Y_SINGLE, IMG_LABEL, img_hosts)
    if text_hosts:
        return build_block(TEXT_BEGIN, TEXT_END, TEXT_ROLE,
                           CREDIT_Y_SINGLE, TEXT_LABEL, text_hosts)
    return ""


def _insert_credit(text: str, block: str) -> str:
    """把标注块插到**根 `<g>` 内部**（最后一个 `</g>` 之前）。

    🔴 必须插在 `<g>` 内，不能追加到文件末尾 —— 标注**不带 `font-family` 声明**
    （契约 §3 第 8 条禁止子孙重复声明字体，§1.6 说"字体由根 `<g>` 覆盖"），
    一旦落到 `<g>` 外面就继承不到字体，导出后会被引擎填成默认的西文字体
    （实测旧实现产出了 `latin=Segoe UI / ea=Microsoft YaHei` 微软雅黑，违反 §1.1）。
    """
    idx = text.rfind("</g>")
    if idx == -1:
        # 异常片段（没有 </g>）→ 退回追加到末尾，至少不丢内容
        return text.rstrip("\n") + "\n" + block
    return text[:idx].rstrip("\n") + "\n" + block + text[idx:]


def main() -> int:
    ap = argparse.ArgumentParser(description="来源标注自动注入：图片行 + 文字行（Step 2f）")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写文件")
    ap.add_argument("--page", help="只处理指定片段（如 page04.svg）")
    args = ap.parse_args()

    if not BODY_DIR.is_dir():
        print(f"❌ 正文片段目录不存在: {BODY_DIR}")
        print("   提示：Step 2 尚未写正文片段")
        return 1

    web = load_web_images()
    facts = load_web_facts()
    bodies = sorted(BODY_DIR.glob("*.svg"))
    if args.page:
        bodies = [b for b in bodies if b.name == args.page]
        if not bodies:
            print(f"❌ 未找到片段: {args.page}")
            return 1

    print("=== 来源标注注入（Step 2f：图片来源行 + 内容来源行）===")
    print(f"文本.md 网图: {len(web)} 张" + ("" if web else " —— 无图片行"))
    for n in sorted(web):
        print(f"    {web[n]['name']}  ← {web[n]['host'] or '(未知站点)'}")
    print(f"网文事实: {len(facts)} 条" + ("" if facts else "（无 _web_text_facts.json 或为空）"))
    for fid in sorted(facts):
        print(f"    {fid}  ← {facts[fid]['host'] or '(未知站点)'}")
    print()

    injected = removed = 0
    unknown_all: list[tuple[str, str]] = []
    for b in bodies:
        raw = b.read_text(encoding="utf-8")
        clean = strip_credit(raw)

        img_hit = sorted(referenced_ids(clean) & set(web))
        declared = declared_facts(clean)
        unknown = [f for f in declared if f not in facts]
        fact_hit = [f for f in declared if f in facts]
        if unknown:
            unknown_all.append((b.name, "、".join(unknown)))

        block = build_credits(
            [web[n]["host"] for n in img_hit],
            [facts[f]["host"] for f in fact_hit],
        )
        new = _insert_credit(clean, block) if block else clean

        if block:
            print(f"  ✅ {b.name}")
            if img_hit:
                print(f"       图片行 ← 引用网图: {'、'.join(web[n]['name'] for n in img_hit)}")
            if fact_hit:
                print(f"       文字行 ← 事实锚点: {'、'.join(fact_hit)}")
            if unknown:
                print(f"       ⚠️ 锚点里有 facts.json 查不到的 id: {'、'.join(unknown)}")
            injected += 1
        else:
            if raw != clean:
                print(f"  🧹 {b.name}  无外部引用 → 已清除旧标注")
                removed += 1
            elif unknown:
                print(f"  ⚠️ {b.name}  锚点 id 全部查不到，未注入")
            else:
                print(f"  ·  {b.name}  无外部引用，跳过")

        if not args.dry_run and new != raw:
            b.write_text(new, encoding="utf-8")

    print()
    if args.dry_run:
        print(f"[dry-run] 将注入 {injected} 个、清除 {removed} 个（未落盘）")
    else:
        print(f"✅ 注入 {injected} 个、清除 {removed} 个")
    if injected == 0 and (web or facts):
        print("⚠️ 有网图 / 网文但没有任何页引用 —— 全部闲置，PPT 里不会出现对应标注")
    if unknown_all:
        print()
        print("⚠️ 以下片段声明了 facts.json 里不存在的事实 id（契约 §1.6：不能漏、不能编）：")
        for name, ids in unknown_all:
            print(f"    {name}: {ids}")
        print("   → 请修正锚点或重跑 §0d；`check_web_research.py` 会把这条判为硬错误")
    return 0


if __name__ == "__main__":
    sys.exit(main())
