#!/usr/bin/env python3
"""Generate `assets/sources/_web_text_facts.json` from a plain-text fact list.

Why this exists
---------------
The facts file is the provenance record for every web-sourced claim
(the contract §1.6 red line). It used to be written by hand, and that kept
failing: on 2026-10-09 and again on 2026-10-10 the model wrote raw JSON and
broke it on Chinese quotes inside Python strings, burning minutes on a
pointless debug loop. Telling the model "use json.dump, do not hand-write
JSON" did not stop it - the instruction was read, acknowledged, and skipped.

So the writing step is taken away entirely. Input is a plain-text list, not
code: no quotes to escape, no braces to balance, no Python syntax to get
right. This script owns the JSON.

Input format (one fact per line, fields separated by `` | ``)::

    F001 | <claim> | <source_title> | <source_url> | <retrieved_at>
    F002 | <claim> | <source_title> | <source_url> | <retrieved_at>

Optional relation groups (``#GROUP`` directive lines, 2026-10-14)
-----------------------------------------------------------------
A flat list of facts carries **no relation between the facts**, and the
contract's component routing (②冊 §4.8「R1~R6 路由判据」/ ③冊 §5.0「骨架选择
速查」) is driven entirely by *what logical relation the content has*
(構成 / 對照 / 遞進 / 時序 …). A flat list can therefore only ever fall back
to the generic components (C1 / C9 / 圖文左右) — measured 2026-10-12: R1~R6
had **zero** references in real decks.

The relation has to be recorded **while the model is still reading the raw
sources** — that is the only moment the context exists. §0d E deletes the
whole ``_research/`` directory (crawled articles included) immediately after
this step, so by Step 1 the model only holds the flat claims and can no
longer infer any relation at all.

So a ``#GROUP`` line declares "the facts that follow belong to one logical
unit"::

    #GROUP G1 | 構成 | 中國 LNG 接收站儲存規模增長
    F001 | 2020 年液態儲存規模 1,070 萬 m³ | … | …
    F002 | 2025 年達 2,868 萬 m³，增長 168% | … | …

* Field 2 must be one of :data:`RELATIONS` (the six values are exactly the
  relation words the contract's routing tables already use).
* Field 3 (the group title) is optional.
* Facts written **before** the first ``#GROUP``, or in a file that has no
  ``#GROUP`` line at all, are **ungrouped** — the old flat rendering is kept
  verbatim, so existing fact lists behave exactly as before.

Rules
-----
* One fact per line. Blank lines and lines starting with ``#`` are ignored
  — **except ``#GROUP``**, which is a directive.
* ``claim`` may contain any punctuation, including ``"`` and ``「」`` - the
  field separator is `` | `` (space-pipe-space), so a bare pipe inside a
  claim is fine as long as it is not surrounded by spaces.
* ``retrieved_at`` is optional; omit it (leave the 5th field empty) and the
  script uses today's date. ``source_title`` may also be empty.

Usage::

    python scripts/emit_facts.py --topic "茶马古道" --mode full assets/sources/_research/facts.txt
    python scripts/emit_facts.py --topic "茶马古道" --mode full < assets/sources/_research/facts.txt
    python scripts/emit_facts.py --topic "…" --check-only

Where the fact list lives: ``assets/sources/_research/facts.txt``. That
directory is wiped wholesale by §0d E, so a scratch list placed there is
cleaned up for free. Passing a bare ``facts.txt`` (resolved against the
current working directory) is the failure seen in the field on 2026-10-13 -
the file had been written into ``assets/sources/`` and the script could not
find it. Note ``--check-only`` takes no positional argument: it re-reads
``--output`` (the JSON), not the fact list.

``--sync-text`` (opt-in)
------------------------
Also renders the same facts into the web-sourced section of
``assets/sources/文本.md``, so the model writes only the fact list and both
downstream artifacts come from **one** source — the "一一对应" between
``_web_text_facts.json`` and ``文本.md`` stops being a promise and becomes a
construction fact.

The file is **never rewritten wholesale**: the section is replaced if already
present (idempotent), inserted before the image-registration sections
otherwise, and created when the file does not exist. Local material and the
image-registration sections (``## 附加图片`` / ``## 联网补充图片``) are left
untouched — ``verify_extraction.py`` counts the placeholders in the latter.
See ``references/step_0_extract.md`` §0d D.

The ``--check-only`` form validates an existing facts file and prints the
same diagnostics as ``check_web_research.py`` but without writing anything.

``--local`` (本地来源模式, §0c 统一结构化)
-----------------------------------------
把**本地素材**当抓取件跑**同一套流水线** —— 这是「两条取材路出口形态一致」的
落点（2026-10-14 用户拍板：本地素材也要提纯、图要说明含义）：

* 读**同一格式**的清单（`L001 | <要点> | <块名> | <素材名>` ＋ `#GROUP` 行）；
* **不写** ``_web_text_facts.json`` —— 本地素材是调用方给的「既定内容」，
  **不对外部来源负责**，故无溯源产物（用户明确：本地不做溯源）；
* `source_url` **不强制**（留空即可），其余校验（`#GROUP` 合法性、关系覆盖、
  事实密度）**完全复用**；
* ``--sync-text`` 把渲染结果写进 ``文本.md`` 的**本地段**（标记
  ``__来源本地 · ``、组标题 ``### [L1] …``），替换掉 §0b 解压出的原文正文区。

⚠️ 调用前必须先「移图」（把正文里的 `[[image_NNN]]` 全部挪进 `## 附加图片`
登记段、并给每张补「内容：」）—— 否则占位符会随旧正文一起被替换掉。
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from console_encoding import configure_utf8_stdio  # noqa: E402

SCHEMA = "tcc-ppt-skill-webtext-facts/v1"
DEFAULT_OUTPUT = "assets/sources/_web_text_facts.json"
DEFAULT_TEXT_MD = "assets/sources/文本.md"
FIELD_SEP = " | "
MIN_FIELDS = 2          # fact_id | claim  (其余可缺)
MAX_FIELDS = 5          # fact_id | claim | source_title | source_url | retrieved_at

# ── 逻辑关系分组（2026-10-14 新增）──
# 为什么要有它：facts 平铺落盘时**丢失了「事实之间的关系」**，而契约的组件 /
# 骨架选型（②册 §4.8 路由判据、③册 §5.0 骨架速查）**全部以逻辑关系为输入**。
# 关系只能在「模型刚读完原文」那一刻标注 —— §0d E 随即删掉整个 _research/
# （抓取原文在内），到 Step 1 时手上只剩平铺 claims，无从反推。
#
# 🔑 六个关系值**严格取自契约已有出口**，不发明新概念：
#    并列 → §5.2 多栏并列 / §5.6 卡片矩阵
#    构成 → R1 进度条 / §5.9 不等宽主次分区
#    对比 → R3 VS 对比卡 / §5.18 双图对开
#    递进 → R6 递增柱 / L2 阶梯轴 / §5.15 阶梯
#    时序 → §5.3 流程链 / L1 中轴交替时间轴 / L3 阶段色带
#    分层 → §5.10 编号条目列 / §5.19 环形放射
# ⚠️ 契约里**没有**「因果 / 问题-对策」类骨架出口 ⇒ 不列入枚举（列了也查不到组件）。
GROUP_PREFIX = "#GROUP"
GROUP_MIN_FIELDS = 2    # group_id | relation  (组标题可缺)
RELATIONS: dict[str, str] = {
    "并列": "§5.2 多栏并列 / §5.6 卡片矩阵",
    "构成": "R1 进度条 / §5.9 不等宽主次分区",
    "对比": "R3 VS 对比卡 / §5.18 双图对开",
    "递进": "R6 递增柱 / L2 阶梯轴 / §5.15 阶梯",
    "时序": "§5.3 流程链 / L1 中轴交替 / L3 阶段色带",
    "分层": "§5.10 编号条目列 / §5.19 环形放射",
}
# 文本.md 里的组标题行 —— 渲染成 `### [G1] 构成 · <组标题>`：
# ① 模型在 Step 1 读 md，md 小标题最易扫到；② `verify_extraction.body_only()`
#    按 `^\s*#{2,4}\s*\[G\d+\]` 把这个标题**整行剥掉** ⇒ 关系元信息不进正文体量，
#    体量 / 内容量判据不会被虚高污染（该正则与这里的渲染格式同源，改一处要改两处）。
GROUP_HEAD_TEMPLATE = "### [{gid}] {relation}"

# ``--sync-text`` artifacts: 文本.md carries the same claims as the JSON,
# minus the URLs. Both views are rendered from one fact list, so "一一对应"
# stops being a promise the model has to keep and becomes a construction fact.
WEB_SECTION_MARK = "__来源网络 · "
# 本地素材段（§0c 统一结构化）—— 与网络段**同形**，只换「标记」：
#   标记 `__来源本地 · ` ＋ 组标题行 `### [L1] 关系 · <块名>`。
#   `[L…]` 与网络段的 `[G…]` 语法同形 ⇒ 下游一套正则、一套心智模型。
LOCAL_SECTION_MARK = "__来源本地 · "
_IMAGE_SECTION_RE = re.compile(r"(?m)^##\s*(?:附加图片|联网补充图片)")


def parse_facts(text: str, *, require_url: bool = True,
                default_prefix: str = "F"
                ) -> tuple[list[dict], list[dict], list[str]]:
    """Parse the plain-text fact list into dicts. Returns (facts, groups, errors).

    ``groups`` 按文档顺序列出 ``#GROUP`` 关系块。清单里没有 ``#GROUP`` 行
    ⇒ ``groups == []``、且每条 fact 不带 ``group_id`` —— 即旧的平铺行为，
    渲染与体量判据都逐字节不变（向后兼容）。

    ``require_url`` —— **本地来源模式传 False**：本地素材是调用方自己给的
    「既定内容」，**不对外部来源负责**（用户 2026-10-14 拍板：本地文本不做溯源），
    所以不强制每条带 URL。网络模式仍强制（契约 §1.6 红线）。

    ``default_prefix`` —— 行首**没有编号**时补的前缀：网络段 `F`、本地段 `L`。
    🔴 行首**已有单字母编号**时一律保留原前缀（`L001` ⇒ 仍是 `L001`）——
    早期版本只认 `F`，把 `L001` 当成「漏编号」整行右移，claim 变成 `L001`，
    rc 还是 0，**静默产出垃圾**（实测「平均 4 字符/条」）。
    """
    facts: list[dict] = []
    groups: list[dict] = []
    seen_gid: set[str] = set()
    errors: list[str] = []
    current: dict | None = None
    today = datetime.date.today().isoformat()

    for lineno, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue

        # ── #GROUP 指令行：声明「接下来的 fact 同属一个逻辑关系块」──
        if line.startswith(GROUP_PREFIX):
            raw = line[len(GROUP_PREFIX):].strip()
            # 容忍 `#GROUP G1 | 并列 |`（留着字段位、标题留空）这种很自然的写法 ——
            # 末尾的 ` |` 后面没有空格，不算分隔符，会把 `并列 |` 整个当成一个字段。
            if raw.endswith("|"):
                raw = raw[:-1].strip()
            parts = [p.strip() for p in raw.split(FIELD_SEP)]
            if len(parts) < GROUP_MIN_FIELDS:
                errors.append(
                    f"第 {lineno} 行：`#GROUP` 字段不足（需要 "
                    f"`#GROUP <组id>{FIELD_SEP}<关系>`，组标题可省），"
                    f"实得 {len(parts)} 段"
                )
                current = None
                continue
            gid, relation = parts[0], parts[1]
            gtitle = FIELD_SEP.join(parts[2:]).strip() if len(parts) > 2 else ""
            if not gid:
                errors.append(f"第 {lineno} 行：`#GROUP` 的组 id 为空")
                current = None
                continue
            if gid in seen_gid:
                errors.append(f"第 {lineno} 行：组 id 重复：{gid}")
                current = None
                continue
            if not relation:
                errors.append(
                    f"第 {lineno} 行（{gid}）：关系为空 —— 必须填六者之一："
                    f"{'、'.join(RELATIONS)}"
                )
                current = None
                continue
            if relation not in RELATIONS:
                errors.append(
                    f"第 {lineno} 行（{gid}）：关系 `{relation}` 不在枚举内 —— "
                    f"只能取 {'、'.join(RELATIONS)}。这几个值不是随便定的："
                    f"它们就是契约判据表里真实存在的出口，自造的值下游查不到组件"
                )
                current = None
                continue
            current = {
                "group_id": gid,
                "relation": relation,
                "title": gtitle,
                "fact_ids": [],
            }
            groups.append(current)
            seen_gid.add(gid)
            continue

        if line.startswith("#"):
            continue

        parts = [p.strip() for p in line.split(FIELD_SEP)]

        # 🔴 2026-10-18 容错：**行首漏写 `F` 编号时自动补**
        #   实测（本地自测）：写成「事实 | 标题 | URL | 日期」⇒ 五列整体左移一格，
        #   `source_url` 里装的是日期、`retrieved_at` 为空 ⇒ 输出「带 URL = 0/23」，
        #   而事实一条没丢、脚本也不报错 —— **静默失败**，比报错难查得多。
        #   处理：行首是 `F001` / `001` 就当编号（纯数字补 F 前缀）；否则整行右移一格。
        if parts:
            # 🔴 2026-10-18：原来只认 `F` ⇒ §0c 文档规定的 `L001` 匹配不上，
            #   被当成「漏编号」整行右移，claim = 'L001'（4 字符）、真实要点掉进
            #   source_url 槽位，rc 仍为 0 —— 与「漏编号」同一类静默失败。
            #   现在：接受任意单字母前缀并**原样保留**（L001 还是 L001，下游
            #   verify_extraction 认的就是 `### [Ln]` 本地段标注）。
            _m = re.match(r'^([A-Za-z])?(\d{2,4})$', parts[0])
            if _m:
                _pre = (_m.group(1) or default_prefix).upper()
                parts[0] = _pre + _m.group(2)
            else:
                parts = ['%s%03d' % (default_prefix, len(facts) + 1)] + parts

        if len(parts) < MIN_FIELDS:
            errors.append(
                f"第 {lineno} 行：字段不足（至少需要 "
                f"`fact_id{FIELD_SEP}claim`），实得 {len(parts)} 段"
            )
            continue
        if len(parts) > MAX_FIELDS:
            # 常见原因：claim 里用了 " | " 当标点 —— 换成「」或把空格去掉即可
            errors.append(
                f"第 {lineno} 行：字段过多（最多 {MAX_FIELDS} 段，实得 {len(parts)} 段）。"
                f"⚠️ 最常见原因是 claim 里出现了「空格-竖线-空格」——"
                f"请把 claim 里的 ` | ` 换成「」或直接去掉空格"
            )
            continue

        parts += [""] * (MAX_FIELDS - len(parts))
        fid, claim, title, url, retrieved = parts

        if not fid:
            errors.append(f"第 {lineno} 行：fact_id 为空")
            continue
        if not claim:
            errors.append(f"第 {lineno} 行（{fid}）：claim 为空")
            continue
        if not url and require_url:
            errors.append(
                f"第 {lineno} 行（{fid}）：source_url 为空 —— 溯源红线（契约 §1.6）"
            )
            continue

        fact = {
            "fact_id": fid,
            "claim": claim,
            "source_title": title,
            "source_url": url,
            "retrieved_at": retrieved or today,
        }
        if current is not None:
            # 组内成员 —— 前五个字段的顺序一个没动，group_id 追加在末尾，
            # 免得按位置读字段的下游被这一层改动波及。
            fact["group_id"] = current["group_id"]
            current["fact_ids"].append(fid)
        facts.append(fact)

    # 空 #GROUP（声明了却没有任何 fact）几乎总是写漏了 —— 报出来，别静默吞掉
    for g in groups:
        if not g["fact_ids"]:
            errors.append(
                f"组 {g['group_id']} 是空的（`#GROUP` 声明后没有任何 fact）"
            )

    if not facts and not errors:
        errors.append("没有解析到任何 fact —— 输入是空的？")
    return facts, groups, errors


def build_payload(topic: str, mode: str, facts: list[dict],
                  groups: list[dict] | None = None) -> dict:
    payload = {
        "schema": SCHEMA,
        "topic": topic,
        "mode": mode,
        "generated_at": datetime.datetime.now().replace(microsecond=0).isoformat(),
        "facts": facts,
    }
    # 关系块只在真有时才写 —— 老清单（无 #GROUP）产出的 JSON 与改动前一致。
    if groups:
        payload["groups"] = groups
    return payload


def build_web_section(topic: str, facts: list[dict],
                      groups: list[dict] | None = None,
                      *, mark: str = WEB_SECTION_MARK) -> str:
    """Render one sourced section of 文本.md from the same fact list.

    带 ``#GROUP`` 的清单渲染成「关系小标题 + 组内条目」—— 这正是 Step 1 选骨架
    时要看的输入（契约 ③册 §5.0 的「手上的内容」一列）。未分组清单仍是平铺的
    ``- claim``，与改动前逐字节一致。

    ``mark`` 区分两条取材路：网络段 ``__来源网络 · ``（`[G…]`）/ 本地段
    ``__来源本地 · ``（`[L…]`）。**除标记外渲染完全同形** —— 这正是把本地素材
    「当抓取件处理」的落点：两条路产出的段，Step 1 用同一套规则读。
    """
    meta = {g["group_id"]: g for g in (groups or [])}
    # ⚠️ 第 4 个元素那个空串是**有意义的**：它与前一项拼出标题后的空行。
    # 去掉它就会在无组清单上少一个空行 ⇒ 与改动前的渲染不再逐字节一致。
    lines = ["---", "", f"{mark}{topic or '（未命名主题）'}__", ""]
    seen_gid: str | None = None
    for f in facts:
        gid = f.get("group_id")
        if gid != seen_gid and gid and gid in meta:
            g = meta[gid]
            head = GROUP_HEAD_TEMPLATE.format(gid=gid, relation=g["relation"])
            if g.get("title"):
                head += f" · {g['title']}"
            # 标题贴在上一组的最后一条后面时要空开一行；第一组前面已有空行，不重复补。
            if lines and lines[-1].strip():
                lines.append("")
            lines += [head, ""]
        seen_gid = gid
        lines.append(f"- {f['claim']}")
    lines += ["", "---"]
    return "\n".join(lines)


def sync_text_md(topic: str, facts: list[dict], path: Path,
                 groups: list[dict] | None = None) -> str:
    """Replace / insert the web-sourced section of 文本.md in place.

    Never rewrites the whole file: 文本.md may already hold converted local
    material (§0b) plus the image-registration sections, and
    ``verify_extraction.py`` counts the placeholders in the latter. Returns a
    short status word for printing.

    分组信息（关系小标题）只写进网络段内部；``body_only()`` 会把这些标题行剥掉，
    所以正文体量 / 内容量判据不会被关系元信息抬高。
    """
    section = build_web_section(topic, facts, groups)
    old = path.read_text(encoding="utf-8") if path.exists() else ""

    idx = old.find(WEB_SECTION_MARK)
    if idx != -1:
        line_start = old.rfind("\n", 0, idx) + 1
        prev = old.rfind("\n---", 0, line_start)
        start = prev if prev != -1 else line_start
        # ⚠️ 起点之前若**只有 `---` 与空行**（= 文件开头没有本地段），把起点推到 0。
        # 否则开场那条 `---` 留在替换区之外，与新段自带的 `---` 撞成两条 —— 只要
        # 网络段位于文件开头（零本地素材），重跑就不幂等
        #（实测 2026-10-14：首次 1 条 → 重跑后 2 条）。这个判据同时能**回收**已被
        # 旧行为写脏的 `---\n\n---`，所以历史脏文件再跑一次即自愈。
        if all((not ln.strip()) or ln.strip() == "---" for ln in old[:start].splitlines()):
            start = 0
        nxt = old.find("\n---", idx)
        end = nxt + len("\n---") if nxt != -1 else len(old)
        while end < len(old) and old[end] in "\r\n":
            end += 1
        parts = [
            p for p in (old[:start].rstrip("\n"), section, old[end:].lstrip("\n")) if p
        ]
        # ⚠️ `old[end:]` 只剥了**前导**换行（`lstrip`），尾换行仍在，而 `section`
        # 自身以 `---` 结尾、`head` 已被 rstrip。因此这里**不能**无条件 `+ "\n"`：
        # 当网络段后面还有内容（图片登记段）时，末尾会每次重跑多出一个空行
        #（实测 2026-10-14：len 101→102→103；纯网络段场景因无 tail 不受影响）。
        # 规范成「恰好一个尾换行」，与首次落盘（m 分支）逐字节对齐 ⇒ 重跑幂等。
        new, action = "\n\n".join(parts).rstrip("\n") + "\n", "替换"
    else:
        m = _IMAGE_SECTION_RE.search(old)
        if m:
            head, tail = old[: m.start()].rstrip("\n"), old[m.start():]
            new = (head + "\n\n" if head else "") + section + "\n\n" + tail
            action = "插入（图片登记段之前）"
        else:
            head = old.rstrip("\n")
            new = (head + "\n\n" if head else "") + section + "\n"
            action = "追加" if head else "创建"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8")
    return action


def sync_local_section(topic: str, facts: list[dict], path: Path,
                       groups: list[dict] | None = None) -> str:
    """Replace the **local-material region** of 文本.md with the condensed section.

    与网络段不同：本地段的「原位」就是文件正文区本身（§0b 解压出的原文），
    它**没有自带标记**。所以本函数把「文件开头 → 图片登记段 / 网络段之前」这段
    正文整体视作本地正文区，用渲染好的本地段整段替换。

    ⚠️ **调用前必须先「移图」** —— 正文里若还留着 `[[image_NNN]]` 占位符，它们会随
    旧正文一起被替换掉 ⇒ 占位符数 ≠ 图片数，`verify_extraction` 当场报错
    （fail loud，不是静默丢图）。本地图的归属改由 `## 附加图片` 登记行的
    「内容：」字段表达。
    """
    section = build_web_section(topic, facts, groups, mark=LOCAL_SECTION_MARK)
    old = path.read_text(encoding="utf-8") if path.exists() else ""

    # 本地正文区的右边界 = 「图片登记段」或「网络段」的起点
    cut = len(old)
    m = _IMAGE_SECTION_RE.search(old)
    if m:
        cut = m.start()
    nw = old.find(WEB_SECTION_MARK)
    if nw != -1:
        prev = old.rfind("\n---", 0, nw)
        if prev != -1:
            cut = min(cut, prev + 1)

    tail = old[cut:].lstrip("\n")
    # 段尾留一个 `---` ＋ 空行 ⇒ 与后面的图片登记段之间仍是 `\n\n---\n\n`
    #（check_materials 靠它数来源段，少一个空行就会把两段并成一段）。
    new = section + ("\n\n" + tail if tail else "\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8")
    return "创建" if not old.strip() else "替换（本地正文区）"


def check_only(path: Path) -> int:
    """Re-read a written facts file and report the fields the skill relies on."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"[ERROR] 找不到 {path}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"[ERROR] {path} 不是合法 JSON：{exc}", file=sys.stderr)
        return 1

    facts = data.get("facts") or []
    groups = data.get("groups") or []
    ids = [f.get("fact_id", "") for f in facts]
    url_ok = sum(1 for f in facts if str(f.get("source_url", "")).startswith("http"))
    print(f"✅ {path} 合法")
    print(f"   schema   = {data.get('schema')}")
    print(f"   topic    = {data.get('topic')}")
    print(f"   mode     = {data.get('mode')}")
    print(f"   facts    = {len(facts)} 条")
    print(f"   带 URL   = {url_ok}/{len(facts)}")
    if groups:
        rels = "、".join(str(g.get("relation") or "?") for g in groups)
        print(f"   关系块   = {len(groups)} 组（{rels}）")
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        print(f"   [WARN] fact_id 重复：{sorted(dupes)}", file=sys.stderr)
        return 1
    if url_ok != len(facts):
        print("   [WARN] 有条目缺 source_url —— 溯源红线", file=sys.stderr)
        return 1
    # ── 关系块自洽（仅当有 groups）—— 防手工改 json 后两边漂移 ──
    id_set = set(ids)
    gid_set = {str(g.get("group_id") or "") for g in groups}
    for g in groups:
        for fid in (g.get("fact_ids") or []):
            if fid not in id_set:
                print(f"   [WARN] 组 {g.get('group_id')} 引用了不存在的 fact_id: {fid}",
                      file=sys.stderr)
                return 1
    for f in facts:
        gid = f.get("group_id")
        if gid and gid not in gid_set:
            print(f"   [WARN] {f.get('fact_id')} 的 group_id「{gid}」不在 groups 里",
                  file=sys.stderr)
            return 1
    return 0


# 目标正文口径 —— 与 verify_extraction.TEXT_PER_PAGE 同源（改一处必须改两处）
TEXT_PER_PAGE = 200
DEFAULT_PAGES = 15
DEFAULT_FACADE = 7

# ── 催补线（2026-10-14 新增）──
# 期望线 = 内容页 × TEXT_PER_PAGE，但**低于期望 ≠ 不够用**：
#   · TEXT_PER_PAGE=200 是「奥特曼」deck 245 字/页反推的，本身自带余量；
#   · 下游 check_creative_svg 的硬下限只有 140 字/页（= 期望的 70%）。
# ⇒ 达到期望的 **80%**（8 页 = 1,280 字符 = 160 字/页，仍高于硬下限 14%）即放行、不再催补。
# 实测 2026-10-14：抓取件净正文 54,472 字符，提炼后 1,385 字符（期望的 87%）——
# 模型为补这 215 字符把整份 facts.txt 重写了一遍，多花约 6 分钟，收益与耗时完全不成正比。
BODY_WARN_RATIO = 0.8

# ── 事实密度参考线（2026-10-14 新增）──
# 「压得太狠」的根因是**每条写得太短**，不是条数不够 ⇒ 把密度摆到落盘当场。
# 条数与每条长度的乘积恒等于期望正文：内容页 × 4 条 × 50 字符 = 内容页 × 200 字符。
FACTS_PER_PAGE = 4
MIN_CLAIM_CHARS = TEXT_PER_PAGE // FACTS_PER_PAGE      # = 50


def _body_chars(text_md: Path) -> int | None:
    """Net body characters of 文本.md, reusing verify_extraction's stripper.

    Reusing the function (rather than re-implementing the strip) keeps the two
    counts from drifting apart - same lesson as the 2026-10-13 shell-page ratio
    bug: "the constants matched, the input didn't".
    """
    try:
        here = Path(__file__).resolve().parent / "validators"
        if str(here) not in sys.path:
            sys.path.insert(0, str(here))
        from verify_extraction import body_only
    except Exception:  # noqa: BLE001 - not importable ⇒ skip the check, don't fail
        return None
    try:
        return len(body_only(text_md.read_text(encoding="utf-8")).strip())
    except OSError:
        return None


def _expansion_runs_file() -> Path:
    """Ledger for "how many times has the body come up short".

    Cross-process on purpose: every tool call is a fresh process, so an
    in-memory counter would reset to zero and the cap would never bite.
    """
    override = os.environ.get("TCC_RUNTIME_DIR")
    base = Path(override) if override else Path(__file__).resolve().parents[1] / ".runtime"
    return base / "facts_expansions.json"


def _bump_expansion_runs() -> int:
    """Return which short-body report this is (1-based).

    Only *short* runs are counted - a run that meets the target never touches
    the ledger, so "run 2" always means "the expansion did not help".
    """
    path = _expansion_runs_file()
    runs = 0
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            runs = int(loaded.get("runs", 0) or 0)
    except (OSError, ValueError):
        runs = 0
    runs += 1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"runs": runs}), encoding="utf-8")
    except OSError:
        pass
    return runs


def report_fact_density(facts: list[dict], pages: int, facade: int) -> None:
    """Print 条数 + 平均每条字符数 —— 让「压得太狠」在落盘当场就看得见。

    为什么要有这一行：体量不足的根因不是「条数不够」，而是**每条事实写得太短**
    （实测 2026-10-14：30 条 / 平均 46 字符 ⇒ 合计 1,385 字符，只有期望的 87%）。
    等 WARN 报出来时模型已经收工，只能整份重写 facts.txt（多花约 6 分钟）——
    把密度摆在落盘那一刻，第一次就能写足，省掉整个返工回环。
    """
    if not facts:
        return
    total = sum(len(str(f.get("claim", "")).strip()) for f in facts)
    avg = total / len(facts)
    print(f"   → 事实密度：{len(facts)} 条 · 平均 {avg:.0f} 字符/条"
          f"（claim 合计 {total:,} 字符）")
    content_pages = max(0, pages - facade)
    if not content_pages:
        return
    floor = int(content_pages * TEXT_PER_PAGE * BODY_WARN_RATIO)
    if total >= floor:
        return          # 已达放行线 ⇒ 只报密度、不催补（免得为最后 20% 再返工一轮）
    print(f"     参考：目标 {content_pages * FACTS_PER_PAGE} 条（内容页 × {FACTS_PER_PAGE}）、"
          f"每条 ≥ {MIN_CLAIM_CHARS} 字符 ⇒ 合计 ≥ {content_pages * TEXT_PER_PAGE:,} 字符"
          f"（放行线 {floor:,}）")


def report_group_coverage(facts: list[dict], groups: list[dict]) -> None:
    """落盘当场报一行「关系覆盖」—— 有没有提取逻辑关系，一眼可见。

    为什么必须当场报（2026-10-14 新增）：契约的**组件与骨架选型全部以「内容的
    逻辑关系」为输入**（②册 §4.8「R1~R6 路由判据」、③册 §5.0「骨架选择速查」的
    「手上的内容」一列）。平铺的 fact 列表里没有关系 ⇒ 下游只能退回最弱的默认
    「并列」，于是 **R1 / R2 / R3 / R6 / L1~L3 这些按关系触发的组件系统性用不上**
    （2026-10-12 实测：R1~R6 在真实产物里零引用）。

    而关系**只在这个时刻标得出来** —— §0d E 紧接着就把 `_research/`（含全部抓取
    原文）整目录删掉，到 Step 1 时模型手上只剩平铺 claims，再想反推也没有原料。
    所以这里和「事实密度」一样，把结论摆到落盘那一刻，别等交付后才发现骨架雷同。
    """
    if not groups:
        print("   → 关系覆盖：0 组 ⚠️ 一条逻辑关系都没标。")
        print("      契约的组件/骨架选型**全部按逻辑关系触发**（②册 §4.8 / ③册 §5.0）——")
        print("      平铺清单只能退回默认的「并列」，R1/R2/R3/R6/L1~L3 都用不上。")
        print("      给 facts.txt 加 `#GROUP` 行补标（关系枚举见 step_0_extract.md §0d D），")
        print("      ⚠️ **必须在 §0d E 删除 `_research/` 之前**，之后原文就没了。")
        return
    counts: dict[str, int] = {}
    for g in groups:
        counts[str(g.get("relation") or "?")] = counts.get(str(g.get("relation") or "?"), 0) + 1
    covered = sum(1 for f in facts if f.get("group_id"))
    detail = " / ".join(f"{k}×{v}" for k, v in counts.items())
    print(f"   → 关系覆盖：{len(groups)} 组（{detail}），覆盖 {covered}/{len(facts)} 条")
    if covered < len(facts):
        print(f"      还有 {len(facts) - covered} 条未归组 —— 散点事实不会被 Step 1 当成结构性输入，"
              f"能归就归。")


def warn_if_body_short(text_md: Path, pages: int, facade: int) -> None:
    """Warn when the extracted body cannot fill the deck.

    Why here and not at §0e: §0d E wipes ``_research/`` - facts.txt included -
    immediately after this step. By the time §0e reports "66% of target", the
    fact list is already gone and the only remaining fix is to re-crawl from
    scratch (2026-10-13 实测: 净正文 21,861 字符的素材只提炼出 1,062 字符正文,
    达成率 66%，而模型到 §0e 才发现、那时已无法补)。Catching it here means a
    few facts can still be appended and the command rerun while everything is
    on disk.

    **One expansion only.** Appending facts is worth exactly one shot; past
    that the model tends to keep topping up the list without ever reaching the
    target (2026-10-13 实测: 补了两次仍是 68%，二十多轮还在补). So the first
    short report invites a single top-up and the second tells it to stop and
    move on - a short body is not a failure, the deck just carries less text.
    """
    content_pages = max(0, pages - facade)
    need = content_pages * TEXT_PER_PAGE
    if need <= 0:
        return
    have = _body_chars(text_md)
    floor = int(need * BODY_WARN_RATIO)
    if have is None or have >= floor:
        return

    runs = _bump_expansion_runs()
    head = (
        f"\n[WARN] 正文体量偏少：现有 {have:,} 字符 / 期望 {need:,} 字符"
        f"（{have / need:.0%} < 放行线 {BODY_WARN_RATIO:.0%} = {floor:,} 字符）。\n"
        f"       口径：{pages} 总页 − {facade} 门面 = {content_pages} 内容页 × "
        f"{TEXT_PER_PAGE} 字符/页，再乘放行比例 {BODY_WARN_RATIO:.0%}。\n"
        "       原因几乎总是**提炼压得太狠** —— 抓取件的净正文通常是这个数的十倍以上，"
        "把话写全就能补上。"
    )
    if runs <= 1:
        tail = (
            "\n       ⇒ **补一次**：把 facts.txt 里**过短的几条写详细**"
            "（补上具体数字 / 条件 / 适用范围）—— **不是重写整份文件、也不是只加条数**，"
            "改完重跑本命令。\n"
            "       ⚠️ **只有这一次机会** —— 重跑后**无论体量如何都直接往下走**，"
            "不许再补、不许再重跑。\n"
            "       （真正拖到 §0e 才发现就晚了：那时 `_research/` 已被 §0d E 清掉。）"
        )
    else:
        tail = (
            f"\n       ⇒ **扩容机会已用完（这是第 {runs} 次报不足）—— 就此收手。**\n"
            "       **不要再改 facts.txt、不要再重跑本命令。** 直接进 E 段（清理）→ §0e，"
            "由 §0e 用真实体量做最终判定。\n"
            "       **体量偏少不阻断流程** —— 内容页少写一点即可，别在这一步继续耗时间。"
        )
    print(head + tail, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(
        description="Generate _web_text_facts.json from a plain-text fact list.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
输入格式（每行一条，字段用 ` | ` 分隔）：
  F001 | <claim> | <来源标题> | <URL> | <抓取日期>
  ...
  · claim 里可以随便用引号（半角 " 或「」都行）—— 不用转义。
  · 抓取日期可省（留空即用今天）。
  · 空行和 # 开头的行会被忽略（`#GROUP` 除外，它是指令）。

关系分组（可选，但强烈建议）——`#GROUP <组id> | <关系> | <组标题>`：
  #GROUP G1 | 构成 | 中国 LNG 接收站储存规模增长
  F001 | 2020 年液态储存规模 1,070 万 m³ | … | …
  F002 | 2025 年达 2,868 万 m³，增长 168% | … | …
  · <关系> 只能取：并列 / 构成 / 对比 / 递进 / 时序 / 分层
    （这六个是契约判据表里真实存在的出口，别自造）
  · 作用：契约的组件 / 骨架选型全部按「内容是什么逻辑关系」触发
    （②册 §4.8 / ③册 §5.0）—— 不标关系，下游只能用通用组件。

例：
  python scripts/emit_facts.py --topic "茶马古道" --mode full --sync-text assets/sources/_research/facts.txt
  （--sync-text：同时把网络段写进 assets/sources/文本.md，见 §0d D；
    事实清单固定放 assets/sources/_research/facts.txt —— 该目录由 §0d E 整目录清空）

本地素材（§0c 统一结构化）—— 与网络段同形，只是不写溯源 JSON：
  python scripts/emit_facts.py --local --sync-text --topic "现场实训素材" \\
      assets/sources/_local_facts.txt --pages 15 --facade 7
  （清单格式相同：`L001 | <要点> | <块名> | <素材名>` ＋ `#GROUP L1 | <关系> | <块名>`；
    source_url 可留空；组 id 用 L 前缀，渲染成 `### [L1] 关系 · 块名`）
""",
    )
    parser.add_argument(
        "file", nargs="?", help="事实清单文件；省略则从 stdin 读"
    )
    parser.add_argument("--topic", default="", help="检索主题")
    parser.add_argument(
        "--mode",
        choices=["gap", "full"],
        default="full",
        help="gap=定点补缺口 / full=全量取材（默认 full）",
    )
    parser.add_argument(
        "-o", "--output", default=DEFAULT_OUTPUT, help=f"输出路径（默认 {DEFAULT_OUTPUT}）"
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="不写文件，只校验已存在的 facts.json",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只解析并打印，不落盘"
    )
    parser.add_argument(
        "--sync-text",
        action="store_true",
        help="同时把网络段写进 文本.md（默认关闭；路径见 --text-path）",
    )
    parser.add_argument(
        "--text-path",
        default=DEFAULT_TEXT_MD,
        help=f"文本.md 路径（默认 {DEFAULT_TEXT_MD}）",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="本地来源模式（§0c 统一结构化）：不写 _web_text_facts.json、"
             "不要求 source_url、--sync-text 写「本地段」（__来源本地 ·）。"
             "必须与 --sync-text 同用",
    )
    parser.add_argument(
        "--pages", type=int, default=DEFAULT_PAGES,
        help=f"PPT 总页数（默认 {DEFAULT_PAGES}）—— 只用于体量预检",
    )
    parser.add_argument(
        "--facade", type=int, default=DEFAULT_FACADE,
        help=f"门面页数（默认 {DEFAULT_FACADE}）—— 只用于体量预检",
    )
    args = parser.parse_args(argv)

    if args.check_only:
        return check_only(Path(args.output))

    if args.file:
        try:
            text = Path(args.file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"[ERROR] 读不到 {args.file}：{exc}", file=sys.stderr)
            return 1
    else:
        text = sys.stdin.read()

    facts, groups, errors = parse_facts(
        text, require_url=not args.local,
        default_prefix='L' if args.local else 'F')
    if errors:
        print("[ERROR] 事实清单有问题，未写文件：", file=sys.stderr)
        for e in errors:
            print(f"   · {e}", file=sys.stderr)
        return 1

    # ── 本地来源模式（§0c 统一结构化）──
    # 与网络模式共用整条解析 / 渲染 / 关系校验链，只有两处不同：
    #   ① 不写 _web_text_facts.json（本地素材不对外部来源负责 ⇒ 无溯源产物）；
    #   ② --sync-text 落的是「本地段」（__来源本地 · ）。
    if args.local:
        if not args.sync_text:
            print("[ERROR] --local 必须与 --sync-text 同用 —— "
                  "本地来源模式的产物就是 文本.md 的本地段。", file=sys.stderr)
            return 1
        if args.dry_run:
            print(build_web_section(args.topic, facts, groups, mark=LOCAL_SECTION_MARK))
        else:
            action = sync_local_section(args.topic, facts, Path(args.text_path), groups)
            print(f"   → 文本.md 本地段已{action}（{len(facts)} 条）：{args.text_path}")
        report_group_coverage(facts, groups)
        report_fact_density(facts, args.pages, args.facade)
        return 0

    payload = build_payload(args.topic, args.mode, facts, groups)

    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if args.sync_text:
            print("\n--- 文本.md 网络段预览 ---")
            print(build_web_section(args.topic, facts, groups))
        return 0

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)

    # 写完当场回读复验 —— 与 check_web_research.py 同一套判据
    rc = check_only(out)
    if rc != 0:
        return rc
    print(f"   → 已写 {out}")
    # 关系覆盖与 --sync-text 无关（它是 facts 本身的结构信息）⇒ 无条件报，
    # 免得只生成 JSON 时看不到「一条关系都没标」这个下游代价最高的信号。
    report_group_coverage(facts, groups)
    if args.sync_text:
        action = sync_text_md(args.topic, facts, Path(args.text_path), groups)
        print(f"   → 文本.md 网络段已{action}（{len(facts)} 条）：{args.text_path}")
        report_fact_density(facts, args.pages, args.facade)
        warn_if_body_short(Path(args.text_path), args.pages, args.facade)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
