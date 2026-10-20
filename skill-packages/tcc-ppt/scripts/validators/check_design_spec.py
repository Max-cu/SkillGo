"""
Step 1f 设计稿校验 —— 校验 `assets/design_spec.md` 的四件事：

  ① 字数      ：从 schema.json 读 max_chars，检查 §IX 各占位符是否超限。
  ② 骨架分布  ：读各内容页蓝图的 `**版式骨架**：§5.x` 行，核对契约 §2.1 反重复铁律
                —— 任一版式家族占比 ≤ 1/2、相邻内容页不得同用一族。
                · 新增（2026-10-16）：**骨架种类数 < ceil(内容页数 ÷ 3)** ⇒ 软警告
                  —— 兜住"少数几族交替"（如 §5.11 / §5.1 轮着来）：那种情形占比各 50%
                     **不超线**、相邻也**不同族** ⇒ 上面两条硬错误**双双漏网**。
                  ⚠️ **只加软警告、不动那两条硬错误** —— 它们是既有判据，改档次的回归风险高。
  ③ 布局框    ：读各内容页蓝图的 `**布局框**：x,y,w,h / …` 与 `**内容高度**：h / …` 两行，
                核对坐标自洽性 + 有没有"铺满"（契约**版式册 §5.0.1**；纵向不足的唯一流程）——
                · 底边 ≤ 648（§1）
                · 同列纵向间距 ≥ 24（§2.2）
                · 左右分栏等高差 ≤ 24（§2.2b）
                · 本页最下框底边 ≥ 608（§2.4 指标④）：< 560 硬错误 / 560~608 软警告
                · **内容跨度 ≥ 504**（§5.0.1 铁律）：< 464 硬错误 / 464~504 软警告
                  —— 抓"整体下移"（底边达标，但留白被搬到顶部，翻页时顶部会跳动）
                · **块内密度 ≥ 50%**（§5.0.1）：< 45% 硬错误 / 45~50% 软警告
                  —— 抓"半空容器"（框撑得很大、内容却填不满；实测 page05 = 37%、p7/p8 = 42%）
                  ⚠️ 硬线比 Step 2 更严（45% vs 40%）：蓝图阶段改数字成本低，末端避免昂贵重写
                  🔴 **块数 = 1 时一律降为软警告**：该情形下撑高是达成收口的唯一途径，
                     低密度是"素材不足"的必然结果 ⇒ 判据不适用（见 ⑦ 与下方说明）
                · **内容量下限 ≥ 454**（§5.0.1，2026-09-30 新增）：Σ内容高度 + Σ净间距
                  —— 抓"素材量不足以铺满本页"（< 454 硬错误 / 454~504 软警告）
                  🔑 补的是最根本的缺口：前 6 条全在查**几何自洽性**（框对不对得上），
                     没有一条查**内容够不够**。于是模型被迫用几何手段（撑框）解决内容不足问题，
                     必然在 Step 2 撞上「密度 × 跨度」互斥而死循环（实测 page05/page14 连续 6 轮）。
                     本条把问题前移到"改数字"阶段暴露 —— 那里返工成本极低。
                🔑 补的是「**Step 1 坐标定错 = Step 2 必然返工**」这个盲区：图框尺寸在 Step 1
                   就定死、Step 2 只照抄，此前**没有任何机器检查**，全靠模型自己算对。
                🔑 第 4 条（2026-09-29 加）补的是「**只有上界、没有下界**」：此前只拦"超底"、
                   不拦"太矮"，于是"矮页面"在蓝图阶段完全合法 —— 等 Step 2 生成完才被
                   `check_creative_svg` 的纵向收口拦下，白返工一轮（实测 page04 / page14 各 2:26）。
  ④ 组件编号  ：读各 009 蓝图「区块」表的「图形元素」列（2026-10-16 新增）——
                · **该列有行但为空** ⇒ 硬错误（空列 = 根本没写）
                · **整页没有任何 `组件 <编号>`** ⇒ 软警告（不阻断）
                  —— 抓"组件只选在脑子里、没落到纸上"：口径早写在 ②册 §4.8「组件写在哪」
                     与 §1e-2 清单第 11 条①，但**从来没有机器检查** ⇒ 实测历史产物该列
                     52 行**真组件编号 0 个**（唯一命中的 4 处是 §1.3 的**色彩等级** L1/L2/L3）。
                  🔴 判据要求「组件」二字与编号同现（`组件 R1`）—— 就是要与色彩等级
                     `L1 蓝` / `L2 黄` / `L3 红` 天然分家（色彩等级不带「组件」）。
                  ⚠️ 只设"警告"不阻断：②册 §4.8 明令"别为了用新组件而套组件"，硬卡会逼
                     模型硬套 ⇒ 与铁律冲突。先观察一轮覆盖率，再决定是否收紧。
  ④-b 组件分布：把 ④ 收到的编号做**全篇统计**（2026-10-16 新增）——
                · **只报告部分**：打印各组件出现次数 + 承载组件（C 族）占比（把"你用了 N 次 C1"
                  摆到模型眼前，本身就是反馈）
                · **同一承载组件占比 > 1/2** ⇒ 🔻 **2026-10-17 起降级为「观察项」**
                  （只在统计行标注，**不进警告列表**）—— 两轮实测报了零效果（88% → 88%），
                  且可能是误报：多页确实是「图 + 一组要点」，用 C1 本来就对
                · **同一组件连续 ≥3 个内容页** ⇒ 软警告
                · **承载组件种类数 < ceil(内容页数 ÷ 3)**（至少 2 种）⇒ 软警告
                  —— 兜住"少数几种交替"：`C1 C7 C1 C7 …` 占比各 50%（不超线）、连续也 <3 页
                     （不触发）⇒ **连续那条也漏网**，只有"种类数"拦得住。
                     ⚠️ 不追求更严（如"间隔 ≥3"）——那会让几乎每页都报警，
                       假报警比漏报更糟（模型会开始忽略判据，见 CONTENT_HARD 注释）。
                  —— 抓"盒子千篇一律"。🔴 两条判据**互补**（实测港珠澳大桥**同时触发两条**）：
                     **"占比"**（现为观察项）抓全篇单调（C1 ×7/8 = 88%）；**"连续"**抓局部单调
                     （C1 连续 **5 个内容页** —— 注意口径是**"内容页序列"**，中间的 003 章节页**不打断**；
                      一开始我按"页码相邻"算，以为最长只有 2 页，是错的）。
                  ⚠️ 同样只设"警告"：C 族不挑内容关系（只解决"内容怎么装"），换它零风险，
                     但历史产物 100% 触发 ⇒ 硬卡会让流程全红。先跑一轮看分布再决定是否收紧。

本脚本也被 Step 2e-① 复用（那里 design_spec.md 已定稿，一般只关心字数部分）。

用法：
    cd <SKILL_ROOT>
    python scripts/validators/check_design_spec.py
"""
import json, re, sys, os
from collections import Counter


def count_chars(text):
    """折算字符数：中文=1单位，英文字母/数字/标点=0.5单位（约2拉丁字符≈1中文视觉宽度）"""
    cn = len(re.findall(r'[\u4e00-\u9fff]', text))
    other = len(re.findall(r'[^\s\u4e00-\u9fff]', text))
    return cn + other * 0.5


def norm_family(raw):
    """把 `5` / `5.10` / `5.10.1` 归一化到两段家族号 `5.10`。
    契约里 §5.10.1 是 §5.10 的子条款，同属「编号条目列」家族，必须归一族，
    否则占比会被拆散、反重复闸门形同虚设。"""
    parts = raw.split('.')
    return '.'.join(parts[:2]) if len(parts) >= 2 else raw


# 蓝图里骨架行的规范写法（design_spec_reference.md §IX）。
# 允许 `§5.10.1` / `5.10` / `§ 5.2` 等变体；抓不到的页面会被明确报错，不会静默跳过。
SKEL_PAT = re.compile(r'\*\*版式骨架\*\*\s*[：:]\s*(?:§\s*)?(\d+(?:\.\d+)*)')
MAX_SHARE = 0.5      # 契约 §2.1 第 4 条：任一单一骨架累计占比 ≤ 1/2
SKEL_KIND_DIV = 3    # ②-c 骨架**种类数下限** = ceil(内容页数 ÷ 3)，且至少 2 种（2026-10-16 新增）
                     # 🔑 兜住"少数几族交替"：`§5.11 §5.1 §5.11 §5.1…` 占比各 50%、相邻也不同族
                     #    ⇒ ①② 双双漏网，只有种类数拦得住。与组件侧 CARRY_KIND_DIV 同口径。

# ── ④ 组件编号（「区块」表的「图形元素」列，2026-10-16 新增）──
# 口径早就写在 ②册 §4.8「组件写在哪」与 §1e-2 清单第 11 条①，但**从来没有任何机器检查**
# ⇒ 实测历史产物该列 52 行**真组件编号 0 个**（唯一命中的 4 处是 §1.3 的**色彩等级** L1/L2/L3）
# ⇒ 组件永远"选在脑子里"、Step 2 每页从零重挑 —— 这正是「新组件用不上」的根因。
# 🔴 判据要求「组件」二字与编号同现：色彩等级写作 `L1 蓝`（不带「组件」）⇒ 两者天然分家。
# 🔴 带**捕获组**：④-b 要用 finditer 取出"是哪个编号"来统计（只判真假的话 search 就够）。
COMPONENT_REF_PAT = re.compile(r'组件\s*([CRGL])\s*(\d{1,2})')
GRAPHIC_HEAD = '图形元素'        # 「区块」表第 4 列的表头文字
GRAPHIC_COL_IDX = 3              # 「区块」表第 4 列（0-based）
CARRY_SHARE_MAX = 0.5            # ④-b：承载组件占比**参考线**（2026-10-17 起只作观察项，不参与判定）
CARRY_RUN_MAX = 2                # ④-b：同一组件连续页数上限（连续 ≥3 页即警告）
CARRY_KIND_DIV = 3               # ④-b：承载组件**种类数下限** = ceil(内容页数 ÷ 3)，且至少 2

# ④-e 四族覆盖（2026-10-18）：④-b 的「种类数」只数 C 族 ⇒ R/G/L 用不用都不影响通过。
#   审计 8 份实测稿 119 处：C 93% ｜ R 2% ｜ G 1% ｜ L 4%；R1~R4/R6、G2~G5、C4/C6/C8 从未出现。
#   根因：判据看不见 ⇒ 模型不选（与「组件编号 / C1 款式」同一机理，第三次复现）。
NONC_DIV = 2                     # ④-e：非 C 族**处数下限** = ceil(内容页数 ÷ 2)，且至少 2
NONC_MIN = 2
# 配件类组件白名单：允许写进「+」配件行（不占区块位，只是挂件）。
#   依据 ③册 §5.0「第一层 通用配件」：C3/C4/C6/C8 + G1~G5。
ACCESSORY_IDS = {'C3', 'C4', 'C6', 'C8', 'G1', 'G2', 'G3', 'G4', 'G5'}
ACC_MAX_PER_PAGE = 2             # ④-f：每页配件行上限（防堆配件凑数）
ACC_ROW_PAT = re.compile(r'^\|\s*\+\s*\|')
# ④-c C1 款式（2026-10-17）：读「图形元素」列括号里的 `框 <A~F> · 内容 <1~5>`。
#   目的：④-b 只看编号，`9 处全是 C1` 与 `9 处 C1 但六款外框轮着用` 在它眼里完全一样 ——
#   2026-10-17 首测实测：9 处 C1 **全是「框 B」**，判据毫无反应，用户肉眼才发现。
#   ⇒ 把"款式"也纳入统计，「页内统一 / 页间换款」两条铁律才有机器后果。
#   兼容旧稿：一行 `框 X` 都没写时**不报**（C1_VARIANT_MIN 处不足也不报）。
C1_FRAME_PAT = re.compile(r"框\s*([A-Fa-f])")
C1_CONTENT_PAT = re.compile(r"内容\s*(\d)")
C1_VARIANT_MIN = 3              # ④-c：C1 出现 ≥3 处才检查款式多样性（太少不算雷同）
# 🔴 2026-10-18：光查「种类数」不够 —— 实测两轮都是 A 用 5 次 + B 用 1 次（=2 种，达标但不均衡）。
C1_FRAME_MAX = 2                # ④-c：同一外框最多出现在**几页**（超出即警告；按页不按次，见下）
# ④-d（2026-10-17）：区块表里写的 `image W×H` —— 必须能在「布局框」里找到同尺寸的框。
#   实测：page10 区块表写 `image 588×260`，布局框却是 `384×240` ⇒ Step 2 按 588 画，
#   三条顶部线一条 588、两条 384，长短不一。**蓝图自己前后矛盾，机器不查就没人发现。**
IMG_SIZE_PAT = re.compile(r"image\s*(\d+(?:\.\d+)?)\s*[×xX]\s*(\d+(?:\.\d+)?)")
# ④-h（2026-10-18）：一行多卡的写法 `… ×3`。
#   只认乘号 `×` 且 1~2 位、前后不接数字 —— 否则会把 `384×244` 这种尺寸当成「244 张卡」。
MULTI_CARD_PAT = re.compile(r"(?<![0-9])×\s*[1-9][0-9]?(?![0-9])")
# 只认 `组件 C1 …` / `组件 C7 …`；用 \b 挡住 C10（C10 里也含 "C1" 子串）。
_C1_LIKE_PAT = re.compile(r"组件\s*C(?:1|7)\b")
                                 # 🔑 兜住"少数几种交替"：`C1 C7 C1 C7 …` 占比各 50%（不超线）、
                                 #    连续也 <3 页（不触发）⇒ 前两条**双双漏网**，只有种类数拦得住。
                                 #    取值依据：实测"2 种 ⇒ 观感单调／3 种 ⇒ 可接受"这条分界线。

# ── ③ 布局框（坐标自洽性）──
# 蓝图里一行机器可读的坐标汇总：`x,y,w,h`，多条用 `/` 分隔（见 design_spec_reference.md §IX）。
LAYOUT_PAT = re.compile(r'\*\*布局框\*\*\s*[：:]\s*(.+)')
CANVAS_BOTTOM = 648   # 契约 §1：正文底线
MIN_GAP = 24          # 契约 §2.2：元素最小间距（§2.2b 分栏等高也用这个容差）
BOTTOM_MIN = 608      # 契约 §2.4 指标④：内容底边应落在 y ≥ 608（距底线 648 不超过 40px）
BOTTOM_HARD = 560     # 距底线超 88px（≈正文区 15%）判"明显头重脚轻" —— 与 check_creative_svg 同口径
# ── ⑤⑥⑦ "有没有铺满"（契约版式册 §5.0.1）──
SPAN_MIN = 504        # 铁律：内容跨度（底边 − 顶边）≥ 504 = 544 − 40（顶边对齐 104 时与"底边 ≥608"等价）
SPAN_HARD = 464       # 跨度差 40px 以上 → 典型的"整体下移"，阻断
DENSITY_MIN = 0.50    # 块内密度（内容高度 ÷ 框高）≥ 50%
DENSITY_HARD = 0.45   # 🔴 蓝图阶段的硬线（2026-09-30 由 0.40 提到 0.45，"前紧后松"）
                      # 依据：15 页产物 p7/p8 实测 42%（第二排卡 384×320、文字仅 ~135px）
                      # ——落在 Step 2 的软警告带里被放行，卡内空 170px+，观感确实半空。
                      # 蓝图阶段只是改几个数字、**返工成本极低** ⇒ 卡严；末端（Step 2）
                      # 保持 0.40，只兜底明显不合格的，避免昂贵的 SVG 重写。
                      # ⚠️ 但**块数 = 1 时一律降为软警告**（密度判据在此情形不适用，见 ⑥ 注释）
CONTENT_HARD = 454    # ⑦ 内容量下限（2026-09-30 新增）：Σ内容高度 + Σ净间距 ≥ 454
                      # 🔴 = SPAN_MIN(504) × 0.9 —— 「内容高度」是模型**估算值**（必然偏保守），
                      #    直接卡 504 会大量假报警（假报警比漏报更糟：会让模型开始忽略判据）。
                      #    454 与 SPAN_HARD(464) 口径接近，避免两处判据互相打架。
MID_X = 640           # 画布中线（1280 ÷ 2），用于左右分栏判定
NO_BOX_TOKENS = ('无', 'none', 'None', '-', '——', '—')
# 浮点容差：坐标允许小数（如 3 等分 544 → h=165.33），
# 直接判 `24 < 24` 会被 23.999999999999996 这类误差误报（实跑踩到过）。
EPS = 1e-6


def parse_layout_boxes(block):
    """解析一页的 `**布局框**：x,y,w,h / x,y,w,h` 行。

    返回 `(boxes, err)`：`err is None` 时 `boxes` 有效；否则 `err` 为原因串
    （`'missing'` = 整行缺失 / `'empty'` = 写了但没有框 / 其余为格式不符）。
    """
    # 🔴 **取最后一行**（`findall()[-1]`）—— 不是 `search()`：
    # 模型"改蓝图但忘了删旧行"时，留旧行会让校验器**反复用旧坐标报错**，
    # 而它看到的报错指向的是自己已经改过的那一行（实测平台为此白费 46s）。
    # 重复本身另有专门报错（见主流程的「写了 N 行」检查）。
    ms = LAYOUT_PAT.findall(block)
    if not ms:
        return None, 'missing'
    raw = ms[-1].strip().replace('`', '').strip()
    if raw in NO_BOX_TOKENS:
        return [], None           # 显式声明「本页无容器框」
    boxes = []
    for seg in re.split(r'[/／]', raw):
        seg = seg.strip().strip('。.；;')
        if not seg:
            continue
        parts = re.split(r'[,，]', seg)
        if len(parts) != 4:
            return None, f'格式不符 `{seg}`（每条应为 `x,y,w,h` 四个数字，多条用 `/` 分隔）'
        try:
            boxes.append(tuple(float(p.strip()) for p in parts))
        except ValueError:
            return None, f'含非数字 `{seg}`'
    if not boxes:
        return None, 'empty'
    return boxes, None


CONTENT_PAT = re.compile(r'\*\*内容高度\*\*\s*[：:]\s*(.+)')


def parse_content_heights(block):
    """解析 `**内容高度**：h1 / h2 / …` —— **与「布局框」一一对应**（契约 §5.0.1）。

    返回 `(vals, err)`；`err is None` 时 `vals` 有效（`'missing'` = 整行缺失）。
    估算口径：**标题行 43px + 正文行 26px**；图框直接填框高（视为填满）。
    """
    ms = CONTENT_PAT.findall(block)      # 🔴 同「布局框」：**取最后一行**
    if not ms:
        return None, 'missing'
    raw = ms[-1].strip().replace('`', '').strip()
    if raw in NO_BOX_TOKENS:
        return None, 'empty'
    vals = []
    for seg in re.split(r'[/／]', raw):
        seg = seg.strip().strip('。.；;')
        if not seg:
            continue
        try:
            vals.append(float(seg))
        except ValueError:
            return None, f'含非数字 `{seg}`'
    if not vals:
        return None, 'empty'
    return vals, None


def parse_graphic_column(block):
    """抓「区块 | 位置·形态 | 内容要点 | 图形元素」表的第 4 列（只有 009 创作页有本表）。

    返回 `(rows, err)`：`rows` = 该列各数据行原文（**保持顺序，只做 strip**）；
    `err` = `'no_table'`（本页没有这张表）/ `'empty'`（有表但一行数据行都没有）/ `None`。
    """
    lines = block.split('\n')
    head = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        # 只认「区块」表的表头 —— 同一页另一张表是 `| Placeholder | Content | Check |`，
        # 靠「图形元素」这个列名把两者分开（占位符表不含它）。
        if s.startswith('|') and GRAPHIC_HEAD in s and '区块' in s:
            head = i
            break
    if head is None:
        return [], 'no_table'
    rows = []
    for ln in lines[head + 1:]:
        s = ln.strip()
        if not s.startswith('|'):
            break                      # 表格是连续 block，遇到非表格行即结束
        if re.fullmatch(r'\|[\s:\-|]+\|', s):
            continue                   # 表头下的 `|---|---|` 分隔行
        cells = [c.strip() for c in s.strip('|').split('|')]
        if len(cells) <= GRAPHIC_COL_IDX:
            continue                   # 列数不足 —— 不是本表的数据行
        rows.append(cells[GRAPHIC_COL_IDX])
    if not rows:
        return [], 'empty'
    return rows, None


def parse_accessory_rows(block):
    """抓区块表里的「+」配件行 —— 给小配件（G1~G5 / C4 / C6 / C8）一个独立落笔位。

    返回 `(ids_all, n, order_ok, ids_outside)`：
      `ids_all`     全部配件行登记的组件编号（表内 + 表外）；
      `n`           配件行行数；
      `order_ok`    表内配件行是否**全部排在区块行之后**（排前面会抢掉「该页首个组件」）；
      `ids_outside` 写在**表格外面**的配件行组件 —— 这类行 `parse_graphic_column` 收不到，
                    必须补进组件统计，否则「纸上写了、判据报 0」。

    🔴 **表外容错（2026-10-18 实测）**：模型会把 `| + |` 写在「配图」字段之后、表格之外，
    9 页里 4 行**全被漏掉**，文档里明明写了 G1/G3/G4/C6，判据却报「非 C 族 0 处」。
    这里改为扫整个 page block，表内表外都收，表外的单独返回供统计补齐。

    标准写法（模板也用这个）：写在「区块」表**内部**，作为最后一行 ——
        | + | 页尾 y=592 | 口径注：数据截至 2024 年末 | 组件 G3 脚注注释行 |
    """
    lines = block.split('\n')

    # 先定位表格区间与其数据行文本（用于区分表内 / 表外）
    head = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith('|') and GRAPHIC_HEAD in s and '区块' in s:
            head = i
            break

    table_rows, last_block_row, idx = set(), -1, 0
    if head is not None:
        for ln in lines[head + 1:]:
            s = ln.strip()
            if not s.startswith('|'):
                break
            if re.fullmatch(r'\|[\s:\-|]+\|', s):
                continue
            table_rows.add(s)
            if not ACC_ROW_PAT.match(s):
                last_block_row = idx
            idx += 1

    ids_all, ids_outside, acc_in_table, n = [], [], [], 0
    for ln in lines:
        s = ln.strip()
        if not ACC_ROW_PAT.match(s):
            continue
        cells = [c.strip() for c in s.strip('|').split('|')]
        if len(cells) <= GRAPHIC_COL_IDX:
            continue
        got = [m.group(1) + m.group(2)
               for m in COMPONENT_REF_PAT.finditer(cells[GRAPHIC_COL_IDX])]
        n += 1
        ids_all.extend(got)
        if s in table_rows:
            acc_in_table.append(idx)
        else:
            ids_outside.extend(got)

    # 只判表内顺序：表外配件行天然在表格之后
    order_ok = True
    if head is not None and acc_in_table:
        rows_seq, acc_seq, k = [], [], 0
        for ln in lines[head + 1:]:
            s = ln.strip()
            if not s.startswith('|'):
                break
            if re.fullmatch(r'\|[\s:\-|]+\|', s):
                continue
            (acc_seq if ACC_ROW_PAT.match(s) else rows_seq).append(k)
            k += 1
        if acc_seq and rows_seq:
            order_ok = min(acc_seq) > max(rows_seq)

    return ids_all, n, order_ok, ids_outside


def parse_content_column(block):
    """抓「区块 | 位置·形态 | 内容要点 | 图形元素」表的第 3 列（内容要点）。

    用途：判断「这一页到底有没有具体数字」—— 给「内容 5 数字式」兜底。
    返回各数据行的拼接文本（取不到则返回空串）。
    """
    lines = block.split('\n')
    head = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith('|') and GRAPHIC_HEAD in s and '区块' in s:
            head = i
            break
    if head is None:
        return ''
    out = []
    for ln in lines[head + 1:]:
        s = ln.strip()
        if not s.startswith('|'):
            break
        if re.fullmatch(r'\|[\s:\-|]+\|', s):
            continue
        cells = [c.strip() for c in s.strip('|').split('|')]
        if len(cells) >= 3:
            out.append(cells[2])
    return ' '.join(out)


def check_layout_boxes(boxes, page_id, heights=None):
    """对一页的布局框做七条断言，返回 `(errs, warns)`。

    ① 底边 ≤ 648（§1）｜② 同列净间距 ≥ 24（§2.2）｜③ 左右分栏等高差 ≤ 24（§2.2b）
    ④ 本页最下框底边 ≥ 608（§2.4 指标④）　：< 560 → errs / 560~608 → warns
    ⑤ 内容跨度 ≥ 504（§5.0.1 铁律，防"整体下移"）：< 464 → errs / 464~504 → warns
    ⑥ 块内密度 ≥ 50%（§5.0.1，防"半空容器"）：`heights is None` 时整条跳过
       （< 45% → errs / 45~50% → warns；硬线比 Step 2 的 40% 更严，见常量注释）
    ⑦ 内容量下限 ≥ 454（§5.0.1，防"素材量不足以铺满"）：**仅 `框数≥2` 才判**（单框页整条跳过）
    """
    errs = []
    warns = []
    g = lambda v: f'{v:g}'   # noqa: E731 —— 数值格式化，去掉多余的 .0

    # ① 底边不得越过正文底线 648
    for (x, y, w, h) in boxes:
        if y + h > CANVAS_BOTTOM + EPS:
            over = y + h - CANVAS_BOTTOM
            errs.append(
                f'{page_id}: 框 ({g(x)},{g(y)},{g(w)},{g(h)}) 底边 {g(round(y + h, 2))} '
                f'> {CANVAS_BOTTOM}，超出 {g(round(over, 2))}（契约 §1）'
                f' → 高度减到 ≤{g(round(CANVAS_BOTTOM - y, 2))}，'
                f'或整体上移到 y ≤{g(round(CANVAS_BOTTOM - h, 2))}'
            )

    # ② 同一列（x 完全相同）内，纵向净间距 ≥ 24
    cols = {}
    for b in boxes:
        cols.setdefault(b[0], []).append(b)
    for x, group in cols.items():
        group.sort(key=lambda b: b[1])
        for a, b in zip(group, group[1:]):
            gap = b[1] - (a[1] + a[3])
            if gap < MIN_GAP - EPS:
                need = MIN_GAP - gap
                errs.append(
                    f'{page_id}: 同列 x={g(x)} 两框净间距 {g(round(gap, 2))} < {MIN_GAP}'
                    f'（上框 y {g(a[1])}~{g(a[1] + a[3])}，下框起 y {g(b[1])}）—— 契约 §2.2'
                    f' → 下框 y 提到 ≥{g(round(a[1] + a[3] + MIN_GAP, 2))}（+{g(round(need, 2))}），'
                    f'或上框高度减 {g(round(need, 2))}（{g(a[3])}→{g(round(a[3] - need, 2))}）'
                )

    # ③ 左右分栏等高（仅当"恰好左一组 + 右一组、且无跨中线框"时才判，多栏结构自动跳过）
    cross = [b for b in boxes if b[0] < MID_X < b[0] + b[2]]
    left = [b for b in boxes if b[0] + b[2] <= MID_X]
    right = [b for b in boxes if b[0] >= MID_X]
    if left and right and not cross:
        lb = max(b[1] + b[3] for b in left)
        rb = max(b[1] + b[3] for b in right)
        if abs(lb - rb) > MIN_GAP + EPS:
            diff = abs(lb - rb)
            need = diff - MIN_GAP
            low, high = min(lb, rb), max(lb, rb)
            short_side = '左栏' if lb < rb else '右栏'
            errs.append(
                f'{page_id}: 左右分栏不等高 —— 左底 {g(lb)} / 右底 {g(rb)}，'
                f'差 {g(round(diff, 2))} > {MIN_GAP}（契约 §2.2b）'
                f' → {short_side}加高 {g(round(need, 2))}px 收口到 {g(round(high, 2))} 附近，'
                f'或长的一侧缩到 ≤{g(round(low + MIN_GAP, 2))}'
            )

    # ④ 纵向收口：本页最下框的底边应 ≥ 608（契约 §2.4 指标④）
    #    🔑 前三条只管"别超、别挤、别歪"，**没有一条管"别太矮"** —— 于是蓝图阶段画一个
    #    只铺到 520 的页面完全合法，等 Step 2 生成完才被拦下。本条把这道闸门前移到 Step 1。
    if boxes:
        bot = max(y + h for (_, y, _, h) in boxes)
        if bot < BOTTOM_MIN - EPS:
            lb = max(boxes, key=lambda b: b[1] + b[3])
            need = BOTTOM_MIN - bot
            msg = (
                f'{page_id}: 纵向收口不足 —— 本页最下框 ({g(lb[0])},{g(lb[1])},'
                f'{g(lb[2])},{g(lb[3])}) 底边 {g(round(bot, 2))}，'
                f'距下限 {BOTTOM_MIN} 还差 {g(round(need, 2))}px（契约 §2.4 指标④）'
                f' → 按 §5.0.1 处理：先把最下框底边定到 648，再自下而上分配每块高度。'
                f'🔑 先数该列纵向块数 —— ≥2 块可"撑块 + 同步补文本"；'
                f'⚠️ 只有 1 块时不许撑，走第 2 步"缩小原块 + 加异质新组件"（拆块次选）'
                f'｜🚫 不要用"上移底部块"腾间距 —— 那只是把留白从中间挪到底部'
                f'（指标③ 与 ④ 会互相顶）'
            )
            (errs if bot < BOTTOM_HARD - EPS else warns).append(msg)

    # ⑤ 内容跨度 ≥ 504（§5.0.1 铁律）—— 抓"整体下移"
    if boxes:
        _top = min(b[1] for b in boxes)
        _bot = max(b[1] + b[3] for b in boxes)
        span = _bot - _top
        if span < SPAN_MIN - EPS:
            need = SPAN_MIN - span
            msg = (
                f'{page_id}: 内容跨度不足 —— 顶边 {g(round(_top, 2))}、底边 {g(round(_bot, 2))}，'
                f'跨度 {g(round(span, 2))} < {SPAN_MIN}（契约 §5.0.1 铁律）'
                f' → 这是"整体下移"的典型症状：底边凑到了，但留白被搬到了顶部，'
                f'翻页时顶部会跳动。正确做法是让内容真的占满（还差 {g(round(need, 2))}px）：'
                f'按 §5.0.1 主流程 —— 该列纵向块数 ≥2 时"撑块 + 同步补文本"；'
                f'只有 1 块时走"缩小原块 + 加异质新组件"｜🚫 不要整体下移'
            )
            (errs if span < SPAN_HARD - EPS else warns).append(msg)

    # ⑥ 块内密度 ≥ 50%（§5.0.1）—— 抓"半空容器"（Step 1 硬线 45%，比 Step 2 的 40% 严）
    if heights and boxes:
        if len(heights) != len(boxes):
            errs.append(
                f'{page_id}: 「内容高度」给了 {len(heights)} 个值，与「布局框」的 '
                f'{len(boxes)} 个框数量不匹配 —— 必须一一对应（顺序一致、`/` 分隔）'
            )
        else:
            for (x, y, w, h), ch in zip(boxes, heights):
                if h <= 0:
                    continue
                dens = ch / h
                if dens < DENSITY_MIN - EPS:
                    need = DENSITY_MIN * h - ch
                    ncol = sum(1 for b in boxes if abs(b[0] - x) < EPS)
                    if ncol >= 2:
                        howto = f'该列有 {ncol} 个框 → 可"撑块 + 同步补文本"'
                    else:
                        # 🔴 块数 = 1：密度低是"素材不足"的必然结果，判据不适用（见 ⑦）
                        howto = ('该列只有 1 个框 → ⚠️ 单块撑满整列：低密度是这页'
                                 '素材量不足的必然结果，不是排版失误。'
                                 '🚫 别再缩块（缩了跨度必 <504）、🚫 也别靠撑框凑——两者会互相顶。'
                                 '正解是减少本页承载：换更矮的骨架（§5.9 / §5.10）、'
                                 '或把内容匀到相邻页')
                    msg = (
                        f'{page_id}: 块内密度不足 —— 框 ({g(x)},{g(y)},{g(w)},{g(h)}) '
                        f'内容高度 {g(ch)} / 框高 {g(h)} = {dens:.0%} < {DENSITY_MIN:.0%}'
                        f'（契约 §5.0.1） → '
                        f'{"这是\"半空的容器\"：框太大、里面是空的。" if ncol >= 2 else ""}'
                        f'{howto}（要达标还需约 {g(round(need, 2))}px 内容）'
                    )
                    # 🔴 块数 = 1 → 一律软警告（密度判据在此情形不适用，与 Step 2 同口径）
                    (errs if (dens < DENSITY_HARD - EPS and ncol >= 2) else warns).append(msg)

    # ⑦ 内容量下限（§5.0.1）—— 抓"素材量不足以铺满本页"
    #    🔴 **补的是"判据全在查几何自洽、没一条查内容够不够"这个根本缺口**（2026-09-30）：
    #    之前 6 条都是"框对不对得上"（底边/间距/等高/收口/跨度/密度），
    #    于是模型被迫用**几何手段（撑框）**去解决**内容不足**问题 —— 方向从一开始就错，
    #    必然在 Step 2 撞上"密度 × 跨度互斥"的死循环（实测 page05 / page14 连续 6 轮）。
    #    本条把问题**前移到"改数字"阶段**暴露：那里返工成本极低。
    #
    #    🔑 **判据为什么用「框几何跨度」而不是「Σ内容高度」**：
    #      容器天然有内边距 —— 一个 `544` 高的卡装 `300px` 内容**是正常饱满的**
    #      （组件 C1 的卡就有标题带 + 四周留白）。若直接拿 Σ内容高度 比 454，
    #      会把"内容合理地放进大卡"误判成"内容不足"（实测夹具第一例即假报警）。
    #      ⇒ 改为：**框的几何跨度**（已由 ⑤ 保证 ≥504）+ 各框**内容是否填得进**。
    #      即：只有当"内容高度之和 + 框间净距"**明显低于页面可用跨度**时，才说明
    #      这块内容本身撑不起这一页 —— 与"框画得对不对"解耦。
    #
    #    🔴 **单框页整条跳过** —— 单框页无法靠"框间间距"补足，而它自身必然有内边距，
    #      "内容高度"这个字段根本表达不了"内容量与页面的配比"（实测：单框装 300px
    #      内容仍报"不足"，属假报警）。单框页的"够不够"由**密度判据**负责 ——
    #      而密度判据在块数=1 时已降为软警告（正是本轮的改动一）⇒ 两者口径一致，
    #      不会互相顶。本条只服务**多框页**（那种页才是"框间有洞、需要凑高度"的场景）。
    #
    #    判据：`Σ内容高度 + Σ框间净距 ≥ CONTENT_HARD(454)`
    #      · 用**已有的两个字段**算出，**不增加模型的计算量**；
    #      · 454 = 504（跨度铁律）× 0.9 —— 因「内容高度」是模型**估算值**（必然偏保守），
    #        直接卡 504 会大量假报警；假报警比漏报更糟（会让模型开始忽略判据）。
    if heights and boxes and len(heights) == len(boxes) and len(boxes) >= 2:
        # 按 y 排序后累加：内容高度之和 + 相邻框净间距之和
        pairs = sorted(zip(boxes, heights), key=lambda t: t[0][1])
        sum_ch = sum(ch for _, ch in pairs)
        gaps = 0.0
        for i in range(1, len(pairs)):
            prev_y, prev_h = pairs[i - 1][0][1], pairs[i - 1][0][3]
            cur_y = pairs[i][0][1]
            gaps += max(0.0, cur_y - (prev_y + prev_h))
        actual = sum_ch + gaps
        if actual < CONTENT_HARD - EPS:
            need = CONTENT_HARD - actual
            msg = (
                f'{page_id}: 内容量不足 —— 各框「内容高度」合计 {g(round(sum_ch, 2))}px '
                f'+ 框间净间距 {g(round(gaps, 2))}px = {g(round(actual, 2))}px，'
                f'低于铺满本页所需 {CONTENT_HARD}px（还差 {g(round(need, 2))}px）'
                f'（契约 §5.0.1） → 本页素材量偏少，几何上无法铺满。三选一：'
                f'① 从相邻页匀一部分内容过来（优先，无成本）；'
                f'② 换更"矮"的骨架（§5.10 编号条目列实体化 / §5.4 表格 —— 行高可到 56~58，'
                f'⚠️ 上限 58：单行文字的行高 ≥60 会被判「半空的容器」，见 §4.8 组件 C9）；'
                f'③ 回素材准备阶段补料。'
                f'🚫 不要靠"撑大框"解决 —— 那只会在 Step 2 触发密度报警，'
                f'两条判据会互相顶'
            )
            (errs if actual < CONTENT_HARD - EPS else warns).append(msg)
        elif actual < SPAN_MIN - EPS:
            # 454~504：偏少但可接受 —— 只警告（给模型一个"下次注意"的信号，不阻断）
            warns.append(
                f'{page_id}: 内容量偏少 —— 合计 {g(round(actual, 2))}px，'
                f'距铺满所需 {SPAN_MIN}px 还差 {g(round(SPAN_MIN - actual, 2))}px'
                f' → 当前可接受；若能匀一点内容过来会更饱满（§5.0.1）'
            )

    # 🔴 「整页汇总」置于最前：先给整体方向，再列逐条 ——
    # 否则模型容易"改一条冒一条"（实测 Step 1 布局框连修 3 轮：超底 → 重叠 → 间距）。
    if len(errs) >= 2 and len(boxes) >= 2:
        total_over = sum(max(0.0, (y + h) - CANVAS_BOTTOM) for (_, y, _, h) in boxes)
        if total_over > 0:
            errs.insert(0, f'{page_id}: ⚠️ 本页 {len(boxes)} 个框纵向共超出 '
                           f'{g(round(total_over, 2))}px → 建议先整体重排'
                           f'（每框平均减 {g(round(total_over / len(boxes), 2))}px，或去掉 1 个框），'
                           f'再逐条核对下面的间距')
        else:
            errs.insert(0, f'{page_id}: ⚠️ 本页共 {len(errs)} 处布局框问题 '
                           f'→ 请一次性重排页面框位，不要只改下面列出的这几条'
                           f'（否则下一轮会把剩下的再报一遍）')
    return errs, warns


def main():
    # `-h/--help`：本脚本原本"无参数、跑起来才发现用错"，模型诊断时会白跑一轮（10-14 实测 36s）
    if any(a in ('-h', '--help') for a in sys.argv[1:]):
        print((__doc__ or '').strip().split('\n\n')[0])
        print('\n用法：python3 scripts/validators/check_design_spec.py   '
              '# 无参数，直接校验 assets/design_spec.md')
        print('风格判定：读 assets/pipeline_config.json 的 _meta.palette'
              '（文件缺失 ⇒ 回退 default；用 minimal 时会按 default 的字数上限误判 '
              '—— 见 step_1 §1a-0）')
        return

    skill_root = os.getcwd()
    spec_path = os.path.join(skill_root, 'assets', 'design_spec.md')
    config_path = os.path.join(skill_root, 'assets', 'pipeline_config.json')

    if not os.path.isfile(spec_path):
        print(f'❌ design_spec.md 不存在: {spec_path}')
        sys.exit(1)

    # ── schema 按风格定位（2026-10-09 起每风格各一份，与模板同构）──
    # 🔑 风格名解析**共用 apply_palette.resolve_style_name()**（方案 C：未显式声明时从 palette 派生）
    # 缺省 default；读不到 config 时也按 default（Step 1 阶段 config 可能还没写）
    style_name = 'default'
    if os.path.isfile(config_path):
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                _cfg = json.load(f)
            _meta = _cfg.get('_meta') or {}
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            from apply_palette import resolve_style_name
            style_name, _ = resolve_style_name(
                _meta, palette_name=_meta.get('palette'), warn=False)
        except (json.JSONDecodeError, OSError, ImportError):
            pass
    schema_path = os.path.join(skill_root, 'assets', 'templates', style_name,
                               'pipeline_config.schema.json')

    if not os.path.isfile(schema_path):
        print(f'❌ schema 不存在: {schema_path}')
        print(f'   ← 当前风格 "template_set" = "{style_name}"，'
              f'该风格目录下应有自己的 pipeline_config.schema.json')
        avail = os.path.join(skill_root, 'assets', 'templates')
        if os.path.isdir(avail):
            styles = [d for d in sorted(os.listdir(avail))
                      if os.path.isdir(os.path.join(avail, d))]
            print(f'   可用风格: {", ".join(styles) if styles else "(无)"}')
        sys.exit(1)

    with open(schema_path, 'r', encoding='utf-8') as f:
        schema = json.load(f)
    with open(spec_path, 'r', encoding='utf-8') as f:
        spec = f.read()

    tref = schema['_template_reference']
    errors = []

    # ── 覆盖率统计：成功时也必须说清"查了多少"，否则无法分辨
    #    "校验没生效" 与 "全部合规"（两者输出原本完全一样）──
    pages_seen = []          # 命中的页面 id
    checked = 0              # 实际校验过的占位符数
    skipped = []             # 内容含 ✓/≤ 而被跳过的
    unverified = []          # 取不到占位符行的

    skel_pages = []          # [(page_id, family)] —— 按 §IX 顺序
    skel_missing = []        # 009 页**完全没写**「版式骨架」的
    skel_badfmt = []         # 写了但抓不到 §5.x 家族号（格式不符）的
    skel_warns = []          # ②-c 骨架种类数过少（软警告，不阻断）

    layout_errs = []         # ③ 布局框：格式问题 + 坐标断言失败
    layout_warns = []        # ③ 布局框：纵向收口偏浅（560~608，不阻断）
    layout_ok = []           # ③ 布局框校验通过的页面 id
    layout_none = []         # ③ 显式写了「无」（本页无容器框）的页面

    graphic_errs = []        # ④ 「图形元素」列空 / 整表无登记（阻断）
    comp_errs = []           # ④-c 全篇只用 1 种外框 ⇒ 阻断（2026-10-18 由警告升级）
    graphic_warns = []       # ④ 整页无组件编号 / 缺「区块」表（不阻断）
    graphic_ok = []          # ④ 至少登记了 1 个组件编号的页面 id
    graphic_pages = []       # ④-b [(page_id, [编号…])]，按 §IX 顺序
    acc_pages = []           # ④-f [(page_id, [编号…], 行数, 顺序是否合规)]，「+」配件行
    content_by_page = {}     # ④-g {page_id: 「内容要点」列全文}，用于识别 R/L 专属形态
    c1_variants = []         # ④-c [(page_id, 外框款)]，只收写了 `框 X` 的 C1 行

    # 解析 §IX 中的每个页面 block
    page_blocks = re.split(r'\n### ', spec)
    for block in page_blocks:
        if not block.strip():
            continue

        # 🔴 页面块格式固定为 `### pageXX — <模板名>`（见 design_spec_reference.md）。
        # 必须显式判断：某些 deck 的 §IX 里还留着旧版「骨架分布表」，表里也写着
        # 001_cover / 009_creative 这类模板名，不加这道闸就会被当成页面块、虚报"未取到值"。
        m_id = re.match(r'(page\d+)', block.strip())
        if not m_id:
            continue
        page_id = m_id.group(1)
        # 计数放在"找模板名"之前：010_ending 这类无占位符的页面也应计入"已扫描"，
        # 否则报告页数会比实际页数少，看着像漏检。
        if page_id not in pages_seen:
            pages_seen.append(page_id)

        lines = block.strip().split('\n')

        # 找模板名
        current_template = None
        for line in lines:
            tm = re.search(r'(\d{3}_(?:cover|toc|content))', line)
            if tm:
                current_template = tm.group(1)
                break
        if not current_template:
            # 009 创作页：块名是 009_creative（不是 009_content）。
            # 它与门面页走完全相同的占位符表格校验——蓝图第一部分就是 `| Placeholder | Content | Check |` 表。
            if re.search(r'009_creative', block):
                current_template = '009_content'
            else:
                continue

        # ── 骨架登记 + 布局框校验（只有 009 创作页有）──
        if current_template == '009_content':
            sm = SKEL_PAT.search(block)
            if sm:
                skel_pages.append((page_id, norm_family(sm.group(1))))
            elif '版式骨架' in block:
                # 写了这一行、但没写成 `§5.x` —— 区分于"整行缺失"，
                # 否则文案会说"缺行"而让人对着已有的行发懵。
                skel_badfmt.append(page_id)
            else:
                skel_missing.append(page_id)

            # ── ③ 布局框（坐标自洽性）──
            # 🔴 先查「重复字段」：模型改蓝图时常留下旧行，导致校验器一直用旧坐标
            #    报错、而报错指向的正是它已经改好的那一行（平台实测白费 46s）。
            n_lay = len(LAYOUT_PAT.findall(block))
            if n_lay > 1:
                layout_errs.append(
                    f'{page_id}: 「布局框」写了 {n_lay} 行 —— 每个字段只能写一行，'
                    f'请把旧行删掉（校验器只认最后一行；留着旧行会让你反复看到'
                    f'指向旧坐标的报错，白费一轮）'
                )
            n_hgt = len(CONTENT_PAT.findall(block))
            if n_hgt > 1:
                layout_errs.append(
                    f'{page_id}: 「内容高度」写了 {n_hgt} 行 —— 同上，只保留一行'
                )
            boxes, lerr = parse_layout_boxes(block)
            if lerr == 'missing':
                layout_errs.append(
                    f'{page_id}: 蓝图缺「布局框」行 —— 补写 '
                    f'`**布局框**：x,y,w,h / x,y,w,h`（本页全部容器框；缺行则坐标无人把关）'
                )
            elif lerr:
                layout_errs.append(f'{page_id}: 「布局框」{lerr}')
            elif not boxes:
                layout_none.append(page_id)          # 显式声明「本页无容器框」
            else:
                heights, herr = parse_content_heights(block)
                if herr:
                    layout_errs.append(
                        f'{page_id}: 「内容高度」{herr} —— 补写 `**内容高度**：h1 / h2 …`'
                        f'（须与「布局框」一一对应；估算口径：标题行 43px + 正文行 26px，'
                        f'图框直接填框高）。Step 1f 据此算"块内密度 ≥50%"（契约 §5.0.1）'
                    )
                page_errs, page_warns = check_layout_boxes(boxes, page_id, heights)
                layout_errs.extend(page_errs)
                layout_warns.extend(page_warns)
                if not page_errs:
                    layout_ok.append(page_id)

            # ── ④ 组件编号（「图形元素」列）──
            # 与前六项不同：前六项查"几何对不对"，这一项查"**组件选没选、有没有落到纸上**"。
            grows, gerr = parse_graphic_column(block)
            if gerr == 'no_table':
                graphic_warns.append(
                    f'{page_id}: 没找到「区块」表（`| 区块 | 位置·形态 | 内容要点 | 图形元素 |`）—— '
                    f'它是组件选型唯一的落点，缺了就无从检查（见 step_1 §1e）'
                )
            elif gerr == 'empty':
                graphic_errs.append(
                    f'{page_id}: 「区块」表的「图形元素」列一行都没填 —— '
                    f'每个区块都要写清用什么画（图位行写 `image 宽×高 + 图注`）'
                )
            else:
                blanks = [i + 1 for i, c in enumerate(grows) if not c]
                if blanks:
                    graphic_errs.append(
                        f'{page_id}: 「区块」表第 {"、".join(str(i) for i in blanks)} 行的'
                        f'「图形元素」列为空 —— 每个区块都必须写清用什么画'
                    )
                ids = [m.group(1) + m.group(2)
                       for c in grows for m in COMPONENT_REF_PAT.finditer(c)]
                graphic_pages.append((page_id, ids))
                # ④-f 「+」配件行（不占区块位，只把小配件挂上去）
                content_by_page[page_id] = parse_content_column(block)
                _acc_ids, _acc_n, _acc_order, _acc_out = parse_accessory_rows(block)
                if _acc_n:
                    acc_pages.append((page_id, _acc_ids, _acc_n, _acc_order))
                    if _acc_out:
                        # 写在表格外面的配件行：parse_graphic_column 收不到，这里补上
                        ids.extend(_acc_out)
                        graphic_pages[-1] = (page_id, ids)
                # ④-d 区块表的 image 尺寸必须能在「布局框」里找到同尺寸的框
                _imgs = [(float(m.group(1)), float(m.group(2)))
                         for c in grows for m in IMG_SIZE_PAT.finditer(c)]
                if _imgs and boxes:
                    _bsz = [(b[2], b[3]) for b in boxes]
                    _bl = "、".join(f'{bw:g}×{bh:g}' for bw, bh in dict.fromkeys(_bsz))
                    for _w, _h in sorted(set(_imgs)):
                        if not any(abs(_w - bw) <= 2 and abs(_h - bh) <= 2 for bw, bh in _bsz):
                            graphic_warns.append(
                                f'{page_id}: 「区块」表写 image {_w:g}×{_h:g}，但「布局框」里没有这个尺寸'
                                f'（布局框是 {_bl}）—— **蓝图自相矛盾**：Step 2 会照区块表的尺寸画，'
                                f'画出一个布局框里不存在的框（实测后果：同一排卡的顶部线长短不一）。'
                                f'把两边改成同一个尺寸'
                            )
                # ④-h 区块表必须覆盖所有「布局框」（2026-10-18）
                #    实测：page07 写了 3 个框（两张对比卡 + 一条通栏），区块表只登记 2 行
                #    ⇒ 那条通栏没有归属，Step 2 就自己编了内容，**抄了下一页的主题**，
                #    相邻两页把海缆/防腐/防冲刷讲了两遍。
                #    ⚠️ 只在「一行一框」的页面上判定：写 `×3`（一行多卡）、有 `image`、
                #    或用了整页型 / C5 / C7 / C9（一行管多框）时整条跳过，避免误报。
                _nrows = len(grows) + len(_acc_out)
                _has_mult = any(MULTI_CARD_PAT.search(c) for c in grows)
                _has_img_cell = any(IMG_SIZE_PAT.search(c) for c in grows)
                _exempt = any(i.startswith('L') or i in {'C5', 'C7', 'C9'} for i in ids)
                if (boxes and not _has_mult and not _has_img_cell
                        and not _exempt and _nrows < len(boxes)):
                    _orphan = len(boxes) - _nrows
                    graphic_warns.append(
                        f'{page_id}: 「布局框」有 {len(boxes)} 个，但「区块」表只登记了 '
                        f'{_nrows} 行（含「+」配件行）—— **{_orphan} 个框没有归属**。'
                        f'Step 2 会给这些框自己编内容（实测后果：抄了相邻页的主题，'
                        f'两页讲同一件事）。给每个框补一行区块，或删掉多余的框'
                    )
                # ④-c：收集 C1/C7 的外框款式（一行里若有多处 `框 X`，取第一个）
                # 🔴 必须精确匹配 `组件 C1` / `组件 C7` —— `"C1" in c` 会误命中 **C10**（含子串 C1）。
                # 🔴 C7（2×2 矩阵）**四格也是 C1** ⇒ 那行同样要带框号，否则款式判据看不见它。
                _page_cts = []      # 本页 C1 用到的内容款数字
                _no_fr = 0          # 写了 C1 却没给框号（含照抄占位符 `框 <A~F>`）
                for c in grows:
                    if _C1_LIKE_PAT.search(c):
                        _fm = C1_FRAME_PAT.search(c)
                        if _fm:
                            _cm = C1_CONTENT_PAT.search(c)
                            c1_variants.append((
                                page_id, _fm.group(1).upper(),
                                _cm.group(1) if _cm else ""))
                            if _cm:
                                _page_cts.append(_cm.group(1))
                        else:
                            _no_fr += 1
                # 🔴 2026-10-18 新增：**照抄占位符 `框 <A~F>`** ⇒ 判据此前完全看不见
                #   （`C1_FRAME_PAT = 框\s*([A-Fa-f])` 遇到 `<` 直接不匹配 ⇒ 该行根本不进 c1_variants）
                #   ⇒ 款号留空 = 静默跳过，比"写错"更隐蔽。必须显式报出来。
                if _no_fr:
                    graphic_warns.append(
                        f'{page_id}: {_no_fr} 处 `组件 C1` 没给具体框号（写成 `框 <A~F>` 之类）—— '
                        f'框号必须是 **A~F 里的一个具体字母**。'
                        f'留占位符等于没选款：款式判据完全看不见它，Step 2 只能按默认画'
                    )
                # 🔴 2026-10-18 新增：**内容 5 数字式但本页一个数字都没有**
                #   五款里只有它强制"必须真有数字，不许编"（②册 C1）。没有数字还选它，
                #   等于把"标题+解释"硬套成"数字徽章"，色块里只能编一个数出来。
                if '5' in _page_cts:
                    _ctext = parse_content_column(block)
                    if _ctext and not re.search(r'\d', _ctext):
                        graphic_warns.append(
                            f'{page_id}: 用了**内容 5（数字式）**，但本页「内容要点」列里一个阿拉伯数字都没有 —— '
                            f'数字式必须真有数字，不许编（②册 C1）。'
                            f'没有具体数字就换 **1 上下式 / 3 引领句 / 4 分条式**'
                        )
                if ids:
                    graphic_ok.append(page_id)
                else:
                    graphic_warns.append(
                        f'{page_id}: 「图形元素」列没有任何组件编号 —— 写了组件才叫选过组件'
                        f'（写法 `组件 C1 左色条卡 + …`，编号见 ②册 §4.8）'
                    )

        tmpl = tref.get(current_template, {})
        ps = tmpl.get('placeholder_schema', {})
        if not ps:
            # 如 010_ending 无文本占位符 —— 页面本身算"已扫描"，只是不产生占位符校验
            continue

        for ph_name, ph_info in ps.items():
            max_c = ph_info.get('max_chars', 999)
            # 占位符名可能被反引号包裹（design_spec_reference.md 的写法：`TITLE` | ...），
            # 也可能不带反引号（TITLE | ...）。两种都要能匹配，否则本校验会静默跳过。
            # 🔴 前后加词边界断言（2026-10-15 修）：防槽位名互为子串时**误匹配** ——
            #    实测 `TITLE` 会在 `| \`SUBTITLE\` | … |` 行上命中（"SUB**TITLE**" 尾部含它），
            #    而主流程取 `matches[-1]` ⇒ **拿 SUBTITLE 的内容去过 TITLE 的上限** ⇒ 超限静默漏报
            #    （vibrant 封面有 SUBTITLE 槽位：实测 TITLE 20 折算字 > 上限 18 却没报）。
            #    加断言后 `TITLE` 不再匹配 SUBTITLE / TITLE_EN / TOC_ITEM_x_TITLE / PAGE_TITLE /
            #    CHAPTER_TITLE（极端碰撞测试：旧正则查 TITLE 命中 8 处 ⇒ 新正则 1 处）。
            pattern = rf'(?<![A-Za-z_]){re.escape(ph_name)}(?![A-Za-z_])`?\s*\|(.+?)(?:\||$)'
            matches = re.findall(pattern, block)
            if not matches:
                # 取不到值：**不**报错（占位符漏写由 check_required_slots.py 在 Step 2e-③ 统一管，
                # 两个校验器都报会刷屏）。但 009 例外——它曾因蓝图写成字段行
                # （而非占位符表格行）导致取不到值、静默漏检，所以单独提示，避免重蹈覆辙。
                if current_template == '009_content':
                    errors.append(
                        f'{page_id} {current_template} {ph_name}: 未找到占位符行 '
                        f'（需在蓝图开头写 `| `{ph_name}` | 内容 | `≤15字 ✓` |` 表格行），无法校验字数'
                    )
                else:
                    # 门面页原先在这里静默 continue：一旦正则没匹配上，
                    # 输出与"全部合规"一模一样，使用者无从判断校验是否真的跑过。
                    unverified.append(f'{page_id} ({current_template}) {ph_name}')
                continue
            content = matches[-1].strip().strip('`').strip()
            if '✓' in content or '≤' in content:
                # 逃生舱：本意是跳过 Check 列，但正则取的是 Content 列 ——
                # 正文若自带 "≤24px" 这类参数会被一并跳过。故不能默默跳过，必须计数上报。
                skipped.append(f'{page_id} {ph_name}')
                continue
            checked += 1
            cn = count_chars(content)
            if cn > max_c:
                # 🔴 2026-10-20：**必须带 page_id**。4 个章节页共用 008_chapter，
                #    只报模板名 ⇒ 模型不知道是哪一章超限，只能靠猜（实测连续 2 轮改错页）。
                #    同时把「汉字数 / 其他字符数」拆开显示：模型曾把 7 个汉字数成 6 个，
                #    看到 `7 汉字 + 0 其他 = 7` 才不会跟判据争执。
                _cn_n = len(re.findall(r'[\u4e00-\u9fff]', content))
                _ot_n = len(re.findall(r'[^\s\u4e00-\u9fff]', content))
                errors.append(
                    f'{page_id} {current_template} {ph_name}: {cn:g} 折算字 > 上限 {max_c}'
                    f'（实测 {_cn_n} 汉字 + {_ot_n} 其他字符 → {cn:g}，超出 {cn - max_c:g}）'
                    f'—— 🔑 口径 = **中文 1 / 标点·字母·数字 0.5**（空格不计）'
                    f'；取到的原文是「{content[:40]}」'
                )

    # ── 骨架分布检查（契约 §2.1 第 4 条「反重复闸门」）──
    skel_errors = []
    for page_id in skel_missing:
        skel_errors.append(
            f'{page_id}: 蓝图缺「版式骨架」行 —— 补写 '
            f'`**版式骨架**：§5.x <家族名>`（否则反重复闸门无数据可查）'
        )
    for page_id in skel_badfmt:
        skel_errors.append(
            f'{page_id}: 「版式骨架」没写成 §5.x 家族号（如 `§5.11 上图下文`）'
            f'—— 家族号是机器统计的唯一依据，必须带 `§5.x`'
        )
    n_skel = len(skel_pages)
    skel_line = ''
    if n_skel:
        cnt = Counter(f for _, f in skel_pages)
        # ① 任一单一骨架占比 > 1/2
        top_fam, top_n = cnt.most_common(1)[0]
        if top_n / n_skel > MAX_SHARE:
            skel_errors.append(
                f'版式家族 §{top_fam} 占 {top_n}/{n_skel} = {top_n / n_skel:.0%} '
                f'> {MAX_SHARE:.0%}（契约 §2.1 铁律）—— 把超限的页换到别的家族'
            )
        # ② 相邻内容页不得同用一族（隔页轮换同样算雷同，这里只查相邻这一档）
        for (p1, f1), (p2, f2) in zip(skel_pages, skel_pages[1:]):
            if f1 == f2:
                skel_errors.append(
                    f'相邻内容页 {p1} 与 {p2} 同用骨架 §{f1}（契约 §2.1 铁律）'
                )
        # ③ 种类数下限（2026-10-16 新增，**软警告**）—— 兜住"少数几族交替"：
        #    那情形下占比不超 1/2、相邻也不同族 ⇒ ①② 双双漏网，只有种类数拦得住。
        #    ⚠️ 只加警告、**不改 ①② 的档次** —— 它们是既有硬错误，动它回归风险高。
        n_fam = len(cnt)
        need_fam = max(2, -(-n_skel // SKEL_KIND_DIV))
        if n_fam < need_fam:
            skel_warns.append(
                f'骨架只用了 {n_fam} 种（{"、".join("§" + f for f in sorted(cnt))}），'
                f'低于下限 {need_fam} 种（{n_skel} 页 ÷ {SKEL_KIND_DIV} 向上取整）—— '
                f'少数几族交替出现**同样单调**：占比可能不超 1/2、相邻也可能不同族，'
                f'正好绕过前两条判据。骨架有 19 种，换几族即可（见 ③册 §5.0 速查表）'
            )
        skel_line = (f'✅ 骨架分布：{n_skel} 个内容页 → '
                     + ' ｜ '.join(f'§{f} ×{c}' for f, c in cnt.most_common())
                     + f'\n   最高占比 {top_n / n_skel:.0%} ≤ {MAX_SHARE:.0%} ✓'
                       f'，相邻内容页无重复 ✓'
                       f'；种类 {n_fam} 种（下限 {need_fam}）')

    # ── ④-b 组件分布统计 + 「盒子千篇一律」检查（2026-10-16 新增）──
    # 丁（只报告）：把"你用了 N 次 C1"摆到模型眼前 = 给反馈。
    # 甲（判定）  ：连续 ≥3 个内容页 ⇒ 软警告（第一版不阻断）。
    #    占比 >1/2 已于 2026-10-17 **降级为观察项**（只在统计行标注，不进警告）。
    # 🔴 口径提醒：**"连续"按内容页序列算**，中间的 003 章节过渡页**不打断**。
    #    （曾按"页码相邻"算，以为最长只有 2 页 —— 错的，实测 C1 连续 5 个内容页。）
    comp_line = ''
    comp_warns = []
    if graphic_pages:
        all_ids = [i for _, ids in graphic_pages for i in ids]
        n_pg = len(graphic_pages)
        cnt_all = Counter(all_ids)
        _body = ' ｜ '.join(f'{k} ×{v}' for k, v in cnt_all.most_common())
        comp_line = (f'✅ 组件分布：{n_pg} 个内容页共登记 {len(all_ids)} 处'
                     + (f' → {_body}' if _body else '（本组没有任何组件编号）'))
        c_cnt = Counter(i for i in all_ids if i.startswith('C'))
        if c_cnt:
            top_c, top_n = c_cnt.most_common(1)[0]
            # 🔴 2026-10-18 口径修正：原来用「出现次数 ÷ 页数」当占比，一页多卡就会 >100%
            #   （实测 v4.13 打出 `C1 ×13/8 = 162%`，读起来像 bug）。改成**按覆盖页数**算：
            #   一页里 3 张 C1 仍记 1 页 ⇒ 占比天然 ≤100%，与「页内统一」的语义也对得上。
            top_pages = sum(1 for _, ids in graphic_pages if top_c in ids)
            share = top_pages / n_pg
            comp_line += (f'\n   承载组件（C 族）出现最多：{top_c} ×{top_n} 处 / 覆盖 {top_pages} 页'
                          f' = {share:.0%}（观察项，不判定；参考线 {CARRY_SHARE_MAX:.0%}）')
            # 🔻 2026-10-17 降级：「占比 >1/2」从判据改为**观察项**（只在上面统计行标注，不再进警告）。
            #   两轮实测：v4.1 C1 7/8=88% → v4.2 仍 7/8=88%，**报了零效果**；
            #   且这条本身可能误报 —— 多页确实是「图 + 一组要点」，用 C1 本来就对（C 族受内容形态约束）。
            #   保留它进警告列表只会稀释其余判据的可信度（模型会开始忽略警告）。
            #   CARRY_SHARE_MAX 仍被上面的统计行引用，作为参考线；要做硬约束时在此恢复判定即可。
            # 种类数下限 —— 兜住"少数几种交替"（占比不超线、连续不到 3 页时的漏网）
            n_kind = len(c_cnt)
            need_kind = max(2, -(-n_pg // CARRY_KIND_DIV))
            comp_line += f'；承载组件种类 {n_kind} 种（下限 {need_kind}）'
            if n_kind < need_kind:
                comp_warns.append(
                    f'承载组件只有 {n_kind} 种（{"、".join(sorted(c_cnt))}），低于下限 {need_kind} 种'
                    f'（{n_pg} 页 ÷ {CARRY_KIND_DIV} 向上取整）—— 少数几种交替出现**同样单调**：'
                    f'连续也可能不到 3 页，正好绕过连续那条判据。'
                    f'C 族不挑内容关系（只解决"内容怎么装"）⇒ 换它零风险，可换：'
                    f'C2 实心数据块 / C7 2×2 矩阵 / C9 斑马纹表格 / C10 编号条目行'
                    f'（取舍见 ③册 §5.0「骨架 × 组件 兼容表」）'
                )

        # ── ④-e 四族覆盖（2026-10-18 新增）──
        # ④-b 只数 C 族种类 ⇒ R/G/L 写不写都不影响通过。实测 8 份稿：C 111 处 / R 2 / G 1 / L 5，
        # R1~R4、R6、G2~G5 一次都没出现。这里给 R/G/L 一条机器后果：非 C 族处数有下限。
        fam_c = sum(1 for i in all_ids if i.startswith('C'))
        fam_r = sum(1 for i in all_ids if i.startswith('R'))
        fam_g = sum(1 for i in all_ids if i.startswith('G'))
        fam_l = sum(1 for i in all_ids if i.startswith('L'))
        nonc = fam_r + fam_g + fam_l
        need_nonc = max(NONC_MIN, -(-n_pg // NONC_DIV))
        comp_line += (f'\n   四族分布：C ×{fam_c} ｜ R ×{fam_r} ｜ G ×{fam_g} ｜ L ×{fam_l}'
                      f'；非 C 族 {nonc} 处（下限 {need_nonc}）')
        if nonc < need_nonc:
            comp_warns.append(
                f'非 C 族只用了 {nonc} 处（R ×{fam_r}、G ×{fam_g}、L ×{fam_l}），低于下限 {need_nonc} 处'
                f'（{n_pg} 页 ÷ {NONC_DIV} 向上取整）—— C 族只解决「内容怎么装」，'
                f'R 解决「关系怎么让人一眼看懂」、G 解决「常见信息怎么排」、L 解决「整页叙事」，'
                f'这三件事 C 族替代不了。'
                f'零风险补法（不挑内容关系，直接挂上就行）：'
                f'G1 键值参数行 / G2 标签胶囊行 / G3 脚注注释行 / G4 术语释义行 / G5 竖线三分行、'
                f'C4 压图图注 / C6 细线分隔 / C8 等级徽章 —— 写进「+」配件行（见 step_1 §1e）。'
                f'有对应关系时再上 R 族：占比⇒R1、单指标⇒R2、两项对照⇒R3、递进⇒R6、短标签⇒R5。'
                f'⚠️ 但**不要用脚注类配件硬凑数量** —— 优先看下面「形态识别」点名的页：'
                f'把那些页换成 L/R（一处顶五处），比加五个脚注有用。'
            )

        # ── ④-f 配件行合法性（2026-10-18 新增，配合 B）──
        # 配件行 = 区块表里第一列写 `+` 的行，给小配件一个独立落笔位（不占区块位）。
        # 三条约束：只挂配件类、每页 ≤2 行、必须排在区块行之后（否则会抢掉「该页首个组件」）。
        acc_ids_all = [i for _, ids, _n, _o in acc_pages for i in ids]
        if acc_pages:
            comp_line += f'\n   配件行：「+」行共 {len(acc_pages)} 页 / {len(acc_ids_all)} 处'
        for _pg, _ids, _n, _order_ok in acc_pages:
            if _n > ACC_MAX_PER_PAGE:
                comp_warns.append(
                    f'{_pg}: 配件行有 {_n} 行，超过上限 {ACC_MAX_PER_PAGE} 行 —— '
                    f'配件是点缀，堆多了会把正文挤掉'
                )
            _bad = [i for i in _ids if i not in ACCESSORY_IDS]
            if _bad:
                comp_warns.append(
                    f'{_pg}: 配件行里挂了 {"、".join(_bad)} —— 这些不属配件类，'
                    f'它们要占区块位（写成区块行），不能塞进「+」行。'
                    f'「+」行只接受：{"、".join(sorted(ACCESSORY_IDS))}'
                )
            if not _order_ok:
                comp_warns.append(
                    f'{_pg}: 「+」配件行排在了区块行前面 —— '
                    f'它会被当成该页的首个组件，影响「连续页」判定。配件行一律写在区块行之后'
                )
        # ── ④-g 形态识别：把「该用 R/L 却用了 C」点名到页（2026-10-18 新增）──
        # ④-e 是**配额式**（不管有没有素材都要 N 处）⇒ 实测模型加 3 个脚注类配件凑数，
        # 真正该换的 page14（2015→2020→2023→未来五年）一动没动。
        # 这里改成**条件触发**：只在这页内容里真的出现某种形态时，才要求对应组件。
        # 原则：没素材就不提 —— 不许为了达标硬套（②册明写「别为了用上新组件而套组件」）。
        for _pg, _ids in graphic_pages:
            _body = content_by_page.get(_pg, '')
            if not _body:
                continue
            _yrs = sorted(set(re.findall(r'(?:19|20)\d{2}', _body)))
            _pcts = re.findall(r'\d+(?:\.\d+)?\s*%', _body)
            _has_timeline = any(i.startswith('L') for i in _ids) or 'R6' in _ids
            _has_progress = 'R1' in _ids

            # 🔴 收紧（2026-10-18）：光看数量会误报 —— v4.10 page04 的「2025 年回收量 / 2030 年产生量」
            #   是**数据时点**，不是沿革，却被「4 个年份」判成了时间轴。所以加两个前提：
            #   ① 沿革：模型自己已经判定为「先后序列」（用了 C5 流程链）才提示升级；
            #   ② 构成：各百分比之和须 ≈100%，否则只是互不相关的几个指标。
            _time_like = 'C5' in _ids
            _nums = []
            for _x in _pcts:
                try:
                    _nums.append(float(re.sub(r'[^\d.]', '', _x)))
                except ValueError:
                    pass
            _uniq = sorted(set(_nums), reverse=True)
            _is_composition = (2 <= len(_uniq) <= 6) and (95 <= sum(_uniq) <= 105)

            # 形态①：≥3 个不同年份 **且已用流程链** ⇒ 沿革 / 分期（流程链只表先后，表不了逐级上升）
            if len(_yrs) >= 3 and _time_like:
                if not _has_timeline:
                    # 🔴 2026-10-18 由**警告升级为硬错误**：
                    #   v4.11 没素材（判据成立但无从改）；v4.12 模型自己选了 L1；
                    #   v4.13 被点名后**一动没动**（仍用 C2 + C5 + G3）。
                    #   三次里两次忽略 ⇒ 软警告在这条上没有约束力。
                    #   误报率：5 份稿共 3 条命中，全是真沿革页（港珠澳建造历程 / LNG 发展史 / 海上风电四阶段）。
                    comp_errs.append(
                        f'{_pg}: 「内容要点」里有 {len(_yrs)} 个年份（{"、".join(_yrs[:5])}）—— '
                        f'这是**沿革 / 分期**形态。当前用的是 {("、".join(sorted(set(_ids)))) or "无"}，'
                        f'`C5 流程链` 只能表达"先后"，表达不了"逐级上升"。'
                        f'改用 **L1 中轴交替时间轴 / L2 阶梯轴 / L3 阶段色带**（整页级，'
                        f'写进「版式骨架」为 `§5.5 数据块行（阶梯轴变体）` 这类变体），或 **R6 递增柱**'
                    )
            # 形态②：百分比之和 ≈100% ⇒ 构成 / 占比
            elif _is_composition:
                if not _has_progress:
                    comp_warns.append(
                        f'{_pg}: 「内容要点」里的百分比合计约 {sum(_uniq):.0f}%'
                        f'（{"、".join(_pcts[:4])}）—— 这是**构成 / 占比**形态，该用 **R1 进度条**'
                        f'（各项一条、长度按占比），当前用的是 {("、".join(sorted(set(_ids)))) or "无"}'
                    )
            # 形态③：恰好 1 个百分比 ⇒ 单指标头版
            # 形态③：整页只有一个百分比、且数字总量很少 ⇒ 单指标头版
            #   多加「数字 ≤3」：v4.10 page04 的「同比增长 32.9%」只是众多数字里的一个，
            #   不是"这页围绕它展开"，按单指标报会误导。
            elif len(_uniq) == 1 and len(re.findall(r'\d+(?:\.\d+)?', _body)) <= 3:
                if not _has_progress and 'R2' not in _ids:
                    comp_warns.append(
                        f'{_pg}: 只有一个百分比（{_pcts[0]}）且它就是这页的结论 —— '
                        f'这是**单指标**形态，该用 **R2 环形占比**（一个环 + 环心数字），'
                        f'当前用的是 {("、".join(sorted(set(_ids)))) or "无"}'
                    )

        # 连续 ≥3 个内容页用同一组件（取该页**首个**编号；一页多组件时以第一个为主）
        run_id = prev = None
        run_n = 0
        for pg, ids in graphic_pages:
            cur = ids[0] if ids else None
            run_n = run_n + 1 if (cur and cur == prev) else (1 if cur else 0)
            prev = cur
            if run_n == CARRY_RUN_MAX + 1 and cur:
                comp_warns.append(
                    f'{pg} 起连续 {run_n} 个内容页都用 {cur} —— 翻页观感会显平'
                    f'（阈值：连续 ≥{CARRY_RUN_MAX + 1} 页）'
                )

        # ── ④-c C1 款式：页内统一 + 页间换款（2026-10-17 新增）──
        # ④-b 只数组件编号 ⇒ 全篇 9 处 C1 仍是"1 种"，款式雷同完全看不见（实测过）。
        # 这里读括号里的 `框 X`，让两条铁律有机器后果：
        #   ① 同一页出现 ≥2 种框 ⇒ 页内不统一（会花）；
        #   ② 全篇只有 1 种框 ⇒ 页间没换款（翻页雷同）。
        if len(c1_variants) >= C1_VARIANT_MIN:
            by_page = {}
            for _pg, _fr, _ct in c1_variants:
                by_page.setdefault(_pg, []).append((_fr, _ct))
            # 页内统一：外框与内容款都不能混
            for _pg, _pairs in by_page.items():
                _frs = sorted(set(p[0] for p in _pairs))
                _cts = sorted(set(p[1] for p in _pairs if p[1]))
                if len(_frs) >= 2:
                    # 🔴 2026-10-18 由**警告升级为硬错误**：实测 v4.13 page10 两张卡用 B、两张用 C，
                    #   判据报了「页内必须统一」，模型照旧进了 Step 2（软警告被忽略）。
                    #   误报率：5 份稿共 2 条命中，全为真实的同尺寸混款（多尺寸页已放行）。
                    comp_errs.append(
                        f'{_pg} 一页里用了 {len(_frs)} 种 C1 外框（{"、".join(_frs)}）'
                        f'—— 同一页的 C1 必须用同一个组合（页内统一，见 ②册 C1 两条铁律）'
                    )
                if len(_cts) >= 2:
                    # 🔴 2026-10-18：**多尺寸页放行** —— 内容款与卡片尺寸是强耦合的：
                    #   内容 2 需 w≥384、内容 5 需 w≥300、内容 4 需 h≥124。
                    #   一页里「384×260 的三栏卡 + 1200×260 的通栏卡」是标准配方
                    #   （§5.2 每栏 2×260 + 通栏收口），硬要统一会逼出不合规的搭配 ——
                    #   实测把通栏也套内容 4（分条式），密度只有 47%、内容也显得空。
                    #   所以按「这页有几种卡尺寸」判：同尺寸才要求同款，多尺寸各选各的。
                    _sz = set((round(b[2]), round(b[3])) for b in boxes) if boxes else set()
                    if len(_sz) < 2:
                        comp_warns.append(
                            f'{_pg} 一页里用了 {len(_cts)} 种 C1 内容款（{"、".join(_cts)}）'
                            f'—— 这页的卡**尺寸相同**，必须用同一个组合（页内统一）'
                        )
            _kinds = sorted(set(v[1] for v in c1_variants))
            _ct_kinds = sorted(set(v[2] for v in c1_variants if v[2]))
            comp_line += (f'；C1 外框款式 {len(_kinds)} 种'
                          f'（{"、".join(_kinds)}）')
            if _ct_kinds:
                comp_line += f'；内容款 {len(_ct_kinds)} 种（{"、".join(_ct_kinds)}）'
            if len(_kinds) == 1:
                # 🔴 2026-10-18 由**警告升级为硬错误**。
                #   实测反作用：两条警告并存时，模型选「报得最少」的策略 ——
                #   统一成 1 种 ⇒ 只触发这 1 条；多样化但相邻撞车 ⇒ 可能触发 5 条。
                #   结果它主动 unify C1 frames，外框全变成 A。
                comp_errs.append(
                    f'全篇 {len(c1_variants)} 处 C1 都用了同一种外框（{_kinds[0]}）'
                    f'—— **页间没换款**。这条必须阻断：只要允许统一，模型就会选它（比相邻同款报得少）。'
                    f'②册 C1「外框 6 款」：A 左色条 / E 四角标 / F 顶部通宽线 / D L形框 / C 底部托底线 / B 顶部标头'
                )
            # 🔴 2026-10-18 新增：**单款上限** —— 光有种类数不够（A 用 5 页 + B 用 1 页也算 2 种）。
            #   ⚠️ 必须按「**出现在几页**」算，不能按「出现几次」：
            #   页内统一要求一页的 4 个卡同款 ⇒ 按次数算必然 >2，与页内统一直接打架（自测踩过）。
            _fr_page = Counter(_pairs[0][0] for _pairs in by_page.values())
            for _f, _n in _fr_page.most_common():
                if _n > C1_FRAME_MAX:
                    _dist = "、".join(f"{k}×{v}页" for k, v in _fr_page.most_common())
                    comp_warns.append(
                        f'外框 {_f} 出现在 {_n} 页（上限 {C1_FRAME_MAX} 页）—— 全篇虽有 {len(_kinds)} 种，'
                        f'但分布 {_dist} 太偏，翻页仍会显雷同。**每款最多出现在 {C1_FRAME_MAX} 页**'
                    )
            # 🔴 2026-10-17 补：写了 C1 但**没填内容款数字**（实测有人照抄占位符 `内容 Y`）
            _no_ct = sum(1 for _v in c1_variants if not _v[2])
            if _no_ct:
                comp_warns.append(
                    f'{_no_ct} 处 C1 没写内容款数字（写成 `内容 Y` 之类）—— '
                    f'内容款必须是 **1~5**：1 上下式 / 2 词条式 / 3 引领句 / 4 分条式 / 5 数字式。'
                    f'**不写数字等于没选内容款**，Step 2 只能按默认画（②册 C1「内容 5 款」）'
                )
            # 🔴 2026-10-17 新增：全篇只用了一种**内容款**（首测 10 处全是内容 1）
            if _ct_kinds and len(_ct_kinds) == 1:
                comp_warns.append(
                    f'全篇 {len(c1_variants)} 处 C1 都用了同一种内容款（内容 {_ct_kinds[0]}）'
                    f'—— **五款一款没用**。内容款要按这一格的**内容形态**挑：'
                    f'并列短句⇒4 分条式 / 有具体数字⇒5 数字式 / 标题短正文长⇒2 词条式 / '
                    f'连贯论述⇒3 引领句 / 标题+一段解释⇒1 上下式（②册 C1「内容 5 款」）'
                )
            # 🔴 2026-10-17 新增：**相邻内容页同款**（首测 page04/05 都用 B，旧判据漏掉）
            _seq = [(p, v[0][0], v[0][1]) for p, v in by_page.items()]
            for _i in range(1, len(_seq)):
                _p0, _f0, _c0 = _seq[_i - 1]
                _p1, _f1, _c1v = _seq[_i]
                if _f0 == _f1:
                    comp_warns.append(
                        f'相邻内容页 {_p0} 与 {_p1} 都用了外框 {_f0} —— '
                        f'**相邻页必须换款**（哪怕全篇有 3 种，连着两页同款照样显平）'
                    )
                if _c0 and _c0 == _c1v:
                    comp_warns.append(
                        f'相邻内容页 {_p0} 与 {_p1} 都用了内容 {_c0} —— 相邻页内容款也要换'
                    )

    if errors:
        print('❌ 字数超限:')
        for e in errors:
            print(f'  - {e}')
    if skel_errors:
        print('❌ 骨架分布不合格:')
        for e in skel_errors:
            print(f'  - {e}')
    if skel_warns:
        print('⚠️ 骨架分布警告（不阻断，但强烈建议改掉）:')
        for w in skel_warns:
            print(f'  - {w}')
    if layout_errs:
        print('❌ 布局框不合格:')
        for e in layout_errs:
            print(f'  - {e}')
    if layout_warns:
        print('⚠️ 布局框警告（不阻断，但建议改掉）:')
        for w in layout_warns:
            print(f'  - {w}')
    if comp_errs:
        print('❌ 组件款式不合格:')
        for e in comp_errs:
            print(f'  - {e}')
    if graphic_errs:
        print('❌ 图形元素列不合格:')
        for e in graphic_errs:
            print(f'  - {e}')
    if graphic_warns:
        print('⚠️ 组件编号警告（不阻断，但强烈建议改掉）:')
        for w in graphic_warns:
            print(f'  - {w}')
    if comp_warns:
        print('⚠️ 组件分布警告（不阻断，但强烈建议改掉）:')
        for w in comp_warns:
            print(f'  - {w}')
    if errors or skel_errors or layout_errs or graphic_errs or comp_errs:
        sys.exit(1)

    # 覆盖率报告
    summary = f'✅ 设计稿校验通过：扫描 {len(pages_seen)} 页 / 校验 {checked} 个占位符'
    if skipped:
        summary += f'，跳过 {len(skipped)} 个（内容含 ✓/≤）'
    if unverified:
        summary += f'，{len(unverified)} 个未取到值'
    print(summary)
    for label, items in (('跳过', skipped), ('未取到值', unverified)):
        if items:
            print(f'   {label}（最多列 8 条）：')
            for it in items[:8]:
                print(f'     - {it}')
    if skel_line:
        print(skel_line)
    if layout_ok or layout_none:
        extra = f'，另 {len(layout_none)} 页声明无框' if layout_none else ''
        if layout_warns:
            extra += f'，⚠️ {len(layout_warns)} 页收口偏浅'
        print(f'✅ 布局框：{len(layout_ok)} 页坐标自洽'
              f'（底边 ≤ {CANVAS_BOTTOM} / 同列间距 ≥ {MIN_GAP} / 左右分栏等高'
              f' / 收口 ≥ {BOTTOM_MIN} / 跨度 ≥ {SPAN_MIN} / 密度 ≥ {DENSITY_MIN:.0%}）{extra}')
    if graphic_ok or graphic_warns:
        extra = f'，⚠️ {len(graphic_warns)} 页待补' if graphic_warns else ''
        print(f'✅ 图形元素列：{len(graphic_ok)} 页已登记组件编号'
              f'（写法 `组件 <编号> <名字>`；编号见 ②册 §4.8）{extra}')
    if comp_line:
        print(comp_line)
    sys.exit(0)


if __name__ == '__main__':
    main()
