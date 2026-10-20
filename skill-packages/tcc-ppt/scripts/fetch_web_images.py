#!/usr/bin/env python3
"""§0f：无图时的联网补图（百度图片）。

当素材里一张图都没有时，自动从百度图片搜索补一批「背景 / 概念类」候选图。

用法：
    # 【主路径】智能体通读 文本.md 后指定主题词（推荐，支持多组，用 ; 分隔）
    # ⚠️ 下面只是格式示意，主题词须按本次素材现读现定
    python scripts/fetch_web_images.py --keywords "领域词 对象词;通用知识 词" --count 12

    # 单组主题
    python scripts/fetch_web_images.py --keywords "素材主题 关键词" --count 8

    # 【兜底】智能体未指定时，脚本按 标题+领域词表+后缀特征 提取
    python scripts/fetch_web_images.py --count 12
    python scripts/fetch_web_images.py --count 12 --min-width 900

关键词来源优先级：
    ① `--keywords` 指定 —— 执行素材准备的智能体读一遍文本.md 后给出（最准）
       · **建议给 3~5 组主题**（多组用 `;` 分隔），覆盖素材的不同知识板块
       · 组数越多，覆盖面越广；`--count` 是**总张数**，脚本自动在组间均分
    ② 脚本兜底提取 —— 标题行领域词 → 全文领域词命中（只能出 1 组）
    ③ 两者皆无 —— 放弃补图（宁缺毋滥），内容页走纯图形/表格布局

产出：
    - sources/image_NNN.ext         下载的图（编号接续现有 image_*）
    - sources/_web_images.json      审计清单（完整字段，含来源 URL）
    - 文本.md 文末追加「网图」登记行

设计边界（最终规则见 references/step_0_extract.md §0f 与 creative_contract.md §1.7）：
    - **配图资格按页面性质分流**（契约 §1.7）：具体实物 / 设备 / 材料 / 工艺动作页可配网图；
      抽象概念页（对标 / 生态 / 评估 / 认证 / 架构 / 机制 / 方法论…）**一律不配图**，走无图骨架
    - 排序**语域优先**（现场实拍 > 中性 > 文章 / 文档 / 证书）—— 不再是"清晰度优先"
    - 下载后**剔除扫描件 / 证书类图**（近白底 + 极低饱和）；剔除后**不补位**（宁缺毋滥）
    - **同源去重**：同标题 / 同目录的裁剪版本只留代表
    - 用到网图的页由 Step 2f 自动加来源标注
    - 按缺口张数补（§0e 会算出期望图量），够用即止
    - 找不到合适图时不阻断流程（exit 0 + 警告）
    - 不重复抓：`_web_images.json` 的 `keywords` 按「主题 → **实际得图数**」记账。
      **只有抓到 ≥1 张的组才算「已完成」** —— 0 结果 / 搜索失败的组会记 0，
      下次同主题自动重试，不会被误判为「已抓过」而永久跳过。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
SOURCES = SKILL_ROOT / "assets" / "sources"
MD_PATH = SOURCES / "文本.md"
MANIFEST_PATH = SOURCES / "_web_images.json"

# 让脚本能 import source_to_md/ 下的 web_image_search
sys.path.insert(0, str(Path(__file__).resolve().parent / "source_to_md"))
from web_image_search import (  # noqa: E402
    BaiduImageSearch,
    BaiduImageSearchError,
    MIN_ACTUAL_WIDTH,
)

# ── 停用词：结构词 + 常见虚词（防止提词提到「第一条/备注/说明」这类）──
STOPWORDS = {
    "进行", "通过", "包括", "对于", "以及", "一个", "可以", "使用", "经过",
    "没有", "同时", "并且", "因为", "或者", "但是", "就是", "还有", "什么",
    "怎么", "为什么", "这个", "这些", "那些", "我们", "你们", "他们",
    "第一条", "第二条", "第三条", "第四条", "备注", "说明", "目录", "摘要",
    "小结", "下一步", "计划", "收获", "问题", "改进", "学习", "工作",
    "现场", "今天", "上午", "下午", "晚上", "明天", "昨天", "已经",
    "主要", "重点", "相关", "内容", "情况", "方面", "过程", "方法", "要求",
    "规定", "标准", "根据", "按照", "关于", "其中", "其他", "以上", "以下",
}

# ── 文档类型词：常出现在标题里，但**不是主题**，必须剔除 ──
TITLE_NOISE = {
    "实习记录", "日报", "周报", "月报", "记录", "汇报", "总结", "报告",
    "材料", "文档", "附件", "备注", "目录", "摘要", "提纲", "汇总",
    "清单", "个人", "日报表", "统计",
}

# ── 领域特征后缀：用「X+后缀」切出专业名词（通用，不绑定具体项目）──
DOMAIN_SUFFIXES = (
    "站", "罐", "阀", "泵", "塔", "炉", "池",
    "系统", "工艺", "结构", "管线", "管道", "设备", "装置", "机组",
    "管网", "码头", "平台", "中心", "区域", "车间", "厂区",
    "安全", "规范", "流程", "方案", "技术", "工程", "项目",
    "作业", "施工", "巡检", "防护", "应急", "检测", "验收", "调试",
    "保冷", "保温", "防腐", "焊接", "吊装", "浇筑",
)

# ── 通用工业词表（兜底用；**不含具体项目名**，可按素材扩充）──
DOMAIN_TERMS = {
    "液化天然气", "天然气", "储罐", "气化", "卸料", "装卸", "火炬",
    "高处作业", "安全带", "动火", "受限空间", "脚手架", "临边", "基坑",
    "钢筋", "模板", "试压", "吹扫", "置换", "仪表", "阀门", "法兰",
    "压缩机", "换热器", "消防", "喷淋", "灭火", "可燃气体", "联锁",
    "主控室", "配电", "接地", "防雷", "静电", "防爆", "储运",
    "接收站", "罐区", "泵房", "卸料臂", "保冷层", "工艺流程",
}

# ── 虚词/停连词：长串先按这些字符切碎，消除「作业的安全」这类粘连 ──
_CLITIC_CHARS = "的了在和与及或对为从到向于是着过很就都还也才再又"

# ── 候选词里含这些动词 → 说明边界跑偏了，丢弃 ──
VERB_NOISE = (
    "进行", "完成", "通过", "包括", "使用", "已经", "正在", "开展",
    "组织", "参加", "负责", "针对", "围绕", "结合", "实现", "做好",
    "加强", "确保", "按照", "根据", "属于", "存在", "出现", "需要",
)

DEFAULT_COUNT = 12    # 默认补图**总**张数（按 15~20 页 deck 的备图量估）
MAX_COUNT = 24        # 单次总上限

# ── 多组主题词的分隔符（半角分号 / 竖线 / 全角分号都认）──
KEYWORD_SEP = r"[;|；]"


def _title_lines(text: str, scan: int = 20) -> list[str]:
    """从开头若干行里挑出「像标题」的行（去 markdown 标记 / 序号 / 句末标点）。"""
    out = []
    for line in text.split("\n")[:scan]:
        s = re.sub(r"^[#>\-*\s\d.、]+", "", line.strip())
        if not s or len(s) > 30:
            continue
        if s.endswith(("。", "，", "；", "、", ".", ",", "：", ":")):
            continue
        out.append(s)
    return out


def _candidates(segment: str) -> list[str]:
    """从一段文本抽出候选主题词：领域词表命中 + 后缀特征词 + 英文缩写。"""
    out: list[str] = []
    # ① 领域词表直接命中（权重最高，见 rank）
    out += [t for t in DOMAIN_TERMS if t in segment]
    # ② 后缀特征词：先按「标点 + 虚词」切碎，再切「X+后缀」（最多 4 字前缀）
    for frag in re.split(rf"[^\u4e00-\u9fffA-Za-z0-9]+|[{_CLITIC_CHARS}]", segment):
        if len(frag) < 2:
            continue
        for suf in DOMAIN_SUFFIXES:
            out += re.findall(rf"[\u4e00-\u9fff]{{1,4}}{suf}", frag)
    # ③ 英文 / 数字缩写（如 ABC、DCS…）
    out += re.findall(r"\b[A-Z]{2,6}\b", segment)
    return [w for w in out if 2 <= len(w) <= 8
            and w not in STOPWORDS
            and w not in TITLE_NOISE
            and not any(v in w for v in VERB_NOISE)]


def _is_dup(a: str, b: str, thresh: float = 0.6) -> bool:
    """判断两候选是否「同一个词被切成了两半」（子串包含 或 字符重叠率过高）。"""
    if a == b or a in b or b in a:
        return True
    common = len(set(a) & set(b))
    return common / max(1, min(len(a), len(b))) >= thresh


def extract_keywords(text: str, top_n: int = 3) -> str:
    """兜底提取主题词 —— 仅在智能体未用 `--keywords` 指定时调用。

    优先级：
      ① **标题行**里的领域词  —— 标题最能代表主题
      ② 全文领域词命中（按 频次 × 标题加权 × 词表命中 × 词长 排序）
      ③ 提不出 → 返回空串 → **放弃补图**（宁缺毋滥，不硬搜一堆不相关的图）

    ⚠️ **这是兜底，不是主路径。** 主路径是执行素材准备的智能体通读
    `文本.md` 后，用 `--keywords "主题词A 主题词B"` 直接指定 —— 语义理解
    永远比词表匹配准。纯词频统计对「无主题文本」根本无效。
    """
    if not text.strip():
        return ""

    def rank(words: list[str]) -> list[str]:
        freq = Counter(words)
        titled: set[str] = set()
        for line in _title_lines(text):
            titled |= set(_candidates(line))
        scored = [(n + (5 if w in titled else 0)
                   + (3 if w in DOMAIN_TERMS else 0)
                   + (2 if w.isascii() else 0)        # 英文缩写（如 ABC/DCS）是强主题信号
                   + len(w) * 0.1, w)
                  for w, n in freq.items()]
        scored.sort(key=lambda x: (-x[0], -len(x[1])))
        picked: list[str] = []
        for _, w in scored:                       # 去掉重复/残缺候选，保留分更高的
            if any(_is_dup(w, p) for p in picked):
                continue
            picked.append(w)
            if len(picked) >= top_n:
                break
        return picked

    title_words: list[str] = []
    for line in _title_lines(text):
        title_words += _candidates(line)

    picked = rank(title_words) or rank(_candidates(text[:4000]))
    return " ".join(picked)


def split_keywords(raw: str) -> list[str]:
    """把 --keywords 拆成多组主题（`;` / `|` / `；` 分隔），保序去重。"""
    out: list[str] = []
    for g in re.split(KEYWORD_SEP, raw or ""):
        g = g.strip()
        if g and g not in out:
            out.append(g)
    return out


def find_next_image_id(sources: Path) -> int:
    """返回下一个可用的 image 编号（现有最大编号 + 1）。"""
    max_id = 0
    for f in sources.glob("image_*"):
        m = re.match(r"image_(\d+)", f.name)
        if m:
            max_id = max(max_id, int(m.group(1)))
    return max_id + 1


def load_manifest(sources: Path) -> dict:
    if MANIFEST_PATH.is_file():
        try:
            return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"fetched_at": None, "keywords": {}, "engine": "baidu", "images": []}


def counts_from_md(md_path):
    """从 `文本.md` 的「## 联网补充图片」登记段反推各主题**实际登记的张数**。

    用于迁移**旧版** `keywords`（list，只存主题名、不存张数）。

    返回 `None` 表示**没有登记段**（文本缺失 / 被覆盖）→ 信息不足，调用方应保守处理；
    返回 `{}` 表示**有登记段但一条登记都没有**（等价于所有主题都是 0 张）。
    """
    if not md_path or not Path(md_path).is_file():
        return None
    try:
        text = Path(md_path).read_text(encoding="utf-8")
    except Exception:
        return None
    parts = text.split("## 联网补充图片", 1)
    if len(parts) < 2:
        return None
    counts: dict[str, int] = {}
    for line in parts[1].splitlines():
        if "[[" not in line:
            continue
        m = re.search(r"主题：([^|）)]+)", line)     # 登记行：… | 主题：X | 标题：… …
        if m:
            kw = m.group(1).strip()
            counts[kw] = counts.get(kw, 0) + 1
    return counts


def as_counts(raw, md_path=None) -> dict[str, int]:
    """把 manifest 的 `keywords` 归一成 `{主题: 得图数}`。

    - **新格式**（dict）：抓到几张就记几；`0` = 上次颗粒无收 → 下次自动重试。
    - **旧格式**（list）：只存主题名 → 用 `文本.md` 登记段**反推**张数：
      · 有登记段 → 某主题无登记行，说明它当时**确实一张没抓到** → 记 `0`（自动重试）；
      · 无登记段（文本缺失/被覆盖）→ 无从判断 → 记 `-1`（张数未知，保守跳过）。
    """
    if isinstance(raw, dict):
        out: dict[str, int] = {}
        for k, v in raw.items():
            try:
                out[str(k)] = int(v)
            except (TypeError, ValueError):
                out[str(k)] = -1
        return out
    if isinstance(raw, list):
        known = counts_from_md(md_path)
        if known is None:               # 无登记段 → 张数未知，保守跳过
            return {str(k): -1 for k in raw}
        return {str(k): known.get(str(k), 0) for k in raw}
    return {}


def save_manifest(sources: Path, manifest: dict) -> None:
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="无图时的联网补图（百度）")
    ap.add_argument("--keywords",
                    help="搜索主题词；多组用 ; 分隔（如 \"A B;C D\"）；缺省自动从 文本.md 提取")
    ap.add_argument("--count", type=int, default=DEFAULT_COUNT,
                    help=f"下载总张数（默认 {DEFAULT_COUNT}，上限 {MAX_COUNT}）；多组时自动均分")
    ap.add_argument("--min-width", type=int, default=MIN_ACTUAL_WIDTH,
                    help=f"实际宽度下限（默认 {MIN_ACTUAL_WIDTH}px）")
    ap.add_argument("--force", action="store_true",
                    help="该主题已抓过时仍强制重抓（默认跳过，见 §0f 约束 #4）")
    args = ap.parse_args()

    if not SOURCES.is_dir():
        print("❌ sources/ 不存在 —— 请先执行 §0a / 0b")
        return 1

    # ① 确定主题词组
    #    主路径：智能体通读 文本.md 后用 --keywords 指定（语义理解最准；支持多组）
    #    兜底：智能体没给时，脚本按词表/后缀特征提取（只出 1 组）；提不出就放弃补图
    if args.keywords:
        groups = split_keywords(args.keywords)
        print(f"🔍 指定 {len(groups)} 组主题词:")
        for g in groups:
            print(f"     · {g}")
    else:
        if not MD_PATH.is_file():
            print("❌ 既无 --keywords，又无 文本.md，无法确定搜索主题")
            return 1
        text = MD_PATH.read_text(encoding="utf-8")
        kw = extract_keywords(text)
        if not kw:
            print("⚠️ 兜底提取未得到主题词（文本缺少领域特征词）")
            print("   → 跳过联网补图，不阻断流程；内容页将走纯图形/表格布局")
            print('   建议：先读一遍 文本.md，再用 --keywords "主题A;主题B;主题C" 指定')
            return 0
        groups = [kw]
        print(f"🔍 兜底提取关键词: {kw}")
        print("   ⚠️ 兜底只能出 1 组、且可能不准 —— 建议智能体通读文本后用 --keywords 指定")

    if not groups:
        print("❌ --keywords 解析后为空")
        return 1

    count = min(max(1, args.count), MAX_COUNT)

    # ①b 重复抓检查（§0f 硬约束 #4「不重复抓」）—— 逐组判定
    #     判据是「**抓到过几张**」而不是「搜过没有」：只有 ≥1 张才算已完成。
    #     ⚠️ 0 结果的组若被记成「已抓过」，该主题将永远补不上（除非加 --force）——
    #        这是旧版 `搜过即记账` 的缺陷，现改为按张数记账并自动重试。
    #     旧版 list 格式的清单会用 `文本.md` 登记段反推张数（反推不出才记 -1）。
    manifest = load_manifest(SOURCES)
    kw_counts = as_counts(manifest.get("keywords"), MD_PATH)
    manifest["keywords"] = kw_counts
    if kw_counts and not args.force:
        kept: list[str] = []
        for g in groups:
            cnt = kw_counts.get(g)
            if cnt is None:
                kept.append(g)
            elif cnt < 0:
                print(f"  ⊘ 已抓过，跳过: {g}（旧版记录未存张数；确需重抓请加 --force）")
            elif cnt > 0:
                print(f"  ⊘ 已抓过（{cnt} 张），跳过: {g}")
            else:
                print(f"  ↻ 上次 0 张（未获图），本次自动重试: {g}")
                kept.append(g)
        groups = kept
        if not groups:
            print("⊘ 全部主题均已抓过（记录在 _web_images.json）")
            print("   → 跳过，避免重复索取同一批图。确需重抓请加 --force")
            return 0

    # ② 组间分配张数（--count 是**总**张数，尽量均分；余数给前面的组）
    per = max(1, count // len(groups))
    extra = count - per * len(groups)

    searcher = BaiduImageSearch()
    next_id = find_next_image_id(SOURCES)
    manifest["fetched_at"] = datetime.now().isoformat(timespec="seconds")

    saved: list[str] = []
    register_lines: list[str] = []
    got_by_kw: dict[str, int] = {}   # 本次各组实际得图数（0 = 颗粒无收 → 下次重试）
    carry = 0                      # 上一组没拿够的余量，顺延给下一组

    # ③ 逐组搜索 + 下载 + 登记
    for gi, kw in enumerate(groups):
        if len(saved) >= count:
            break
        want = min(per + (1 if gi < extra else 0) + carry, count - len(saved))
        if want <= 0:
            break
        if len(groups) > 1:
            print()
            print(f"── [{gi + 1}/{len(groups)}] {kw}   计划 {want} 张")

        try:
            cands, stats = searcher.search(kw, limit=max(want * 3, 20))
        except BaiduImageSearchError as e:
            print(f"  ⚠️ 搜索失败: {e}  → 跳过该组，余量让给下一组")
            print("     · 该组记为 0 张 → 下次同主题会自动重试")
            got_by_kw[kw] = 0
            carry = want
            continue

        print(f"     接口 {stats.raw} 条 → 横向 {stats.landscape} → 宽度达标 {stats.wide_enough}"
              + (f" → 同源去重 -{stats.duplicates}" if stats.duplicates else ""))

        if not cands:
            print("  ⚠️ 没有符合条件的结果（可能关键词太冷门）")
            print("     · 该组记为 0 张 → 下次同主题会自动重试（不视为「已抓过」）")
            got_by_kw[kw] = 0
            carry = want
            continue

        got = 0
        for c in cands:
            if got >= want or len(saved) >= count:
                break
            ext = ".jpg" if c.img_type != "png" else ".png"
            name = f"image_{next_id:03d}{ext}"
            dest = SOURCES / name
            ok, msg = searcher.download(c, dest, min_actual_width=args.min_width)
            if not ok:
                print(f"  [跳过] {msg}")
                continue
            next_id += 1
            got += 1
            saved.append(name)
            print(f"  [✓] {name}  {msg}")

            # 登记行（与散图结构统一的「网图」三段式）
            segs = [f"主题：{kw}"]
            if c.title:
                segs.append(f"标题：{c.title[:60]}")
            if c.source_host:
                segs.append(f"来源：{c.source_host}")
            # 版权留痕：优先原始出处；百度偶有不返回该字段 → 回退 CDN 地址，不留空段
            if c.source_url:
                segs.append(f"URL：{c.source_url[:100]}")
            elif c.middle_url:
                segs.append(f"URL：{c.middle_url[:100]}（CDN 回退）")
            else:
                segs.append("URL：(源站未提供)")
            segs.append(f"尺寸：{c.width}×{c.height}")
            register_lines.append(f"[[{name}]]（网图 | {' | '.join(segs)}）")

            # 审计清单
            manifest["images"].append({
                "file": name, **c.as_dict(),
            })

        got_by_kw[kw] = got           # 按「实际得图数」记账（0 张不算完成）
        carry = max(0, want - got)     # 没拿够的余量顺延给下一组

    # ④ 写回记账 —— 抓到 0 张的组照样记录（值 0），作为「下次自动重试」的依据
    for kw, got in got_by_kw.items():
        prev = kw_counts.get(kw, 0)
        if prev < 0:                   # 旧版记录（张数未知）→ 用本次实测数覆盖
            prev = 0
        kw_counts[kw] = prev + got
    manifest["keywords"] = kw_counts

    # ⑤ 落盘审计清单 —— **无论是否抓到都落盘**（0 张也要留痕，供下次重试判定）
    save_manifest(SOURCES, manifest)

    if not saved:
        print("⚠️ 未成功下载任何图，不阻断流程")
        print("   · 已在审计清单记为 0 张 → 下次同主题会自动重试，不会被误判为「已抓过」")
        return 0

    # ⑥ 追加登记到 文本.md
    with MD_PATH.open("a", encoding="utf-8") as f:
        f.write("\n\n## 联网补充图片\n")
        f.write("\n".join(register_lines))
        f.write("\n")

    print()
    print(f"✅ 联网补图 {len(saved)} 张（{len(groups)} 组主题）→ {SOURCES}")
    print(f"   审计清单: {MANIFEST_PATH.name}")
    print(f"   已登记进 文本.md（网图类型）")
    print()
    print("💡 这些是【网图】（调用方已开启联网搜图）；用到它们的页会由 Step 2f 自动加一行来源标注（§1.6）。")
    print("   ⚠️ 来源标注只是版权留痕、不等于获得授权 —— 对外材料如需使用，请先确认授权。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
