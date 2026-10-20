#!/usr/bin/env python3
"""check_creative_svg.py — 校验 009 内容页正文 SVG 片段

把契约三册（references/creative_contract.md + contract_elements.md + contract_layout.md）里的硬规矩翻译成机器检查，在 Step 2 创作完就拦截，
避免带着隐患冲进 Step 5/6 那个"一页坏全盘挂"的关卡。

检查项：
  A. 语法结构：无根 <svg>、XML 合法、canonical authoring 规则
  B. 合规红线：字体(黑体/Arial)、字号(26.67/18.67)、字重(标题加粗)、配色(公司8色)
  C. 空间边界：y >= 88 不盖页头
  D. 资源：照片文件存在、无残留占位符、图框宽高比与裁剪保留率（§6.1）
  E. 内容：非空、填充率、净字数、中间空白带（§2.4 指标①②③）
  E2. 图标（§4.9）：`<use>` 必须带 `data-icon`（否则不展开）、边长在允许范围
      （24~28 / 36 / 44）、**浅色圆底上必须有图标**（防"空圆圈"）
  F. 纵向收口：内容主体底边是否收到 648 附近（§2.4 指标④）——
     **底边 < 560 → 硬错误（阻断）；560~608 → 软警告**
  G. 块内密度：容器框内"文字内容跨度 ÷ 框高"是否 ≥ 50%（契约版式册 §5.0.1）——
     抓"半空容器"（框撑得很大、里面是空的；实测 page05 = 36%）；
     **< 40% → 硬错误（阻断）；40~50% → 软警告**

用法：
  python scripts/validators/check_creative_svg.py             # 扫描 svg_body/ 全部
  python scripts/validators/check_creative_svg.py page06.svg  # 检查指定片段
"""

import os
import re
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

SCRIPT_DIR = Path(__file__).resolve().parent.parent   # scripts/
PROJECT = SCRIPT_DIR.parent                            # skill 根目录
BODY_DIR = PROJECT / "assets" / "svg_body"
SOURCES_DIR = PROJECT / "assets" / "sources"

sys.path.insert(0, str(SCRIPT_DIR))

from console_encoding import configure_utf8_stdio  # noqa: E402
from svg_authoring_contract import canonical_authoring_errors  # noqa: E402

configure_utf8_stdio()

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_HREF = "{http://www.w3.org/1999/xlink}href"

# ─── 合规常量 ───
# 公司标准 8 色（creative_contract.md §1.3）
# 🔵 2026-10-08：原 `#3A4A9D`（辅蓝 Secondary）已删除 —— 全库零引用（模板/正文/示例都没用过），
#    岗位被 `#013B94` 取代。删它是为了**收紧红线的语义**：留在表里等于放任模型写一个
#    没人用过的颜色；删掉后写它会直接报「配色违规」。
ALLOWED_COLORS = {
    "#003C96", "#C11920", "#307FE2", "#013B94", "#FFD806",
    "#FFFFFF", "#262626", "#404040",
}
# 来源标注专用浅灰（creative_contract.md §1.6）—— 例外色，
# **仅对带 CREDIT_ROLES 里任一 data-role 的标注元素放行**，其他元素仍严格限定 9 色。
CREDIT_COLORS = {"#808080"}
# 来源标注的两种角色（§1.6）—— 允许多个是**故意的**：
#   img-credit  = 图片来源行（该页实际引用了「网图」）
#   text-credit = 内容来源行（该页引用了联网取回的「网文」事实，锚点 `<!--FACTS: …-->`）
# ⚠️ 新增角色时**必须同步** `inject_image_credits.py` 的同名常量与契约 §1.6。
CREDIT_ROLES = ("img-credit", "text-credit")
# 允许的字体名（§1.1）
ALLOWED_FONTS = {"黑体", "Arial", "sans-serif"}
# 允许的字号（§1.2：20pt=26.67px，14pt=18.67px）
ALLOWED_SIZES = (26.67, 18.67)
SIZE_TOLERANCE = 0.1
# 页头下边界（§1.4：y 必须 >= 88）
HEADER_BOTTOM = 88
# 版面填充率警戒线（§1.5：填充率低于此值视为偏空，仅警告）
FILL_MIN_RATIO = 0.25
# 纵向收口（§2.4 指标④：内容主体底边应收到 648 附近，避免下半截大面积留白）
BOTTOM_TARGET = 648
BOTTOM_TOLERANCE = 40        # 底边低于 648-40=608 即提示"底部留白过大"（软警告）
BOTTOM_HARD = 560            # 🔴 底边低于此值 → **硬错误**（距底线超 88px，页面明显头重脚轻）
# ─── 报错文案里「可执行修复值」用的换算常量 ───
CH_W = 18.67                 # 一个汉字的宽度（18.67px 字号 ≈ 1.0em），用于"≈N 个字"换算。
                             # 🔵 **2026-09-30 用产物校准过**：48 个"盒宽按文字算"的样本里，
                             # 汉字实测中位 **19.88px/字**（= 1.065em，最小 19.82）——
                             # 那是转换器给文本框留的余量；**字形墨宽仍是 1.0em**，
                             # 本器判"是否越界"、以及"删几个字"都以墨宽为准（实测按此建议改一次即过）。
                             # 纯拉丁/数字实测 **0.556em**，与下方 0.55 的取值吻合。
MIN_GAP = 24                 # §2.2 实体区块最小间距（算"需腾出多少"时用）
TEXT_EDGE_RATIO = 0.5        # text×text 时：交集 < 较短一行的这个比例才判"文字越界"
TEXT_EDGE_MAX_PX = 80.0      # 且交集绝对值须 < 此值（再兜一层，避免短文本被误判）
# 装饰性窄条（组件 C1/C3 的 5px 左色条）——**不算内容**，各统计口径都要排除
DECOR_MIN_W = 20.0
# ─── 正文水平边界（§1.4：内容须落在 x ∈ [40, 1240]）───
CONTENT_LEFT = 40
CONTENT_RIGHT = 1240
BOUNDS_TOL = 1.5             # 浮点/取整容差
# ─── 内容纵向跨度（§5.0.1 铁律：底边 − 顶边 ≥ 504，抓"整体下移"）───
SPAN_MIN = 504               # = 544 − 40；顶边对齐 104 时与"底边 ≥608"等价
SPAN_HARD = 464              # 低于此值 → 硬错误（与 check_design_spec 同口径）


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _fnum(elem, name, default=0.0):
    """安全取数值属性（缺失 / 非数字都退回 default）。"""
    try:
        return float(elem.get(name, default))
    except (ValueError, TypeError):
        return default


def _is_credit(elem):
    """是否为「来源标注」元素（契约 §1.6）—— 图片来源行 **或** 内容来源行。

    标注是机械注入的元信息，**不算内容元素** —— 不参与填充率、净字数统计。
    ⚠️ 两种角色都要认：漏认 `text-credit` 会让内容来源行被当成正文，
    以"看起来完全合理的数值"污染填充率与净字数（§2.4）。
    """
    return (elem.get("data-role") or "") in CREDIT_ROLES


def _is_full_bg(elem):
    """是否为「铺满整页的背景矩形」—— 背景不是内容，各统计口径都必须排除。

    🔴 **不排除会同时打瞎两个指标**（2026-09-30 探测确认）：
    整页浅底 rect 会让 `check_fill` 的填充率被撑到 100%、并让 `check_blank_band`
    的空白带完全消失 —— 也就是说"正文只写了一行"的页面会**全绿通过**。
    🔑 与 `_is_credit`（来源标注）、`DECOR_MIN_W`（5px 色条）同属一类：
    **非内容元素必须在每个统计口径里显式排除**，否则会以"看起来完全合理的数值"污染判定。
    """
    try:
        return (float(elem.get("width", 0)) >= 1240
                and float(elem.get("height", 0)) >= 600)
    except (ValueError, TypeError):
        return False


def _text_width(txt, fs):
    """按字符类型估算文本的渲染宽度（px）。

    🔴 **不能用单一系数**：汉字/全角字符的实际字宽 ≈ **1.0em**，
    拉丁字母、数字、半角标点 ≈ 0.55em。此前统一按 `len × fs × 0.6` 估算，
    会把中文文本盒宽**低估 40%~50%** —— 实测 18 个汉字真实宽约 336px，估算只有 202px，
    于是"文字越界 / 文字相撞"这类错误**只会在擦线时偶然被报出，稍微短一点就完全漏检**。
    """
    total = 0.0
    for ch in txt:
        total += fs * (0.55 if ord(ch) < 0x2E80 else 1.0)
    return total


def check_no_svg_root(body_text):
    errors = []
    if re.search(r"<svg\b", body_text):
        errors.append("正文片段不应含根 <svg> 标签（只需一组 <g> 元素）")
    return errors


def check_placeholders(body_text):
    errors = []
    found = re.findall(r"\{\{[A-Z0-9_]+\}\}", body_text)
    if found:
        errors.append(f"残留占位符: {sorted(set(found))}（正文文字应直接写死，不用占位符）")
    return errors


def check_nonempty(body_text):
    errors = []
    if not body_text.strip():
        errors.append("正文片段为空")
        return errors
    stripped = re.sub(r"<!--.*?-->", "", body_text, flags=re.DOTALL)
    if not re.search(r"<(rect|text|image|path|line|g|circle|ellipse)\b", stripped):
        errors.append("正文片段无实际图形/文字元素")
    return errors


def check_fonts(root):
    errors = []
    for elem in root.iter():
        ff = elem.get("font-family")
        if not ff:
            continue
        for raw in ff.split(","):
            name = raw.strip().strip("\"'")
            if not name:
                continue
            if name not in ALLOWED_FONTS:
                errors.append(f"字体违规: '{name}'（只准黑体/Arial）")
    return errors


def check_font_sizes(root):
    errors = []
    for elem in root.iter():
        fs = elem.get("font-size")
        if fs is None:
            continue
        try:
            val = float(fs)
        except ValueError:
            errors.append(f"字号非法: '{fs}'")
            continue
        if not any(abs(val - s) < SIZE_TOLERANCE for s in ALLOWED_SIZES):
            errors.append(
                f"字号违规: {fs}（只准 26.67=20pt标题 / 18.67=14pt正文）"
            )
    return errors


def check_font_weights(root):
    errors = []
    for elem in root.iter():
        fs = elem.get("font-size")
        if fs is None:
            continue
        try:
            val = float(fs)
        except ValueError:
            continue
        if abs(val - 26.67) < SIZE_TOLERANCE:
            fw = (elem.get("font-weight") or "").strip().lower()
            if fw != "bold":
                label = elem.get("id") or _local(elem.tag)
                errors.append(f"20pt 标题未加粗: {label}（需 font-weight=\"bold\"）")
    return errors


def check_colors(root):
    errors = []
    for elem in root.iter():
        credit = _is_credit(elem)
        for attr in ("fill", "stroke"):
            v = (elem.get(attr) or "").strip()
            if not v or not v.startswith("#"):
                continue
            color = v.upper()
            if credit and color in CREDIT_COLORS:
                continue                      # 来源标注的专用浅灰（§1.6 例外）
            if color not in ALLOWED_COLORS:
                errors.append(f"配色违规: {attr}=\"{v}\"（不在公司8色表）")
    return errors


# ── 图标三查（2026-10-12 新增，契约 §4.9）──────────────────────────────
# 病根：图标这一维**此前没有任何机器判据**，两种错法都会静默通过：
#   ① 尺寸写错（如 width/height=52 配 190 高格 ⇒ 圆底溢出格顶）——
#      `embed_icons` 只按给定宽高缩放、不判合理性；本校验器也不查；
#   ② §VI 填了 None 却按组件 R5 排版（圆底上没有 `<use>`）——
#      会渲染成"空圆圈"，而**其余判据全部照常通过**（填充率靠格底浅底 rect、
#      块内密度靠文字跨度，两者都达标）⇒ 实测整页 ✅ 通过 / exit 0 / 零警告。
# ⇒ 本节三条是这一维**唯一的机器防线**，全部判**硬错误**。
#
# 允许的图标边长（契约 §4.9）：
#   24~28 = 卡片小标题前 / 流程链节点（常规定位）
#   36    = 图标网格单元（组件 R5）—— 「图标作主体」场景
#   44    = 编号条目行序号位（替代 r=22 的序号圆，直径 44）
ICON_SIZE_RANGE = (24.0, 28.0)
ICON_SIZE_EXTRA = (36.0, 44.0)
# 「浅色圆底」= 带 fill-opacity、无 stroke、r ≥ 24 的 circle —— 组件 R5 的圆底特征。
# 🔵 实测标定：真实产物的 9 个 circle 里只有 page13 的 4 个符合；其余都是**实心**，
#    不会被误伤 —— 流程链节点 r=8／分隔点 r=4／组件 C10 序号圆 r=22 实心／
#    组件 R3 的 VS 圆 r=36 实心／§5.19 中心圆 r=70 实心。
ICON_DISC_MIN_R = 24.0


def check_icons(root):
    """图标三查：① `<use>` 必须带 `data-icon` ② 边长在允许范围 ③ 浅色圆底上必须有图标。

    三条都判**硬错误** —— 它们对应的失败模式会**静默通过其余所有判据**。
    """
    errors = []
    uses = [e for e in root.iter() if _local(e.tag) == "use"]

    # ① `<use>` 必须是可展开的 `data-icon="库/名"`，且边长在允许范围
    for u in uses:
        icon = (u.get("data-icon") or "").strip()
        if not icon:
            errors.append(
                "图标写法错误：<use> 缺 data-icon 属性 —— 导出引擎只展开 "
                '`data-icon="库/名"`；写成 xlink:href 之类**不会报错**，'
                "但成稿时图标会缺（契约 §4.9 语法）"
            )
            continue
        if icon.count("/") != 1:
            errors.append(
                f'图标写法错误：data-icon="{icon}" 不是「库/名」两段式（契约 §4.9 语法）'
            )
        w, h = _fnum(u, "width", -1), _fnum(u, "height", -1)
        if w <= 0 or h <= 0:
            errors.append(f"图标 {icon} 缺 width/height（契约 §4.9：尺寸必须显式给）")
            continue
        if abs(w - h) > 0.1:
            errors.append(
                f"图标 {icon} 不是正方形：width={w:g} height={h:g} —— 图标源一律方形，宽高必须相等"
            )
        lo, hi = ICON_SIZE_RANGE
        if not ((lo - 0.1 <= w <= hi + 0.1)
                or any(abs(w - v) <= 0.1 for v in ICON_SIZE_EXTRA)):
            errors.append(
                f"图标尺寸违规：{icon} width/height={w:g} —— 只允许 "
                f"{lo:g}~{hi:g}（卡片小标题前 / 流程链节点）、"
                f"{ICON_SIZE_EXTRA[0]:g}（图标网格单元 · 组件 R5）、"
                f"{ICON_SIZE_EXTRA[1]:g}（编号条目行序号位）；见契约 §4.9 铁律 1"
            )

    # ② 浅色圆底（组件 R5 的圆底）必须压着一个图标 —— 否则就是"空圆圈"
    for elem in root.iter():
        if _local(elem.tag) != "circle":
            continue
        if not (elem.get("fill-opacity") or "").strip():
            continue                    # 实心圆不参与（C10 序号圆 / VS 圆 / 中心圆）
        if (elem.get("stroke") or "").strip():
            continue                    # 组件 R2 的环底
        r = _fnum(elem, "r", 0.0)
        if r < ICON_DISC_MIN_R:
            continue
        cx, cy = _fnum(elem, "cx", 0.0), _fnum(elem, "cy", 0.0)
        covered = False
        for u in uses:
            uw, uh = _fnum(u, "width", 0.0), _fnum(u, "height", 0.0)
            if uw <= 0 or uh <= 0 or not (u.get("data-icon") or "").strip():
                continue
            dx = _fnum(u, "x", 0.0) + uw / 2 - cx
            dy = _fnum(u, "y", 0.0) + uh / 2 - cy
            if dx * dx + dy * dy <= r * r + 0.5:
                covered = True
                break
        if not covered:
            errors.append(
                f"「空圆圈」：浅色圆底 (cx={cx:g}, cy={cy:g}, r={r:g}) 上没有 <use data-icon> —— "
                f"组件 R5 的每个格子必须在圆底内放图标。🔴 若 §VI 填了 None 就**不要用组件 R5**"
                f"（改用 §5.6 卡片矩阵 / 组件 C1）；否则会渲染成一排空圆圈，"
                f"而填充率 / 块内密度 / 配色等判据**全部照常通过**"
            )
    return errors


def check_bounds(root):
    errors = []
    for elem in root.iter():
        tag = _local(elem.tag)
        for attr in ("y", "y1", "y2"):
            v = elem.get(attr)
            if v is None:
                continue
            try:
                y = float(v)
            except ValueError:
                continue
            if y < HEADER_BOTTOM:
                errors.append(
                    f"<{tag}> {attr}={v} 盖住页头（需 >= {HEADER_BOTTOM}）"
                )
        d = elem.get("d")
        if tag == "path" and d:
            nums = re.findall(r"-?\d+\.?\d*", d)
            for i in range(1, len(nums), 2):
                try:
                    y = float(nums[i])
                except ValueError:
                    continue
                if y < HEADER_BOTTOM:
                    errors.append(
                        f"<path> 坐标 y={nums[i]} 盖住页头（需 >= {HEADER_BOTTOM}）"
                    )
                    break

            # ── 角标斜线（2026-10-18 新增 ④-i）──
            #    E 款四角标是 4 条 `10×10` 的两段直角，每段都必须与卡边平行。
            #    实测 v4.14 page11：右下角写成 `M x+w-10,y+h-10 L x+w,y+h L x+w,y+h-10`，
            #    首点比正确值高 10px ⇒ 那条横边变成 45° 斜线；四个角只有右下是歪的，
            #    远看像"角标被削了一刀"。此类笔误肉眼极难发现，用机器兜。
            #    只在 bbox ≤16×16 的小 path 上判 —— 大图形用斜线是正常的（三角形/斜切）。
            _pts = [(float(a), float(bb)) for _c, a, bb in re.findall(
                r"([ML])\s*(-?\d+\.?\d*)[ ,]\s*(-?\d+\.?\d*)", d)]
            if len(_pts) >= 3:
                _xs = [q[0] for q in _pts]
                _ys = [q[1] for q in _pts]
                if (max(_xs) - min(_xs) <= 16) and (max(_ys) - min(_ys) <= 16):
                    for _i in range(len(_pts) - 1):
                        _dx = abs(_pts[_i + 1][0] - _pts[_i][0])
                        _dy = abs(_pts[_i + 1][1] - _pts[_i][1])
                        if _dx > 0.5 and _dy > 0.5:
                            errors.append(
                                f"<path> 小尺寸直角（{d}）的第 {_i + 1} 段是斜线"
                                f"（Δx={_dx:g} / Δy={_dy:g}）—— E 款四角标的每段"
                                f"都必须与卡边平行。常见笔误：右下角起笔点写成 "
                                f"(x+w-10, y+h-10)，**正确是 (x+w-10, y+h)**"
                            )
                            break

        # ── 水平边界（§1.4：内容须落在 x ∈ [40, 1240]）──
        # 🔴 补的是长期盲区：此前 `check_bounds` **只查 y、完全不查 x** ——
        # 一行超宽文字（右缘 2603）能被四个判据全部放过（除非它恰好撞上别的元素）。
        try:
            if tag in ("rect", "image"):
                if _is_full_bg(elem):
                    continue
                x, w = _fnum(elem, "x"), _fnum(elem, "width")
                if tag == "rect" and w < DECOR_MIN_W:
                    continue      # 5px 装饰色条与宿主卡片同心 → 报了就是重复
                errors.extend(_x_bound_msgs(tag, x, x + w, None))
            elif tag == "text":
                txt = "".join(elem.itertext()).strip()
                if not txt:
                    continue
                fs = _fnum(elem, "font-size", 18.67)
                tw = _text_width(txt, fs)
                x = _fnum(elem, "x")
                anchor = (elem.get("text-anchor") or "start").strip().lower()
                if anchor == "middle":
                    x1, x2 = x - tw / 2, x + tw / 2
                elif anchor == "end":
                    x1, x2 = x - tw, x
                else:
                    x1, x2 = x, x + tw
                errors.extend(_x_bound_msgs(tag, x1, x2, tw))
            elif tag == "line":
                errors.extend(_x_bound_msgs(
                    tag, _fnum(elem, "x1"), _fnum(elem, "x2"), None))
            elif tag in ("circle", "ellipse"):
                cx = _fnum(elem, "cx")
                r = _fnum(elem, "r") or _fnum(elem, "rx")
                errors.extend(_x_bound_msgs(tag, cx - r, cx + r, None))
        except (ValueError, TypeError):
            pass
    return errors


def _x_bound_msgs(tag, x1, x2, tw):
    """水平越界文案（§1.4）。`tw` = 文字墨宽（用于给"删 N 个字"的可执行建议）。"""
    out = []
    if x1 < CONTENT_LEFT - BOUNDS_TOL:
        over = CONTENT_LEFT - x1
        out.append(
            f"<{tag}> 左缘 x={x1:.0f} 越出正文左界 {CONTENT_LEFT}（超出 {over:.0f}px）"
            f" → 正文左右留白各 40px（§1.4）：把整块往右挪 {over:.0f}px"
        )
    if x2 > CONTENT_RIGHT + BOUNDS_TOL:
        over = x2 - CONTENT_RIGHT
        hint = ""
        if tw:
            hint = (f"｜该行文字宽约 {tw:.0f}px（超出 {over:.0f}px）"
                    f" → 再删约 {int(over / CH_W) + 1} 个字，或拆成两行")
        out.append(
            f"<{tag}> 右缘 x={x2:.0f} 越出正文右界 {CONTENT_RIGHT}（超出 {over:.0f}px）"
            f"{hint} → 内容须落在 x ∈ [{CONTENT_LEFT}, {CONTENT_RIGHT}]（§1.4）"
        )
    return out


def check_content_span(root):
    """纵向跨度检查（§5.0.1 铁律：内容底边 − 内容顶边 ≥ 504）—— 抓「整体下移」。

    🔴 补的是「**Step 1 只查蓝图、Step 2 不查实际 SVG**」这个漏点：
    实测平台产物有一页蓝图合规、但生成的 SVG 把内容整体下移到 `y=196`，
    顶部空出 92px、底边仍是 648 —— 底边检查满意、跨度却只有 452，**此前没有任何判据覆盖**。

    ⚠️ **只在「底边已达标（≥608）」时才判** —— 底边不达标由 `check_bottom_fill` 负责，
    两处都报会让模型收到重复信息。
    """
    errors, warnings = [], []
    tops, bots = [], []
    for elem in root.iter():
        tag = _local(elem.tag)
        if _is_credit(elem):
            continue
        if tag in ("rect", "image"):
            if _is_full_bg(elem):
                continue
            w, h = _fnum(elem, "width"), _fnum(elem, "height")
            if h <= 0 or w < DECOR_MIN_W:      # 装饰色条不计
                continue
            y = _fnum(elem, "y")
            tops.append(y)
            bots.append(y + h)
        elif tag == "text":
            txt = "".join(elem.itertext()).strip()
            if not txt:
                continue
            y = _fnum(elem, "y")
            fs = _fnum(elem, "font-size", 18.67)
            tops.append(y - 0.75 * fs)
            bots.append(y + 0.25 * fs)
    if not tops:
        return errors, warnings

    top, bot = min(tops), max(bots)
    floor = BOTTOM_TARGET - BOTTOM_TOLERANCE          # 608
    if bot < floor:
        return errors, warnings                       # 底边不达标 → 交给 check_bottom_fill
    span = bot - top
    if span >= SPAN_MIN:
        return errors, warnings
    need = SPAN_MIN - span
    msg = (
        f"纵向跨度不足：内容顶边 y={top:.0f}、底边 y={bot:.0f}，"
        f"跨度 {span:.0f} < {SPAN_MIN}（契约 §5.0.1 铁律）"
        f" → 这是「整体下移」的典型症状：底边凑到了，但留白被搬到了顶部，"
        f"翻页时顶部会跳动。正确做法是让内容真的占满：从 y=104 起排，"
        f"并按 §5.0.1 处理（⚠️ 单块列直接换骨架；多块列走撑块 → 缩块+加异质新组件），"
        f"还差 {need:.0f}px"
    )
    (errors if span < SPAN_HARD else warnings).append(msg)
    return errors, warnings


def check_images(root):
    errors = []
    for elem in root.iter():
        if _local(elem.tag) != "image":
            continue
        href = elem.get("href") or elem.get(XLINK_HREF)
        if not href or href.startswith("data:"):
            continue
        basename = os.path.basename(href)
        if not (SOURCES_DIR / basename).exists():
            errors.append(f"照片不存在: {href}（sources/ 下无 {basename}）")
    return errors


def check_fill(root):
    """检查页面填充率（警告，非硬错误，对应 §1.5）。

    填充率 = 内容元素面积（rect/image 的 w×h + text 的估算面积）÷ 创作区面积。
    有了底部对齐规则（底边统一 648）后，"最底边位置"不再能反映页面充实度，
    改用面积占比衡量。
    """
    warnings = []
    CANVAS_AREA = 1200 * 632  # 创作区 x 40~1240、y 88~720
    content_area = 0.0
    for elem in root.iter():
        if _is_credit(elem):
            continue                      # 来源标注不参与填充率（§1.6）
        tag = _local(elem.tag)
        if tag in ("rect", "image") and _is_full_bg(elem):
            continue                      # 🔴 整页背景不是内容（否则填充率被撑满）
        try:
            if tag in ("rect", "image"):
                content_area += (
                    float(elem.get("width", 0)) * float(elem.get("height", 0))
                )
            elif tag == "text":
                fs = float(elem.get("font-size", 18.67))
                text_len = len("".join(elem.itertext()).strip())
                # 文字面积仍用粗估系数 0.6：这里只是"内容量"的代理指标，
                # 低估文字面积 → 填充率算得偏低 → 判定偏严（不会放过空洞页），方向安全。
                # ⚠️ 与 check_spacing 的 `_text_width()` 精确口径**故意不同**，勿"顺手统一"。
                content_area += fs * text_len * 0.6
        except (ValueError, TypeError):
            continue
    ratio = content_area / CANVAS_AREA
    if ratio < FILL_MIN_RATIO:
        need_area = CANVAS_AREA * FILL_MIN_RATIO - content_area
        warnings.append(
            f"版面偏空：填充率约 {ratio * 100:.0f}% < {FILL_MIN_RATIO * 100:.0f}%"
            f"（内容 {content_area / 10000:.1f} 万 px²，达标需 {CANVAS_AREA * FILL_MIN_RATIO / 10000:.1f} 万）"
            f" → 还差约 {need_area / 10000:.1f} 万 px² ≈ 一张 384×240 的图（9.2 万）"
            "；回 Step 1 按 §5.0.1 补内容 / 缩块+加异质新组件（而非硬填空）"
        )
    return warnings


def check_blank_band(root):
    """连续空白带检查（§2.4 指标③：正文区任一纵向连续空白 ≤140px，且禁止中间断层）。

    ⚠️ 本函数**只抓"中间镂空"**，且**豁免贴顶/贴底的留白** ——
    所以"下半截整片空着"它抓不到，那由 `check_bottom_fill`（指标④）负责。

    留白只能贴顶（y≈88 起）或贴底（到 y≈648），中间出现的纵向大缝判为断层。
    """
    errors = []
    TOP, BOTTOM = 88, 648
    BAND_MAX = 140
    spans = []
    for elem in root.iter():
        tag = _local(elem.tag)
        try:
            if tag in ("rect", "image"):
                if _is_full_bg(elem):
                    continue              # 🔴 整页背景不是内容（否则断层永不被发现）
                y = float(elem.get("y", 0))
                h = float(elem.get("height", 0))
                if h > 0:
                    spans.append((y, y + h))
            elif tag == "text":
                y = float(elem.get("y", 0))
                fs = float(elem.get("font-size", 18.67))
                txt = "".join(elem.itertext()).strip()
                if txt:
                    spans.append((y - 0.75 * fs, y + 0.25 * fs))
            elif tag == "line":
                spans.append((float(elem.get("y1", 0)), float(elem.get("y1", 0)) + 1))
        except (ValueError, TypeError):
            continue
    if not spans:
        return errors
    covered = [False] * (BOTTOM - TOP + 1)
    for s, e in spans:
        s = max(int(s), TOP)
        e = min(int(e), BOTTOM)
        for yy in range(s, e + 1):
            covered[yy - TOP] = True
    # 收集连续空白段
    bands = []
    cur = 0
    start = TOP
    for i, occ in enumerate(covered):
        yy = TOP + i
        if not occ:
            if cur == 0:
                start = yy
            cur += 1
        else:
            if cur > 0:
                bands.append((start, yy - 1, cur))
                cur = 0
    if cur > 0:
        bands.append((start, BOTTOM, cur))
    # 只报"中间断层"：空白带上、下方都还有内容（贴顶/贴底的留白允许）
    for (s, e, h) in bands:
        if h <= BAND_MAX:
            continue
        if s <= TOP or e >= BOTTOM:
            continue
        errors.append(
            f"中间断层：y {s}~{e} 连续空白 {h}px > {BAND_MAX}px"
            f" → 需在这段补 ≥{h - BAND_MAX}px 的内容，或把下方区块整体上移 {h - BAND_MAX}px"
            "（§2.4 指标③：留白只能贴顶/贴底，禁止中间镂空）"
        )
    # 🔴 截断时必须告知总数 —— 否则模型以为"改完列出的就完事"，下一轮又冒出来，白多一轮返工
    total = len(errors)
    if total > 4:
        errors = errors[:4] + [
            f"↑ 断层共 {total} 处，以上仅列前 4 —— 请整页重排，别只补列出的这几处"
        ]
    return errors


def check_bottom_fill(root):
    """纵向收口检查（§2.4 指标④：内容主体底边应收到 `y=648` 附近，避免下半截大面积留白）。

    与 `check_blank_band` 的分工：后者只管**中间**断层（且**豁免贴顶/贴底**的留白），
    本函数专管**贴底那一块** —— 只看内容最低点离 648 有多远。

    🔴 **分级**（2026-09-29 从「纯软警告」升级）：
      · 底边 < `BOTTOM_HARD`(560) → **硬错误，阻断**；
      · 底边 560 ~ 608           → 软警告。

    🔑 **为什么升级**：软警告阶段实测发现**模型会忽略它** —— 平台产物 page04 / page14 被报
    「底部留白」后，模型只把硬错误清零就收工，底边仍停在 **520 / 508**（距底线 88~100px），
    页面明显头重脚轻。而契约 §2.4 开头本就写着「四个指标都要达标」，
    指标④ 实现成纯软警告属**文档与实现不一致**。

    返回 `(errors, warnings)`。
    """

    errors, warnings = [], []
    bottoms = []
    for elem in root.iter():
        tag = _local(elem.tag)
        if _is_credit(elem):
            continue                       # 来源标注固定在 y≈690，不算内容
        try:
            if tag in ("rect", "image"):
                h = float(elem.get("height", 0))
                if h <= 0 or _is_full_bg(elem):
                    continue
                bottoms.append(float(elem.get("y", 0)) + h)
            elif tag == "text":
                if not "".join(elem.itertext()).strip():
                    continue
                y = float(elem.get("y", 0))
                fs = float(elem.get("font-size", 18.67))
                bottoms.append(y + 0.25 * fs)
            elif tag == "line":
                bottoms.append(float(elem.get("y1", 0)))
        except (ValueError, TypeError):
            continue
    if not bottoms:
        return errors, warnings
    bottom = max(bottoms)
    floor = BOTTOM_TARGET - BOTTOM_TOLERANCE          # 608：达标线
    if bottom < BOTTOM_HARD:
        errors.append(
            f"底部严重留白：内容最低止于 y={bottom:.0f}，距底线 {BOTTOM_TARGET} 差 "
            f"{BOTTOM_TARGET - bottom:.0f}px（下限 {BOTTOM_HARD}）"
            f" → 页面下半截明显空着：把最低元素底边延伸到 ≥{floor}"
            f"（还差 {floor - bottom:.0f}px）。处理办法见 §5.0.1"
            f"（先数该列纵向块数：≥2 块可撑块 + 同步补文本；"
            f"只有 1 块时走第 2 步「缩小原块 + 加异质新组件」）"
            "（§2.4 指标④）"
        )
    elif bottom < floor:
        warnings.append(
            f"底部留白偏大：内容最低止于 y={bottom:.0f}，距底线 {BOTTOM_TARGET} 还差 "
            f"{BOTTOM_TARGET - bottom:.0f}px > 上限 {BOTTOM_TOLERANCE}px"
            f" → 把最低元素底边延伸到 ≥{floor}（还差 {floor - bottom:.0f}px）；"
            "处理办法见 §5.0.1（§2.4 指标④）"
        )
    return errors, warnings


def check_block_density(root):
    """块内密度检查（契约**版式册 §5.0.1**）：抓"框撑得很大、里面却是空的"（半空容器）。

    🔑 **为什么需要它**：实测 page05 这类页面**通过了所有既有检查** —— 底边 648（④ 合规）、
    大 rect 占面积（① 填充率达标）、无断层、无重叠 —— 但 4 栏并列**每栏只有 1 块**、
    被撑到 544 高，块内文字只占约 200px（密度 36%），观感就是"通高的空框"。
    既有指标全抓不住：**填充率统计的是面积**，一个被撑大的 rect 直接占满，看不出里面是空的。

    判据：**文字内容跨度 ÷ 容器高 ≥ 50%**（阈值由产物实测标定：反例 page05 = 36%，
    正例 page08 = 69% / page10 = 61%）——
      · 只统计**含文字的容器**；图块 / 纯色块（块内无文字）天然饱满，**跳过**；
      · 同尺寸容器去重（浅底 rect 常与卡片 rect 完全重合）；
      · 文字顶/底口径：顶 = `y − 0.75×font-size`，底 = `y + 0.25×font-size`；
      · 分级：**< 40% → 硬错误 / 40~50% → 软警告**；
        🔴 **但「该列纵向块数 = 1」时一律降为软警告**（见下）。

    🔴 **单块列豁免（2026-09-30）—— 修「密度 × 收口」互斥导致的死循环**：
    块数 = 1 时，为达成收口（§2.4 指标④：底边 ≥608）**只能把这块撑满整列 544**，
    而"框高"正是密度的分母 ⇒ **分母恒定最大、分子不变 ⇒ 密度必然最低**。
    于是模型陷入两难：撑到 544 → 密度 15%；缩到相称 → 跨度 452 < 504。
    **两条判据在块数=1 时数学上互斥**（实测 1 标题 + 2 行正文：跨度 80.7px，
    密度要 50% 则框高须 ≤161，收口 608 则框高须 =544，缺口 382.7px）。
    实测平台产物 page05 / page14 因此**连续 6 轮**在两条报错间来回，无法收敛。

    ⇒ 处置：块数 = 1 时**判据本身不适用**（此时低密度是"素材量不足"的**必然结果**，
    不是"排版失误"）。降为软警告，把真问题交给 **Step 1 的内容量下限判据**
    （`check_design_spec.py` ⑦）在**改数字阶段**暴露 —— 那里返工成本极低。

    返回 `(errors, warnings)`。
    """
    DENSITY_MIN = 0.50
    DENSITY_HARD = 0.40
    MIN_BLOCK_H = 60          # 容器最小高度（排除通栏条 / 图注条 / 色条）
    MIN_BLOCK_W = 150
    errors, warnings = [], []

    def _f(elem, name, default=0.0):
        try:
            return float(elem.get(name, default))
        except (ValueError, TypeError):
            return default

    # ① 容器（rect / image）：排除全屏背景与细小条
    #    🔴 **必须记住"是不是图"** —— 图注条的文字（组件 C4，压在图片下缘）**落在图框范围内**，
    #    若不按"是不是 image"跳过，图块会被算成"文字跨度 19px ÷ 框高 260 = 7% 的半空容器"（实跑踩到）。
    blocks = []
    for elem in root.iter():
        tag = _local(elem.tag)
        if tag not in ("rect", "image"):
            continue
        w, h = _f(elem, "width"), _f(elem, "height")
        if h < MIN_BLOCK_H or w < MIN_BLOCK_W:
            continue
        if _is_full_bg(elem):
            continue
        blocks.append((_f(elem, "x"), _f(elem, "y"), w, h, tag))
    if not blocks:
        return errors, warnings

    # ② 同尺寸去重（浅底 rect 与卡片 rect 常常完全重合，否则会重复报）
    uniq = []
    for b in blocks:
        if any(abs(b[0] - u[0]) <= 4 and abs(b[1] - u[1]) <= 4
               and abs(b[2] - u[2]) <= 4 and abs(b[3] - u[3]) <= 4 for u in uniq):
            continue
        uniq.append(b)

    # ③ 文字（有内容、非来源标注）
    texts = []
    for elem in root.iter():
        if _local(elem.tag) != "text" or _is_credit(elem):
            continue
        txt = "".join(elem.itertext()).strip()
        if not txt:
            continue
        fs = _f(elem, "font-size", 18.67)
        texts.append((_f(elem, "x"), _f(elem, "y"), fs))
    if not texts:
        return errors, warnings

    # ④ 逐容器算密度
    for (bx, by, bw, bh, tag) in uniq:
        if tag == "image":
            continue                       # 图片框天然饱满（图注条文字会污染跨度，必须跳过）
        inner = [t for t in texts if by - 2 <= t[1] <= by + bh + 2
                 and bx - 2 <= t[0] <= bx + bw + 2]
        if not inner:
            continue                       # 图块 / 纯色块：天然饱满
        top = min(t[1] - 0.75 * t[2] for t in inner)
        bot = max(t[1] + 0.25 * t[2] for t in inner)
        span = bot - top
        dens = span / bh if bh else 1.0
        if dens >= DENSITY_MIN:
            continue
        need = DENSITY_MIN * bh - span
        ncol = sum(1 for u in uniq if abs(u[0] - bx) <= 2)
        if ncol >= 2:
            howto = f"该列有 {ncol} 个框 → 可「撑块 + 同步补文本」"
        else:
            # 🔴 块数 = 1：密度低是"素材不足"的必然结果，不是排版失误。
            #    此时撑高是达成收口的唯一途径 ⇒ 密度判据**不适用**，
            #    只能降为软警告，并明确**劝止"再缩块"**（越缩跨度越不足）。
            howto = ("该列只有 1 个框 → ⚠️ 单块撑满整列：低密度是这页素材量不足的必然结果，"
                     "不是排版失误。🚫 别再缩块（缩了跨度必 <504，§5.0.1 铁律）、"
                     "🚫 也别靠撑框凑——两者会互相顶。"
                     "正解是减少本页承载：换更矮的骨架（§5.9 不等宽主次分区 / §5.10 编号条目列实体化）、"
                     "或把内容匀到相邻页")
        msg = (
            f"块内密度不足：框 ({bx:.0f},{by:.0f},{bw:.0f},{bh:.0f}) "
            f"文字跨度 {span:.0f}px ÷ 框高 {bh:.0f} = {dens:.0%} < {DENSITY_MIN:.0%}"
            f" → {'这是「半空的容器」：框撑得很大、里面是空的。' if ncol >= 2 else ''}"
            f"{howto}"
            f"（要达标还需约 {need:.0f}px 内容；§5.0.1）"
        )
        # 🔴 块数 = 1 → 一律软警告（密度判据在此情形不适用，见函数 docstring）
        (errors if (dens < DENSITY_HARD and ncol >= 2) else warnings).append(msg)
    return errors, warnings


def check_image_aspect(root, svg_path=None):
    """图框宽高比 / 裁剪保留率检查（contract_layout.md §6.1）。

    `<image>` 全部用 `preserveAspectRatio="xMidYMid slice"` —— 框比原图宽就裁上下、
    比原图窄就裁左右，**裁掉的部分永久丢失**。框越扁，照片越只剩中间一条。

    两条判据（一硬一软，互补）：

    - 🔴 **硬错误**：框 AR > 2.8 —— 直译 §6.1 硬规则 1。
      这是"通栏图被裁成一条缝"（`1200×240` AR=5.0、`1200×188` AR=6.4）的正解，
      必须在 Step 2 拦住，不能靠人眼在 Step 5 之后才发现。
    - ⚠️ **警告**：能读到原图、且真实保留率 < 55% —— 对应 §6.1 硬规则 4 的「裁剪保留率下限」，
      🔴 **按实拍图 AR 1.33 计**（开关 OFF 的默认路径下素材全是实拍图）。
      🔑 为什么 AR 之外还要这一条：**框 AR 不超标 ≠ 没裁狠**。框 `580×214` 的 AR 只有 2.71
      （过得了硬规则 1），但对 4:3 实拍图实际只保留 **49%** —— 光看 AR 判据完全抓不到。
      （旧版 §6.1 把 55% 挂靠在 `580×214` 上、按网图 1.50 计算，是错的；实拍半栏应用 `580×246`。）

    保留率（面积口径）= `min(框AR, 图AR) / max(框AR, 图AR)`，与 §6.1 的两条公式等价。
    读不到原图（文件缺失/缺 PIL）时退回只用 AR 判据。

    返回 `(errors, warnings)`。
    """
    errors = []
    warnings = []
    MAX_AR = 2.8          # §6.1 硬规则 1：图框 AR 上限
    MIN_KEEP = 0.55       # §6.1 硬规则 4：裁剪保留率下限（🔴 按实拍图 AR 1.33 计）
    src_dir = None
    if svg_path is not None:
        src_dir = Path(svg_path).resolve().parent.parent / "sources"

    for elem in root.iter():
        if _local(elem.tag) != "image":
            continue
        try:
            w = float(elem.get("width", 0))
            h = float(elem.get("height", 0))
        except (ValueError, TypeError):
            continue
        if w <= 0 or h <= 0:
            continue
        ar = w / h
        href = elem.get("href") or elem.get(XLINK_HREF) or ""
        name = href.rsplit("/", 1)[-1]

        # 读原图尺寸 → 精确保留率（读不到就退回只用 AR 判据）
        kept = None
        ar_img = None
        size = ""
        if src_dir is not None and name:
            try:
                from PIL import Image
                iw, ih = Image.open(src_dir / name).size
                ar_img = iw / ih
                kept = min(ar, ar_img) / max(ar, ar_img)
                size = f"{iw}×{ih}"
            except Exception:
                kept = None

        keep_txt = (
            f"，对 {size} 原图仅保留 {kept * 100:.0f}% 画面"
            if kept is not None else ""
        )

        if ar > MAX_AR:
            errors.append(
                f"图框过扁：{name or 'image'} 框 {w:.0f}×{h:.0f}（AR={ar:.2f} > {MAX_AR}）"
                f"{keep_txt}（契约 §6.1 硬规则 1）"
                f" → 把图高从 {h:.0f} 提到 ≥{w / MAX_AR:.0f}（宽 {w:.0f} 不变），"
                "或改用 2~3 张图横向并排（每张 384 宽）"
            )
        elif kept is not None and kept < MIN_KEEP:
            fix = (f"把框高从 {h:.0f} 改到 ≈{w / ar_img:.0f}（宽 {w:.0f} 不变）可接近全保留；"
                   if ar_img else "")
            warnings.append(
                f"裁剪偏重：{name} 框 {w:.0f}×{h:.0f}（AR={ar:.2f}）对 {size} 原图"
                f"仅保留 {kept * 100:.0f}% < {MIN_KEEP * 100:.0f}%（契约 §6.1 硬规则 4）"
                f" → {fix}或换一张横幅比例的照片"
            )

    # 🔴 截断时告知总数（同 check_spacing）
    n_err, n_warn = len(errors), len(warnings)
    if n_err > 8:
        errors = errors[:8] + [
            f"↑ 图框违规共 {n_err} 处，以上仅列前 8 —— 请把所有图片的框都过一遍"
        ]
    if n_warn > 4:
        warnings = warnings[:4] + [
            f"（裁剪偏重提示共 {n_warn} 条，以上仅列前 4）"
        ]
    return errors, warnings


def check_content_amount(root):
    """内容量检查（§2.4 指标②：正文字数下限 ≥140 + 单页图占比 ≤40%）。

    净字数 = 非白色文字（排除图注条白字 / 数据块白字）的字符数。
    图占比 = 图片总面积 ÷ 创作区面积（1200×560，y 88~648）。
    """
    warnings = []
    MIN_WORDS = 140
    MAX_IMG_RATIO = 0.40
    CANVAS = 1200 * 560
    net_words = 0
    img_area = 0.0
    for elem in root.iter():
        if _is_credit(elem):
            continue                      # 来源标注不计入净字数（§1.6）
        tag = _local(elem.tag)
        try:
            if tag == "text":
                fill = (elem.get("fill") or "").strip().upper()
                if fill != "#FFFFFF":
                    net_words += len("".join(elem.itertext()).strip())
            elif tag == "image":
                img_area += float(elem.get("width", 0)) * float(elem.get("height", 0))
        except (ValueError, TypeError):
            continue
    ratio = img_area / CANVAS
    if net_words < MIN_WORDS:
        warnings.append(
            f"正文偏少：净字数 {net_words} < {MIN_WORDS}"
            f" → 还差 {MIN_WORDS - net_words} 字（§2.4 指标②，图注白字不计入）"
        )
    if ratio > MAX_IMG_RATIO and net_words < MIN_WORDS:
        warnings.append(
            f"图多字少：图占 {ratio * 100:.0f}% > {MAX_IMG_RATIO * 100:.0f}% "
            f"且净字数 {net_words} < {MIN_WORDS}"
            f" → 图面积需降到 ≤{CANVAS * MAX_IMG_RATIO / 10000:.1f} 万 px²（现 {img_area / 10000:.1f} 万），"
            f"或正文补足到 {MIN_WORDS} 字（还差 {MIN_WORDS - net_words} 字）"
            "（§2.4 指标②，图片不是文字的替身）"
        )
    return warnings


def check_spacing(root):
    """区块间距检查（creative_contract.md §2.2 区块间距铁律）。

    只检测**垂直方向的重叠**——这是"元素贴在一起"最严重的形式，也是最容易
    出错的地方：组件示例给的固定坐标（组件 C4 图注 y=616、组件 C5 流程链 y=632）
    在"上方还有内容"时直接照抄，就会与上方区块重叠。

    做法：把每个元素算成 [顶边, 底边] 区间，取每个 x 带的元素按 y 排序，
    若相邻两元素区间重叠（且不属于同一逻辑组），报错。

    🔴 **报错分两类，因为修复方向相反**（判定见 `_is_text_edge`）：
      · 「文字越界」→ 改**文字**（缩短 / 拆行），区块位置不用动；
      · 「位置冲突」→ 动**元素**（下移 / 压高度）。
    实测教训：原先两类混报成一句"元素重叠 … 组件示例的固定 y 坐标需整体平移"，
    而 page08 的病因其实是左栏文字越过中线 —— 模型照引导语去移元素，白费 2:42。
    """
    errors = []
    # 取每个元素的可视区间：(x1, x2, top, bottom, 标签)
    items = []
    for elem in root.iter():
        tag = _local(elem.tag)
        try:
            if tag in ("rect", "image"):
                x = float(elem.get("x", 0))
                y = float(elem.get("y", 0))
                w = float(elem.get("width", 0))
                h = float(elem.get("height", 0))
                if w <= 0 or h <= 0:
                    continue
                items.append((x, x + w, y, y + h, f"{tag}({x:.0f},{y:.0f})", tag, w))
            elif tag == "text":
                x = float(elem.get("x", 0))
                y = float(elem.get("y", 0))
                fs = float(elem.get("font-size", 18.67))
                txt = "".join(elem.itertext()).strip()
                if not txt:
                    continue
                # 基线 y → 字形区间（近似）：顶部 y-0.75fs，底部 y+0.25fs
                # 🔴 水平区间必须按 text-anchor 展开：middle 以 x 为中心、
                # end 以 x 为右边缘。若一律当左对齐，居中的长文本会被误判
                # 为"越出容器右边界"→ 假报重叠。
                tw = _text_width(txt, fs)
                anchor = (elem.get("text-anchor") or "start").strip().lower()
                if anchor == "middle":
                    tx1, tx2 = x - tw / 2, x + tw / 2
                elif anchor == "end":
                    tx1, tx2 = x - tw, x
                else:
                    tx1, tx2 = x, x + tw
                items.append(
                    (tx1, tx2, y - 0.75 * fs, y + 0.25 * fs,
                     f'text({x:.0f},{y:.0f})"{txt[:8]}"', "text", tx2 - tx1)
                )
            elif tag == "line":
                y1 = float(elem.get("y1", 0))
                x1 = float(elem.get("x1", 0))
                x2 = float(elem.get("x2", 0))
                items.append((x1, x2, y1, y1 + 1, f"line({x1:.0f},{y1:.0f})", "line", x2 - x1))
        except (ValueError, TypeError):
            continue

    # 按 x 方向重叠分组：仅比较"水平投影有交集"的元素
    for i, a in enumerate(items):
        ax1, ax2, atop, abot, alab = a[:5]
        for b in items[i + 1:]:
            bx1, bx2, btop, bbot, blab = b[:5]
            # 水平无交集 → 不比较
            if ax2 <= bx1 or bx2 <= ax1:
                continue
            # 垂直重叠量
            overlap = min(abot, bbot) - max(atop, btop)
            if overlap <= 2:
                continue
            # 排除同一逻辑组内部的正常层叠：
            # ① 文字落在色块/图片上（图注条上的白字、卡片上的文字，属正常设计）
            if _contained(a, b) or _contained(b, a):
                continue
            ix = min(ax2, bx2) - max(ax1, bx1)      # 水平交集宽度
            # ── 分流：两种病因的**修复方向完全相反**，必须说清是哪一种 ──
            if _is_text_edge(a, b, ix):
                # 文字越界：文字伸出了容器 / 尾巴扫到邻栏 → 该改**文字**，别动区块
                t_it, o_it = (a, b) if a[5] == "text" else (b, a)
                t_lab, t_w, other = t_it[4], t_it[6], o_it[4]
                # 🔑 附「可执行修复值」：容器可用宽 ≈N 个汉字 + 还需删 K 个字。
                # 只报"像素超了多少"时，模型得自己拿 16 进制去猜"该删几个字"——
                # 实测 page12 表格为此白返工 3 轮（2026-09-30）。
                hint = ""
                if o_it[5] in ("rect", "image"):
                    tx1, tx2, cx1, cx2 = t_it[0], t_it[1], o_it[0], o_it[1]
                    if tx1 >= cx1 - 1e-6:             # 从容器内往右伸
                        avail, over = cx2 - tx1, tx2 - cx2
                    elif tx2 <= cx2 + 1e-6:           # 往左伸
                        avail, over = tx2 - cx1, cx1 - tx1
                    else:                             # 比容器还宽：两边都伸出去
                        avail, over = cx2 - cx1, max(tx1 - cx1, tx2 - cx2)
                    if over > 0:
                        hint = (f"｜容器可用宽 {avail:.0f}px ≈ {int(avail / CH_W)} 个汉字，"
                                f"该行文字宽 {t_w:.0f}px、超出 {over:.0f}px"
                                f" → 再删约 {int(over / CH_W) + 1} 个字（或拆成两行）")
                if hint:
                    errors.append(
                        f"元素重叠（文字越界）：{t_lab} 与 {other} 重叠 {ix:.0f}px{hint}"
                        f" → 只改文字，区块位置不用动（§2.2）"
                    )
                else:
                    errors.append(
                        f"元素重叠（文字越界）：{t_lab} 与 {other} 重叠 {ix:.0f}px"
                        f"（该行文字宽 {t_w:.0f}px）"
                        f" → 缩短这行文字 / 拆成两行即可，区块位置不用动（§2.2 间距铁律）"
                    )
            else:
                # 位置冲突：实体元素真的叠了 → 该动**元素**（下移 / 压高度）
                up, dn = (a, b) if atop <= btop else (b, a)
                up_h = up[3] - up[2]                    # ⚠️ 元组里 [3] 是 bottom，高度要减出来
                gap = dn[2] - up[3]                     # 下元素顶 − 上元素底（重叠时为负）
                need = MIN_GAP - gap                    # 需腾出的净空间
                if up[5] == "text" and dn[5] == "text":
                    # 两行文字相撞 = 行距不足，别建议"压缩高度"
                    opts = [f"把下面这行文字下移 {need:.0f}px（拉开两行间距）"]
                else:
                    opts = []
                    if dn[3] + need <= BOTTOM_TARGET:
                        opts.append(f"下方元素整体下移 {need:.0f}px（到 y≥{dn[2] + need:.0f}）")
                    if up_h - need >= 40:
                        opts.append(f"上方元素高度减 {need:.0f}px（{up_h:.0f}→{up_h - need:.0f}）")
                    if not opts:
                        opts.append(f"两条微调路都走不通（下移会越过底线 {BOTTOM_TARGET}、"
                                    "压缩后过矮）→ 建议换更矮的组件或改版式")
                errors.append(
                    f"元素重叠（位置冲突）：{up[4]} 与 {dn[4]} 垂直重叠 {overlap:.0f}px，"
                    f"净间距 {gap:.0f} < {MIN_GAP}（§2.2 间距铁律）"
                    " → " + "，或 ".join(opts)
                )
    # 🔴 上限由 6 提到 12（重叠是最常见的返工源，报全比让模型自己找更省一轮），
    #    且**超出时必须告知总数** —— 原先静默截断，模型以为"改完列出的就完事"，
    #    下一轮又冒出剩下的，白多一轮模型推理（实测每轮 ≈55 秒）。
    total = len(errors)
    if total > 12:
        errors = errors[:12] + [
            f"↑ 元素重叠共 {total} 处，以上仅列前 12 —— 请一次性排查全页同类重叠，"
            "不要只改列出的这几条（否则下一轮会把剩下的再报一遍）"
        ]
    return errors


def _is_text_edge(a, b, ix):
    """这次碰撞是否属于「文字越界」（文字伸出容器 / 尾巴扫到邻栏元素）。

    区分它与「位置冲突」很重要 —— **两者的修复方向相反**：
      · 文字越界 → 改**文字**（缩短 / 拆行），移动区块白费功夫；
      · 位置冲突 → 动**元素**（下移 / 压高度）。

    判据：
      ① **text × 容器（rect / image）** → 必判越界。
         因为"文字完整落在卡片内"这种**正常层叠**已被 `_contained()` 提前排除，
         能走到这里的，就是文字**伸出了容器边界**（或从外面伸进来）。
      ② **text × text** → 交集既小于**较短那一行**的 50%、又 < 80px 才算越界；
         否则是行距不足造成的整段相撞，应判「位置冲突」。
      ③ **不含 text**（rect / image / line 之间）→ 一律「位置冲突」。

    宁漏不误 —— 指错方向比少给提示更糟。
    """
    if a[5] == "text":
        if b[5] in ("rect", "image"):
            return True
        if b[5] == "text":
            shorter = min(a[6], b[6])
            return shorter > 0 and ix < shorter * TEXT_EDGE_RATIO and ix < TEXT_EDGE_MAX_PX
    elif b[5] == "text" and a[5] in ("rect", "image"):
        return True
    return False


def _contained(a, b):
    """a 是否水平+垂直都基本被 b 包住（判定"文字压在色块/图片上"）。"""
    ax1, ax2, atop, abot = a[:4]
    bx1, bx2, btop, bbot = b[:4]
    h_inside = ax1 >= bx1 - 2 and ax2 <= bx2 + 2
    v_inside = atop >= btop - 2 and abot <= bbot + 2
    return h_inside and v_inside


def check_column_balance(root):
    """左右分栏「高度对齐」检查（creative_contract.md §2.2b）。

    判定思路：把**容器元素**按水平位置分成"左栏 / 右栏 / 通栏"三类，
    比较左右两栏的**视觉内容底边**。差值 > 阈值就提示（warning，不阻断）。

    - 左栏：中心 < 640；右栏：中心 >= 640；通栏：宽度 >= 1100（如底部总结条、通栏表格）
    - 🔴 **只统计 rect / image，不统计 text**：text 的 x 是"文字起点"，不代表它占据哪半边。
      通栏表格的第 3 列（左对齐 x=660）会被误判成"右栏内容"，报出假的分栏失衡
      —— 表格页（§5.4）曾因此 100% 误报。
    - 🔴 **宽度 < 20px 的装饰条不计入**（组件 C1/C3 的 5px 左色条）：色条与卡片**同高**，
      它的底边会把所在栏的底边拉到卡片底部，导致"左栏底 648 / 右栏底 384"这种**假失衡**
      （实测 §5.7 两图 + 通栏卡的页面 100% 误报）。
    - 🔴 **多栏结构直接跳过**：若存在"跨中线"的容器、且其左右两侧都还有其他容器
      （三栏并列 / 卡片矩阵这类布局），则本页不是二分栏页面，§2.2b 不适用。
      注意必须三个条件同时成立 —— 只看"跨中线"会误跳过 §5.9 不等宽分区
      （它的左区宽 800、本身就跨中线，但确实是需要对齐的左右结构）。
    - 忽略来源标注（§1.6）。
    """
    warnings = []
    GAP_LIMIT = 48.0      # 底边差超过这个值就提示（§2.2b 允许 ±24，留一倍余量）
    SPLIT_X = 640.0       # 左右分界
    FULL_MIN_W = 1100.0   # 超过此宽度视为通栏
    # DECOR_MIN_W 已提升为模块级常量（`check_content_span` 也要用）
    SPAN_MARGIN = 60.0    # 判定"跨越中线"时，两端各需超出中线这么多

    # ── 收集容器元素 ──
    boxes = []            # (x1, x2, bottom)
    for elem in root.iter():
        if _is_credit(elem):
            continue
        if _local(elem.tag) not in ("rect", "image"):
            continue
        try:
            x = float(elem.get("x", 0))
            w = float(elem.get("width", 0))
            y = float(elem.get("y", 0))
            h = float(elem.get("height", 0))
        except (ValueError, TypeError):
            continue
        if w <= 0 or h <= 0:
            continue
        if w < DECOR_MIN_W:              # 装饰色条（5px），不代表栏内内容
            continue
        if w >= FULL_MIN_W:              # 通栏条，不算入左右栏
            continue
        boxes.append((x, x + w, y + h))

    if not boxes:
        return warnings

    # ── 多栏结构判据：跨中线 + 左右两侧都还有容器 ──
    left_only = right_only = spanning = False
    for x1, x2, _ in boxes:
        if x2 <= SPLIT_X - SPAN_MARGIN:
            left_only = True
        elif x1 >= SPLIT_X + SPAN_MARGIN:
            right_only = True
        elif x1 < SPLIT_X - SPAN_MARGIN and x2 > SPLIT_X + SPAN_MARGIN:
            spanning = True
    if spanning and left_only and right_only:
        return warnings                  # 三栏 / 多栏布局，§2.2b 不适用

    # ── 按中心点分左右，取各自最大底边 ──
    left_bottom = right_bottom = 0.0
    left_cnt = right_cnt = 0
    for x1, x2, bottom in boxes:
        if (x1 + x2) / 2 < SPLIT_X:
            left_bottom = max(left_bottom, bottom); left_cnt += 1
        else:
            right_bottom = max(right_bottom, bottom); right_cnt += 1

    # 两侧都至少有一个实心内容元素才比较（避免"单栏页"误报）。
    if left_cnt >= 1 and right_cnt >= 1:
        gap = abs(right_bottom - left_bottom)
        if gap > GAP_LIMIT:
            shorter = "左栏" if left_bottom < right_bottom else "右栏"
            low, high = min(left_bottom, right_bottom), max(left_bottom, right_bottom)
            need = gap - MIN_GAP
            warnings.append(
                f"左右分栏高度失衡：左栏底 {left_bottom:.0f} / 右栏底 {right_bottom:.0f}，"
                f"相差 {gap:.0f}px（{shorter}偏短）—— §2.2b 要求两栏底边误差 ≤{MIN_GAP}px"
                f" → {shorter}加高 {need:.0f}px 收口到 {high:.0f} 附近，"
                f"或长的一侧缩到 {low + MIN_GAP:.0f} 以下；"
                "按 §5.0.1 的「分栏分支」处理：高栏合规时调矮栏 —— 加高偏短的一侧，"
                "或用分栏独有的「反向缩短」压短长的一侧；高栏本身也不合规时两栏先降齐，"
                "再走主流程补高度。"
            )
    return warnings


RENDER_TAGS = {"text", "rect", "image", "line", "circle", "ellipse",
               "path", "polygon", "polyline", "use"}


def check_root_wrapped(root):
    """🔴 正文片段的渲染元素必须全部包在根 `<g>` 内（不能裸挂在顶层）。

    契约 §3 第 8 条要求 `font-family` **只在根 `<g>` 上声明一次**，所以脱离 `<g>` 的
    顶层元素**继承不到字体**，导出后会被引擎填成默认西文字体
    —— 实测会产生 `latin=Segoe UI / ea=Microsoft YaHei`（微软雅黑），违反 §1.1「中文必须黑体」。
    """
    errors = []
    for elem in list(root):
        tag = _local(elem.tag)
        if tag in RENDER_TAGS:
            errors.append(
                f"顶层 <{tag}> 脱离根 <g>：继承不到 font-family，导出后会被填成默认字体"
                "（违反 §1.1 中文必须黑体）。请把它移进根 <g> 内部。"
            )
    return errors


# ── 图注底条（组件 C4，2026-10-19 新增）────────────────────────────────
def check_caption_bar(root):
    """压在照片上的白字必须有半透明底条。

    🔴 2026-10-19 实测（动力电池回收 page04）：三张图的图注**全画在图片上缘**
    （C4 定义是"压在照片下缘"），且**一张都没画 `#013B94` 底条** —— 白字直接压在
    蓝色示意图上，第一张还叠在图片自带的标题「退役锂电池回收产业链」上，两层字打架。
    当时判据完全不查图注，所以零报错、直接出片。

    判定范围（故意收窄，避免误伤）：
      · 只查 **`<image>` 宽>200 且高>100** 的大图（排除 logo / 小图标）；
      · 只查**落在图片 bbox 内的白色 text**（= 真的压在图上）；
      · 图外挂图注**不判**（契约允许「图注放图外」，只是不够紧凑）。
    覆盖判据：文字 bbox 有 ≥60% 面积落在某个"半透明条"里即算有底条。
    半透明条 = `rect` 且 `fill-opacity ≥ 0.5` 且 高 ≤ 80 且 宽 ≥ 图宽×0.5
    （不锁死 `#013B94` —— 配色替换后它会变成各方案的 ROLE_DEEP，锁色会误伤 minimal/vibrant）。
    """
    warnings = []
    images = []
    for elem in root.iter():
        if _local(elem.tag) != "image":
            continue
        x, w = _fnum(elem, "x"), _fnum(elem, "width")
        y, h = _fnum(elem, "y"), _fnum(elem, "height")
        if w < 200 or h < 100:
            continue
        images.append((x, y, w, h))
    if not images:
        return warnings

    bars = []
    for elem in root.iter():
        if _local(elem.tag) != "rect":
            continue
        op = elem.get("fill-opacity")
        if op is None:
            continue
        try:
            opv = float(op)
        except (TypeError, ValueError):
            continue
        x, w = _fnum(elem, "x"), _fnum(elem, "width")
        y, h = _fnum(elem, "y"), _fnum(elem, "height")
        if opv >= 0.5 and 0 < h <= 80 and w >= 200:
            bars.append((x, y, w, h))

    def _cover_ratio(tx1, tx2, ty1, ty2, bx, by, bw, bh):
        ix = max(0.0, min(tx2, bx + bw) - max(tx1, bx))
        iy = max(0.0, min(ty2, by + bh) - max(ty1, by))
        area = max(1e-6, (tx2 - tx1) * (ty2 - ty1))
        return ix * iy / area

    for elem in root.iter():
        if _local(elem.tag) != "text":
            continue
        fill = (elem.get("fill") or "").strip().upper()
        if fill not in ("#FFFFFF", "#FFF"):
            continue
        txt = "".join(elem.itertext()).strip()
        if not txt or _is_credit(elem):
            continue
        fs = _fnum(elem, "font-size", 18.67)
        x, y = _fnum(elem, "x"), _fnum(elem, "y")
        tw = _text_width(txt, fs)
        anchor = (elem.get("text-anchor") or "start").strip().lower()
        if anchor == "middle":
            tx1, tx2 = x - tw / 2, x + tw / 2
        elif anchor == "end":
            tx1, tx2 = x - tw, x
        else:
            tx1, tx2 = x, x + tw
        ty1, ty2 = y - 0.75 * fs, y + 0.25 * fs

        for (ix, iy, iw, ih) in images:
            if not (tx2 > ix and tx1 < ix + iw and ty2 > iy and ty1 < iy + ih):
                continue
            best = 0.0
            for (bx, by, bw, bh) in bars:
                best = max(best, _cover_ratio(tx1, tx2, ty1, ty2, bx, by, bw, bh))
            if best < 0.6:
                warnings.append(
                    f'图注「{txt[:10]}」是白字，且压在图片 ({ix:.0f},{iy:.0f},'
                    f'{iw:.0f}×{ih:.0f}) 内 —— 但**没有半透明底条**兜底。'
                    f'②册 C4：图注必须「深色底条 + 白字」，否则白字压浅色照片会糊成一片；'
                    f'且图注应压在**图片下缘**，不要放上缘'
                )
            break
    return warnings


# ── R2 环形占比：环不得压卡（2026-10-19 新增）──────────────────────────
def check_ring_overlap(root):
    """组件 R2 的环不得与卡片（大 rect）在几何上重叠。

    🔴 2026-10-19 实测（动力电池回收 page05）：环 `cx=335 r=88`（外径 ~200），
    压在左侧卡片（`x=40~420`）上，**盖掉三行正文的右半**（"电池""缺失""漏洞"全被切）。
    当时判据只查"元素间距"，把环当普通图形放过 —— circle 不在它的统计口径里。

    识别 R2 的环：`circle` 且 `r ≥ 40`、`stroke-width ≥ 12`、`fill` 为 none/空。
    （排除 R5 的浅色圆底：它有实心 fill 且无粗描边；也排除 C10 的序号圆：`r≈14`。）
    重叠超过**环 bbox 面积 15%** 才报 —— 环贴着卡边但没压字的情况不打扰。
    """
    errors = []
    rings = []
    for elem in root.iter():
        if _local(elem.tag) != "circle":
            continue
        r = _fnum(elem, "r")
        sw = _fnum(elem, "stroke-width")
        fill = (elem.get("fill") or "").strip().lower()
        if r < 40 or sw < 12 or fill not in ("none", "transparent", ""):
            continue
        rings.append((_fnum(elem, "cx") - r - sw / 2, _fnum(elem, "cy") - r - sw / 2,
                      (r + sw / 2) * 2, (r + sw / 2) * 2, r))
    if not rings:
        return errors

    # 🔑 只把**内部有文字**的 rect 当"卡片"：
    #   C1 卡一定带文字；而"环形放在浅底容器里"是合法画法（那是背景、不是卡）。
    #   用「文字锚点是否落在框内」过滤 —— 比按 fill-opacity 判可靠（C1 的浅底就是 0.03~0.06）。
    anchors = []
    for elem in root.iter():
        if _local(elem.tag) != "text":
            continue
        if not "".join(elem.itertext()).strip() or _is_credit(elem):
            continue
        anchors.append((_fnum(elem, "x"), _fnum(elem, "y")))

    cards = []
    for elem in root.iter():
        if _local(elem.tag) != "rect":
            continue
        w, h = _fnum(elem, "width"), _fnum(elem, "height")
        if w <= 100 or h <= 100:
            continue
        cx0, cy0 = _fnum(elem, "x"), _fnum(elem, "y")
        if not any(cx0 < ax < cx0 + w and cy0 < ay < cy0 + h for (ax, ay) in anchors):
            continue
        cards.append((cx0, cy0, w, h))

    # 🔴 R2 是**同心双环**（底环 + 进度环），同一张卡会被命中两遍 ⇒ 按卡去重
    _seen = set()
    for (rx, ry, rw, rh, r) in rings:
        for (cx, cy, cw, ch) in cards:
            ix = max(0.0, min(rx + rw, cx + cw) - max(rx, cx))
            iy = max(0.0, min(ry + rh, cy + ch) - max(ry, cy))
            if ix * iy / (rw * rh) <= 0.15:
                continue
            if (cx, cy, cw, ch) in _seen:
                continue
            _seen.add((cx, cy, cw, ch))
            errors.append(
                f'环形占比（r={r:g}）的 bbox ({rx:.0f},{ry:.0f},{rw:.0f}×{rh:.0f}) '
                f'与卡片 rect ({cx:.0f},{cy:.0f},{cw:.0f}×{ch:.0f}) 重叠 —— '
                f'**环压到卡片上了，会盖住卡里的文字**。②册 R2 要求「环占半区、卡占另半区」：'
                f'左区 `x 40~620`、右区 `x 660~1240`，环与卡的 bbox **不得相交**'
            )
    return errors


# ── C1 框 D（L形框）：顶横线必须画到卡的右边缘（2026-10-19 新增）──────
def check_frame_d_path(root):
    """框 D 的顶横线长度必须 ≈ 卡宽。

    🔴 2026-10-19 实测（动力电池回收 page09）：六张卡的框 D 顶线只画到 `x+60`
    （卡宽 310），断在半空；而**同一份稿的 page04 是画对的**（顶线通宽）。
    60 ≈ 组件 B 的顶部标头 `56×6` ⇒ 高置信度是「把 D 的顶线长度记成了 B 的标头宽度」。

    识别框 D：`path` 恰好 3 个点、第一段**竖直**（Δx≈0、|Δy|>20）、
    第二段**水平**（Δy≈0）。角标（10×10）第二段太短，跳过（交给 ④-i）。
    比对对象：左边缘与 `x0` 对齐（±6）的大 rect（宽>200）。
    """
    warnings = []
    cards = []
    for elem in root.iter():
        if _local(elem.tag) != "rect":
            continue
        w, h = _fnum(elem, "width"), _fnum(elem, "height")
        if w > 200 and h > 80:
            cards.append((_fnum(elem, "x"), _fnum(elem, "y"), w, h))
    if not cards:
        return warnings

    for elem in root.iter():
        if _local(elem.tag) != "path":
            continue
        d = (elem.get("d") or "").strip()
        pts = [(float(a), float(b)) for _c, a, b in
               re.findall(r"([ML])\s*(-?\d+\.?\d*)[ ,]\s*(-?\d+\.?\d*)", d)]
        if len(pts) != 3:
            continue
        (x0, y0), (x1, y1), (x2, y2) = pts
        if not (abs(x1 - x0) < 1.0 and abs(y1 - y0) > 20):
            continue
        if abs(y2 - y1) > 1.0:
            continue
        seg = abs(x2 - x1)
        if seg < 20:          # 角标尺寸，交给 ④-i
            continue
        for (cx, cy, cw, ch) in cards:
            if abs(cx - min(x0, x1)) > 6:
                continue
            if not (min(y0, y1) >= cy - 6 and max(y0, y1) <= cy + ch + 6):
                continue
            if seg < cw * 0.6:
                warnings.append(
                    f'框 D 的顶横线只画到 `x{min(x0, x1) + seg:.0f}`（长度 {seg:.0f}），'
                    f'而卡片宽 {cw:.0f} —— **顶线必须一路画到卡的右边缘**（`x+w`）。'
                    f'常见笔误：把 D 的顶线长度写成了组件 B 的标头宽度 `56`。'
                    f'原文：`{d}`'
                )
            break
    return warnings



# ── 底部保留区（2026-10-20 新增）──────────────────────────────────────
CONTENT_FLOOR_MAX = 650.0   # 除来源标注外，任何内容的视觉底边不得越过此线


def check_bottom_intrusion(root):
    """内容不得侵入底部保留区（契约 §1.6：648 以下只允许来源标注）。

    🔴 2026-10-19 实测（LNG 接收站 page08）：图片铺到 `y=616`，模型把 G3 脚注
    「紧跟相关区块下方」放在 `线 640 / 文字基线 668` —— 而来源标注第一行也在 668，
    **两者基线完全相同、叠成一团**。当时判据只查 `y<88`（盖页头），
    **没有任何一条查"越过 648"**，所以零报错直接出片。

    阈值取 **650**（不是 648）：
      · 来源第一行文本框上缘 652.1、字形上缘 654；
      · 内容收到 648 时字底 652.7，只剩 1.3px —— 视觉上已经贴死；
      · 650 给"不重叠"留 2px 缓冲，同时不误伤「图注条压在图片下缘」的常规画法
        （实测 page05 图注字底 642.9、page07 卡片底 648，都在线内）。

    只查 `text` 与 `rect`：`image` 可以是满版底图，且 C4 图注条本就压在图上。
    """
    # 🔴 阈值统一 650，**不分有没有图**。
    #   一度想过"无图的页放到 676"（只有内容来源 690、间距够），被实测否掉：
    #   page11 无照片、脚注在 668，确实没与 690 那行重叠 —— 但**它压在递增柱的底边（648）上**，
    #   整页下半截挤成一坨。根因不是"撞没撞"，是 **G3 本该在 592，它跑到了 652**。
    #   ⇒ 判据只管一条固定红线，别为个例开小口子。
    floor = CONTENT_FLOOR_MAX

    errors = []
    for elem in root.iter():
        tag = _local(elem.tag)
        if tag not in ("text", "rect"):
            continue
        if _is_credit(elem) or _is_full_bg(elem):
            continue
        try:
            if tag == "text":
                txt = "".join(elem.itertext()).strip()
                if not txt:
                    continue
                fs = _fnum(elem, "font-size", 18.67)
                bottom = _fnum(elem, "y") + 0.25 * fs
                label = f'text({_fnum(elem, "y"):.0f})"{txt[:12]}"'
            else:
                h = _fnum(elem, "height")
                if h < 2:          # 1px 分隔线 / 色条，不算内容
                    continue
                bottom = _fnum(elem, "y") + h
                label = f'rect({_fnum(elem, "y"):.0f},h={h:.0f})'
        except (TypeError, ValueError):
            continue
        if bottom > floor:
            errors.append(
                f'{label} 的底边到 {bottom:.0f}，越过了 **{floor:.0f}** —— '
                f'`y=648` 以下（648~720）是**来源标注专属区**（契约 §1.6），正文/脚注/卡片都不许进。'
                f'来源第一行字形上缘在 654，越线就会**与它同基线叠字**（实测 page08 叠成一团）。'
                f'处理：把这块内容整体上移，收在 **≤648**（有两条来源时建议 ≤620）；'
                f'真放不下就换组件或删掉这条注释，别往下挤'
            )
    return errors



def check_body(file_path):
    body_text = file_path.read_text(encoding="utf-8")
    errors = []
    warnings = []

    errors.extend(check_no_svg_root(body_text))
    errors.extend(check_placeholders(body_text))
    errors.extend(check_nonempty(body_text))

    # 包一层根 <svg> 供解析 + canonical 检查
    # 根字体用 "sans-serif" 模拟 009 头部的根 font-family，让正文 g 上的
    # "黑体, Arial, sans-serif" 成为有效覆盖（而非被误判为冗余继承）
    wrapped = (
        f'<svg xmlns="{SVG_NS}" font-family="sans-serif">'
        f"{body_text}</svg>"
    )
    try:
        root = ET.fromstring(wrapped)
    except ET.ParseError as e:
        errors.append(f"XML 解析失败: {e}")
        return errors, warnings  # 解析失败，后续检查无意义

    errors.extend(check_root_wrapped(root))
    errors.extend(canonical_authoring_errors(root))
    errors.extend(check_fonts(root))
    errors.extend(check_font_sizes(root))
    errors.extend(check_font_weights(root))
    errors.extend(check_colors(root))
    errors.extend(check_icons(root))
    errors.extend(check_bounds(root))
    errors.extend(check_images(root))
    _aspect_errors, _aspect_warnings = check_image_aspect(root, file_path)
    errors.extend(_aspect_errors)
    errors.extend(check_spacing(root))
    errors.extend(check_blank_band(root))
    warnings.extend(check_fill(root))
    warnings.extend(check_content_amount(root))
    warnings.extend(_aspect_warnings)
    warnings.extend(check_column_balance(root))
    _bf_errors, _bf_warnings = check_bottom_fill(root)
    errors.extend(_bf_errors)
    warnings.extend(_bf_warnings)
    _bd_errors, _bd_warnings = check_block_density(root)
    errors.extend(_bd_errors)
    warnings.extend(_bd_warnings)
    _sp_errors, _sp_warnings = check_content_span(root)
    errors.extend(_sp_errors)
    warnings.extend(_sp_warnings)
    # 2026-10-19 新增三条：图注底条 / 环压卡 / 框 D 顶线
    warnings.extend(check_caption_bar(root))
    errors.extend(check_ring_overlap(root))
    warnings.extend(check_frame_d_path(root))
    errors.extend(check_bottom_intrusion(root))   # 2026-10-20：底部保留区
    return errors, warnings


def main():
    args = sys.argv[1:]
    if args:
        files = [BODY_DIR / args[0]]
    else:
        if not BODY_DIR.exists():
            print("[INFO] 无 svg_body/ 目录（尚未创作正文片段）")
            return 0
        files = sorted(BODY_DIR.glob("*.svg"))

    if not files:
        print("[INFO] 没有可检查的正文片段")
        return 0

    total_errors = 0
    total_warnings = 0
    for fp in files:
        if not fp.exists():
            print(f"❌ {fp.name}: 文件不存在")
            total_errors += 1
            continue
        errors, warnings = check_body(fp)
        if errors:
            total_errors += len(errors)
            print(f"❌ {fp.name}: {len(errors)} 个问题")
            for e in errors:
                print(f"    - {e}")
        else:
            print(f"✅ {fp.name}: 全部通过")
        for w in warnings:
            total_warnings += 1
            print(f"    ⚠️  {w}")

    print()
    if total_errors:
        print(f"❌ 共 {total_errors} 个问题，需修正后重跑")
        return 1
    if total_warnings:
        print(f"⚠️  共 {total_warnings} 条提示（不阻断，建议优化）")
    print(f"✅ 全部 {len(files)} 个正文片段校验通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
