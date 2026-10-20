#!/usr/bin/env python3
"""校验「联网取材」的溯源完整性（§0d 产出 + Step 2 声明）。

职责：把「内容可溯源」这件事变成**机器判据**（契约 §1.6 的红线不靠自觉）。

检查项：
    ① `_web_text_facts.json` 结构合法（schema / facts / 字段齐全）
    ② `fact_id` 唯一、且**顺序编号**（F001、F002…，不允许跳号）
    ③ `source_url` 非空、是 http(s) 绝对地址 —— 🔴 直接决定「可溯源」
    ④ 009 正文片段里 `<!--FACTS: …-->` 声明的每个 id **都必须真实存在**
       （契约 §1.6：**不能漏、不能编**）
    ⑤ 同一片段**只允许一行** `<!--FACTS: …-->`（重复声明会让注入器取到不完整集合）

无 `_web_text_facts.json` = 这份 deck 没用网文 ⇒ **直接通过**（绝大多数 deck 都是这样）。

退出码：0 = 通过（可含警告）；1 = 有硬错误。
用法：
    python scripts/validators/check_web_research.py
    python scripts/validators/check_web_research.py --quiet
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

SKILL_ROOT = Path(__file__).resolve().parents[2]      # scripts/validators/ → skill 根
SOURCES = SKILL_ROOT / "assets" / "sources"
BODY_DIR = SKILL_ROOT / "assets" / "svg_body"
FACTS_PATH = SOURCES / "_web_text_facts.json"

sys.path.insert(0, str(SKILL_ROOT / "scripts"))
try:
    from console_encoding import configure_utf8_stdio  # noqa: E402

    configure_utf8_stdio()
except Exception:                                     # 编码配置失败不该阻断校验
    pass

EXPECTED_SCHEMA = "tcc-ppt-skill-webtext-facts/v1"

FACTS_RE = re.compile(r"<!--\s*FACTS\s*:\s*(?P<ids>[^>]*?)\s*-->")
FACT_ID_RE = re.compile(r"F\d+")
FACT_ID_FULL_RE = re.compile(r"^F(\d{3,})$")


def _diagnose_json_error(exc: Exception, text: str) -> str:
    """把 `JSONDecodeError` 变成「哪一行、什么内容、怎么改」的可操作提示。

    🔑 动机（2026-10-09 实测）：模型手写这个 JSON 时最容易漏转义 `claim` 里的双引号
    （如 `"claim": "俗称"安全带"…"`）。原实现只打印 `JSONDecodeError: Expecting ',' delimiter`，
    模型要自己数行号才能定位 ⇒ 白烧一轮 debug。
    """
    line_no = getattr(exc, "lineno", None)
    col_no = getattr(exc, "colno", None)
    msg = getattr(exc, "msg", "") or str(exc)

    lines = [
        f"_web_text_facts.json 不是合法 JSON（{type(exc).__name__}: {msg}）",
        f"   位置：第 {line_no} 行第 {col_no} 列" if line_no else "",
    ]

    if line_no:
        src = text.splitlines()
        ctx = src[line_no - 1] if 0 < line_no <= len(src) else ""
        if ctx:
            lines.append(f"   该行内容：{ctx.strip()}")
            # 列号处打一个插入符，直观指出出错位置
            if col_no and col_no <= len(ctx) + 1:
                lines.append("             " + " " * (col_no - 1) + "^")
            # 最可能的成因：字符串值里的裸双引号（按列号判定，比数引号个数可靠）
            if '"' in ctx:
                before = ctx[: col_no - 1] if col_no else ctx
                if before.count('"') % 2 == 0:
                    lines.append(
                        "   💡 最可能的原因：某个字符串值里出现了**未转义的双引号** —— "
                        '须写成 \\" （如 "claim": "俗称\\"安全带\\"的装置"）'
                    )

    lines.extend(
        [
            "   💡 检查要点：① 字符串内的双引号是否转义为 \\\"；② 行尾是否缺逗号或多逗号；",
            "                ③ 换行是否写在字符串内（JSON 字符串不能跨行，须用 \\n）。",
            "   修好后重跑本命令；也可用 `python -c \"import json;json.load(open('assets/sources/_web_text_facts.json',encoding='utf-8'))\"` 复验。",
        ]
    )
    return "\n".join(part for part in lines if part)


def load_facts() -> tuple[dict[str, dict], list[str], list[str]]:
    """返回 (facts, errors, warnings)。文件不存在 → (空, 空, 空) 表示未使用网文。"""
    if not FACTS_PATH.is_file():
        return {}, [], []

    errors: list[str] = []
    warnings: list[str] = []
    raw_text = FACTS_PATH.read_text(encoding="utf-8")
    try:
        data = json.loads(raw_text)
    except Exception as e:
        return {}, [_diagnose_json_error(e, raw_text)], []

    if not isinstance(data, dict):
        return {}, ["_web_text_facts.json 顶层必须是对象"], []

    schema = str(data.get("schema") or "")
    if schema != EXPECTED_SCHEMA:
        errors.append(f"schema 应为 {EXPECTED_SCHEMA}，实得 {schema or '(缺失)'}")

    raw = data.get("facts")
    if raw is None:
        errors.append("缺少 facts 数组")
        return {}, errors, warnings
    if not isinstance(raw, list):
        errors.append("facts 必须是数组")
        return {}, errors, warnings

    out: dict[str, dict] = {}
    for idx, item in enumerate(raw, 1):
        where = f"facts[{idx - 1}]"
        if not isinstance(item, dict):
            errors.append(f"{where} 必须是对象")
            continue
        fid = str(item.get("fact_id") or "").strip()
        claim = str(item.get("claim") or "").strip()
        url = str(item.get("source_url") or "").strip()

        if not fid:
            errors.append(f"{where} 缺少 fact_id")
            continue
        if fid in out:
            errors.append(f"{where} fact_id 重复: {fid}")
            continue
        if not FACT_ID_FULL_RE.match(fid):
            errors.append(f"{where} fact_id 格式应为 F + 至少 3 位数字（如 F001），实得 {fid}")
        if not claim:
            errors.append(f"{fid} 缺少 claim")
        if not url:
            errors.append(f"{fid} 缺少 source_url —— 溯源红线（契约 §1.6）")
        else:
            p = urlparse(url)
            if p.scheme not in ("http", "https") or not p.netloc:
                errors.append(f"{fid} 的 source_url 不是 http(s) 绝对地址: {url[:60]}")

        out[fid] = {"claim": claim, "url": url}
    return out, errors, warnings


def declared_by_page() -> tuple[dict[str, list[str]], list[str]]:
    """返回 ({片段名: [fact_id]}, errors)。"""
    errors: list[str] = []
    per_page: dict[str, list[str]] = {}
    if not BODY_DIR.is_dir():
        return per_page, errors

    for f in sorted(BODY_DIR.glob("*.svg")):
        text = f.read_text(encoding="utf-8", errors="replace")
        hits = FACTS_RE.findall(text)
        if not hits:
            continue
        if len(hits) > 1:
            errors.append(
                f"{f.name}: 出现 {len(hits)} 行 <!--FACTS: …-->，只允许一行"
                "（重复声明会让注入器取到不完整集合）"
            )
        ids: list[str] = []
        for blob in hits:
            body = blob
            bad = re.findall(r"[A-Za-z0-9_]+", body)
            for tok in bad:
                if not FACT_ID_RE.fullmatch(tok):
                    errors.append(f"{f.name}: 锚点里有非法标识 {tok!r}（应形如 F001）")
            for fid in FACT_ID_RE.findall(body):
                if fid not in ids:
                    ids.append(fid)
        if ids:
            per_page[f.name] = ids
    return per_page, errors


def main() -> int:
    ap = argparse.ArgumentParser(description="联网取材溯源校验")
    ap.add_argument("--quiet", action="store_true", help="只打印结论与问题")
    args = ap.parse_args()

    log = (lambda *a: None) if args.quiet else print

    facts, errors, warnings = load_facts()
    if not FACTS_PATH.is_file():
        print("✅ 未使用网文（无 _web_text_facts.json）—— 无需溯源校验")
        return 0

    print("=== 联网取材溯源校验 ===")
    log(f"  事实条数: {len(facts)}")
    if facts:
        n_url = sum(1 for v in facts.values() if v["url"])
        log(f"  带 source_url: {n_url}/{len(facts)}")
    print()

    per_page, page_errors = declared_by_page()
    errors += page_errors

    referenced: set[str] = set()
    for name, ids in per_page.items():
        for fid in ids:
            referenced.add(fid)
            if fid not in facts:
                errors.append(
                    f"{name}: 声明了 facts.json 里不存在的 {fid}"
                    "（契约 §1.6：不能漏、不能编）"
                )

    if per_page:
        log("  页面锚点:")
        for name, ids in per_page.items():
            log(f"    {name}: {'、'.join(ids)}")
        log("")
        unused = sorted(set(facts) - referenced)
        if unused:
            warnings.append(f"以下事实从未被任何页面引用: {'、'.join(unused)}")
    else:
        warnings.append(
            "没有任何页面声明 <!--FACTS: …--> —— 网文事实全部闲置，PPT 里不会出现内容来源标注"
        )

    if errors:
        print(f"❌ 有 {len(errors)} 个硬错误：")
        for e in errors:
            print(f"    · {e}")
    if warnings:
        print(f"⚠️ {len(warnings)} 条警告：")
        for w in warnings:
            print(f"    · {w}")
    if not errors:
        print("✅ 溯源校验通过")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
