"""§0e-② 校验部分：验证提取输出 —— **纯校验，无副作用**。

> 表头旧写 `§0c-②` / `§0d-②` —— 那是**改动前的旧步骤号**（本文件由 §0e 执行）；
> 2026-10-14 随「本地素材提纯」一并订正为 `§0e-②`。

六项检查：
  ① `文本.md` 存在且非空
  ①-b **净正文非空**（2026-10-08 新增）—— 只有图片登记行时，提示转 §0d 联网取材
  ①-c **正文体量是否撑得住页数**（2026-10-08 新增，传入 `--pages` 时启用）
       —— 偏少则提示转 §0d **定点补内容**（不是失败）
  ①-d **本地段关系标注**（2026-10-14 新增）—— 有 `__来源本地 ·` 段、却一块
       `### [Ln] <关系> · …` 都没有 ⇒ §0c 漏标（只提示、不阻断）
  ② `image_*` 文件数量 > 0
  ③ `[[image_NNN.xxx]]` 占位符数量与图片一致
  ④ **图量是否够用**（可选，传入 `--pages` 时启用）—— 图太少则提示联网补图

**无图时只警告、不改数据** —— 补图交给 §0f 的 `fetch_web_images.py`。
（校验器不该有副作用：下载图片、改写 `文本.md` 都是写操作，归 0f。）

用法：
    python scripts/validators/verify_extraction.py                        # 只做 ①②③
    python scripts/validators/verify_extraction.py --pages 15 --facade 7  # 额外做 ①-c 正文体量 + ④ 图量
      └─ `--pages` 传 **PPT 总页数**（含门面页）；`--facade` 传**门面页数**（6 或 7）。
         期望图量 = (总页数 − 门面页) × 2；期望正文 = (总页数 − 门面页) × 375 字符。

🔑 **为什么门面页要由调用方传**：本脚本在 **素材准备** 运行，而门面页本该从
   `pipeline_config.json` 数出（003 过渡页张数 + 3）—— 但那是 **Step 2** 才生成的，
   素材准备时**根本不存在**。所以改由调用方**先定好再传**；
   只有在没传时才回退去读 config（通常读不到），最后按门面 7 页兜底。

🔵 **门面页 6 / 7 的含义**：门面页 = 3 + 章节数，而章节数只能是 3 或 4
   → **6 页 = 3 章**（目录用 `002_toc_3ch.svg`）；**7 页 = 4 章**（目录用 `002_toc.svg`）。
   `--chapters 3|4` 是**等价别名**（`facade = FACADE_FIXED + chapters`），两者都传时以 `--facade` 为准。

🔄 **门面页是一个调节旋钮**：总页数固定时，门面页 +1 → 内容页 −1 → 期望图量 −2。
   素材**主题冷门、网上搜不到足够图**时，可把 6 升到 7，用「内容页 −1」换「图需求 −2」。
"""
import json
import os
import re
import sys
from pathlib import Path

IMG_SUFFIXES = {'.jpeg', '.jpg', '.png', '.gif', '.webp', '.bmp'}

# ── 净正文判据（2026-10-08 新增）──
# `文本.md` 里的「图片登记段标题」与「[[image_NNN]] 登记行」**不算正文**。
# 净正文为空 / 极少 ⇒ 素材**只有图片、没有文字**（zip 里只放了照片）——
# 这种素材不能直接进 Step 1（内容页无字可写），应转 §0d 联网取材。
MIN_BODY_CHARS = 50
# 登记行可能带括注 —— `[[image_006.jpg]]（散图 | 目录：/ | 文件名：… | 内容：…）`
# 或 `[[image_001.jpeg]]（内嵌图 | 内容：…）`。锚定在行首即可整行剥掉
# （括注内容不参与匹配、也不影响剥除）。
IMG_ENTRY_RE = re.compile(r'^\[\[image_\d+\.\w+\]\]')
IMG_SECTION_RE = re.compile(r'^#{2,3}\s*(附加图片|联网补充图片)')

# 本地素材段（§0c 统一结构化 的产出）—— 有段却无标注 = 漏标。
LOCAL_SECTION_MARK = '__来源本地 · '
LOCAL_HEAD_RE = re.compile(r'(?m)^#{2,4}\s*\[L\d+\]')

# **关系标注行** —— 元信息、不是素材，与图片登记行同理不算正文。不剥会让正文体量
# 虚高，把「素材偏少」的告警压掉（体量判据是精确口径，必须干净）。
# · `[G…]` = 网络段，由 `emit_facts` 渲染（`### [G1] 构成 · …`）；
# · `[L…]` = 本地素材段，由 **§0c** 补标（`### [L1] 并列 · <板块名>`）。
# ⚠️ 与 emit_facts.GROUP_HEAD_TEMPLATE 同源，改一处必须改两处。
GROUP_HEAD_RE = re.compile(r'^\s*#{2,4}\s*\[(?:G|L)\d+\]')

# ── 正文体量评估参数（2026-10-08 新增）──
# 口径：期望正文 = 内容页数 × TEXT_PER_PAGE。
# 🔑 **TEXT_PER_PAGE = 200 是实测反推的**，不是拍脑袋：
#    2026-10-08 那份「奥特曼」deck（零素材联网取材）净正文 1,959 字符 / 8 内容页
#    ≈ 245 字符/页，成品 8 个内容页**全部通过** `check_creative_svg`（净字数下限 140/页）。
#    ⇒ 200 留了一点余量、又不会把"刚够"误报成"偏少"（245/200 = 122% ✓）。
# ⚠️ 定得过高会**误报**（真实可行的素材被劝去补内容），过低则形同虚设。
# 🔑 与 check_materials.py 的 SMALL_TEXT 同口径（8 × 200 × 80% = 1,280）——
#    **改动此值必须同步 check_materials.py**，否则两处参考线会漂移。
TEXT_PER_PAGE = 200
# 硬指标：不足即提示（不阻断 —— 由 §0d 定点补内容解决）。
# 🔄 2026-10-14：由 1.0 降到 0.8 —— 期望线自带余量（下游 check_creative_svg 硬下限仅
#    140 字/页 = 期望的 70%），按 100% 报「偏少」会把本来够用的素材误报，诱导模型回去
#    反复补料（实测：为补 215 字符重写整份 facts.txt，多花约 6 分钟）。
# 🔑 与 emit_facts.py 的 BODY_WARN_RATIO 同源（改一处必须改另一处）。
TEXT_ADEQUACY_THRESHOLD = 0.8

# ── 图量评估参数 ──
# 门面页构成：001 封面 + 002 目录 + N 张 003 章节过渡 + 010 致谢。
# N（章节数）从 pipeline_config.json 数实际张数，不写死。
FACADE_FIXED = 3          # 001 封面 + 002 目录 + 010 致谢
# 每个内容页期望的图片数。图片进的是**备选池**（不一定全部用上），
# 所以按上限 2 张/页给池子留足余量。
# ⚠️ 改动此值须同步 check_materials.py（0e 的粗筛参考线由它折算）。
IMG_PER_PAGE = 2
# 硬指标：不足即报警（留 0 容错 —— 备选池宁多勿少）。
ADEQUACY_THRESHOLD = 1.0
# 门面页的**可调范围**（= FACADE_FIXED + 章节数，而章节数只能 3 或 4）：
#   6 页 = 3 章（目录用 002_toc_3ch.svg）
#   7 页 = 4 章（目录用 002_toc.svg）
FACADE_MIN = FACADE_FIXED + 3   # 6
FACADE_MAX = FACADE_FIXED + 4   # 7
# 兜底：未传 --facade / --chapters 且读不到 config 时，按门面 7 页（4 章）算。
# 🔑 素材准备时 config 还没生成，正常路径应由调用方传 --facade（6 或 7）。
DEFAULT_FACADE = FACADE_MAX     # 7


def _parse_int_arg(argv, name):
    """解析 `--name N` / `--name=N` 两种写法；未出现则返回 None。"""
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            try:
                return int(argv[i + 1])
            except ValueError:
                print(f"❌ {name} 需要整数，收到 {argv[i + 1]!r}")
                sys.exit(1)
        if a.startswith(name + "="):
            try:
                return int(a.split("=", 1)[1])
            except ValueError:
                print(f"❌ {name} 需要整数，收到 {a!r}")
                sys.exit(1)
    return None


def parse_args():
    """返回 `(pages, chapters, facade)` —— 未出现的为 None。

    `--facade`（门面页 6|7）与 `--chapters`（章节数 3|4）是**同一件事的两种写法**：
    `facade = FACADE_FIXED + chapters`。两者都传时以 `--facade` 为准。
    """
    argv = sys.argv[1:]
    return (_parse_int_arg(argv, "--pages"),
            _parse_int_arg(argv, "--chapters"),
            _parse_int_arg(argv, "--facade"))


def _facade_desc(total_pages, facade, chapters):
    """统一的「共 N 页 = 门面 X 页（…具体构成…）+ 内容页 Y 页」描述串。"""
    return (f"共 {total_pages} 页 = 门面 {facade} 页"
            f"（001 封面 + 002 目录 + {chapters} 张 003 章节过渡 + 010 致谢）"
            f" + 内容页 {max(0, total_pages - facade)} 页")


def count_facade_pages(skill_root):
    """数门面页数量 = 001 封面 + 002 目录 + N 张 003 章节过渡 + 010 致谢。

    章节数 N 从 pipeline_config.json 数实际张数（不写死 4）。
    config 不存在或读不出来时，回退为「总页数未知」→ 让调用方按 7 页兜底。
    返回 (facade_count, chapter_count, source_desc)。
    """
    cfg = skill_root / "assets" / "pipeline_config.json"
    if not cfg.is_file():
        return None, None, "config 不存在"

    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
        pages = data.get("pages") or []
    except Exception as e:
        return None, None, f"config 解析失败({e.__class__.__name__})"

    if not pages:
        return None, None, "config 无 pages"

    chapters = sum(
        1 for p in pages
        if str(p.get("template", "")).startswith("003")
    )
    total = len(pages)
    facade = FACADE_FIXED + chapters
    return facade, chapters, _facade_desc(total, facade, chapters)


def resolve_facade(skill_root, facade_arg, chapters_arg):
    """按**统一优先级**解析门面页数，返回 (facade, chapters)。

    优先级：`--facade` > `--chapters`（等价别名）> config 实读 > 兜底 7 页。

    🔑 ①-c（正文体量）与 ④（图量）**必须用同一个门面页**，否则同一条命令会给出
       两套内容页数、两个"缺口"，用户无从判断。所以两处都调本函数。
    """
    if facade_arg is not None:
        return facade_arg, facade_arg - FACADE_FIXED
    if chapters_arg is not None:
        return FACADE_FIXED + chapters_arg, chapters_arg
    facade, chapters, _ = count_facade_pages(skill_root)
    if facade is None:
        return DEFAULT_FACADE, DEFAULT_FACADE - FACADE_FIXED
    return facade, chapters


def body_only(text: str) -> str:
    """剥掉「图片登记段 / 登记行 / 空行 / 分隔线」，返回**净正文**。

    背景（2026-10-08 实测）：zip 里只放图片时，`extract_all.py` 依然会往
    `文本.md` 写一段「## 附加图片」+ 若干 `[[image_NNN]]（散图 | …）` 登记行，
    于是 `content.strip()` **非空** —— 原来的「文本为空」判据**永远不触发**，
    素材就带着"没有正文"的状态一路跑到 Step 1 才炸（且报的错看不出根因）。
    """
    kept = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s == '---':
            continue
        if IMG_SECTION_RE.match(s) or IMG_ENTRY_RE.match(s):
            continue
        if GROUP_HEAD_RE.match(s):
            continue
        kept.append(line)
    return '\n'.join(kept)


def main():
    skill_root = Path(os.getcwd())
    sources = skill_root / "assets" / "sources"
    md_path = sources / "文本.md"
    pages, chapters_arg, facade_arg = parse_args()

    # ── ⓪ 参数兜底（2026-10-08 新增）──
    # `--pages` 是可选参数 ⇒ 不传就【静默】跳过图量评估，§0f 也因此算不出缺口张数。
    # 这种"沉默降级"事后极难发现，所以在这里显式提示一句，把沉默变成可见。
    if not pages:
        print("⚠️ 未传 --pages（PPT 总页数）⇒ 图量评估已跳过")
        print("   本 skill 的缺省规模 = 15 页 / 4 章：调用方没给页数时，应传 --pages 15 --facade 7")
        print("   不传就评估不了图量，§0f 也算不出缺口张数 —— 建议补上再跑一次")
        print()

    # ── ① 文本.md ──
    if not md_path.is_file():
        print("❌ 验证失败: 文本.md 不存在")
        sys.exit(1)

    content = md_path.read_text(encoding="utf-8")
    if not content.strip():
        print("❌ 验证失败: 文本.md 为空（连图片登记都没有）")
        sys.exit(1)
    print(f"✅ 文本.md: {len(content)} 字符")

    # ── ①-b 净正文（排除图片登记行）── 🔴 2026-10-08 新增
    # 为什么要有这一条：`文本.md` 只要含图片登记行就 `strip()` 非空，
    # 于是「只有图片」的素材会被误判为"正常"，一路跑到 Step 1 才炸。
    body = body_only(content)
    if len(body.strip()) < MIN_BODY_CHARS:
        n_img = len([
            f for f in sources.iterdir()
            if f.is_file() and f.name.startswith("image_")
            and f.suffix.lower() in IMG_SUFFIXES
        ])
        print(f"⚠️ 正文: 仅 {len(body.strip())} 字符（其余都是图片登记行）"
              f" —— 这份素材【只有图片、没有文字】，图片 {n_img} 张")
        print()
        print("🔴 这不是失败，是「无文字素材」分支 —— 与「零素材」同路：")
        print("   ⇒ 转 §0d **联网取材**补齐文字内容；图片保留，作实拍配图用（优先于网图）。")
        print("   · 提示词里已有主题 → 直接用该主题检索；")
        print("   · 没有主题 → 看图片内容 / 原始文件名推断主题；推不出才终止（见 §0h）。")
        print("   ⚠️ 别带着这份没有正文的素材进 Step 1 —— 内容页会无字可写，")
        print("      而且到那时报的是「内容量不足」，看不出根因。")
        sys.exit(0)

    # ── ①-c 正文体量评估（仅传入 --pages 时）── 🔴 2026-10-08 新增
    # 为什么要有这一条：④ 只管「图够不够」，正文体量一直没人管 —— 素材偏薄时会
    # 一路走到 Step 1，卡在「内容量不足 / 净字数不够」，报的错完全看不出根因是"料不够"。
    # 这里按目标页数提前量一句 ⇒ 不足就转 §0d 定点补缺口（补内容），而不是硬做。
    if pages:
        facade_t, _ = resolve_facade(skill_root, facade_arg, chapters_arg)
        content_pages_t = max(0, pages - facade_t)
        need_text = content_pages_t * TEXT_PER_PAGE
        have_text = len(body.strip())
        if need_text > 0:
            ratio_t = have_text / need_text
            print()
            print(f"📊 正文体量评估: {pages} 页 = 门面 {facade_t} + 内容页 {content_pages_t}")
            print(f"   内容页 {content_pages_t} 页 × {TEXT_PER_PAGE} 字符/页 → "
                  f"期望正文 {need_text:,} 字符，现有 {have_text:,} 字符，达成率 {ratio_t:.0%}")
            if ratio_t < TEXT_ADEQUACY_THRESHOLD:
                floor_t = int(need_text * TEXT_ADEQUACY_THRESHOLD)
                print(f"⚠️ 正文偏少（达成率 {ratio_t:.0%} < 放行线 "
                      f"{TEXT_ADEQUACY_THRESHOLD:.0%} = {floor_t:,} 字符）"
                      f"—— 缺口约 {floor_t - have_text:,} 字符")
                print("   ⇒ 这份素材撑不满目标页数，硬做只能「少讲」或「注水」"
                      "（同属 sufficiency test 的缺口，不是失败）：")
                print("      转 §0d 定点补缺口补内容；或把页数往下调。")
                print("   （口径：正文 = 文本.md 去掉图片登记行后的净正文）")
            else:
                print(f"✅ 正文体量与目标页数相称（达成率 {ratio_t:.0%} ≥ 放行线 "
                      f"{TEXT_ADEQUACY_THRESHOLD:.0%}）")

    # ── ①-d 本地段关系标注（§0c 统一结构化 的产出）── 🔴 2026-10-14 新增
    # 本地素材经 §0c 重组为「关系块 + 要点」后，正文里应出现 `__来源本地 ·` 段、
    # 且每块带一行 `### [Ln] <关系> · <块名>`。**一块都没有 = 漏标** —— 那正是
    # 「关系驱动的组件（R1~R6 / L1~L3）系统性用不上」的病根（见 pitfalls.md）。
    # 只提示、不阻断：它是「质量信号」，不是「数据损坏」（与 ①-c 的正文偏少同级）。
    if LOCAL_SECTION_MARK in content:
        n_local_heads = len(LOCAL_HEAD_RE.findall(content))
        if n_local_heads == 0:
            print("⚠️ 本地段（__来源本地 ·）没有任何 `### [Ln] <关系> · <块名>` 标注")
            print("   ⇒ §0c 漏标了 —— 下游只能退回默认「并列」，R1 / R3 / R6 / L1~L3 触发不了")
            print("      （回 §0c 重跑：清单须带 `#GROUP Ln | <关系> | <块名>` 行）")
        else:
            print(f"✅ 本地段关系标注: {n_local_heads} 块")

    # ── ② 图片数量 ──
    imgs = sorted([
        f for f in sources.iterdir()
        if f.is_file() and f.name.startswith("image_")
        and f.suffix.lower() in IMG_SUFFIXES
    ])

    if not imgs:
        print("⚠️ 图片: 0 张 —— 素材里没有任何图片")
        print()
        print("🔌 联网补图缺省即执行（2026-10-08 起缺省 ON；仅提示词显式写 [联网搜图：OFF] 才不补）。")
        print("   若提示词写了 OFF：忽略以下建议，Step 1 全内容页走无图骨架（契约 §5.10.1 等）。")
        print("   ── 开关为 ON（缺省）时，直接运行以下命令：")
        print('   python scripts/fetch_web_images.py --keywords "主题A;主题B;主题C" --count 12')
        print("   （先通读 文本.md 定 3~5 组主题词，多组用 ; 分隔；提不出主题就跳过，内容页走纯图形/表格布局）")
        sys.exit(0)

    print(f"✅ 图片: {len(imgs)} 张")
    for i in imgs:
        print(f"    {i.name}")

    # ── ③ 占位符一致性 ──
    ph = re.findall(r'\[\[image_\d+\.\w+\]\]', content)
    if len(ph) != len(imgs):
        print(f"❌ 验证失败: 占位符 {len(ph)} 个 ≠ 图片 {len(imgs)} 张")
        ph_names = {re.match(r'\[\[(.+?)\]\]', p).group(1) for p in ph}
        img_names = {i.name for i in imgs}
        for missing in sorted(img_names - ph_names):
            print(f"    缺占位符: {missing}")
        for extra in sorted(ph_names - img_names):
            print(f"    无对应文件: {extra}")
        sys.exit(1)

    print(f"✅ 占位符: {len(ph)} 个 (格式正确，与图片一一对应)")

    # ── ④ 图量评估（仅传入 --pages 时） ──
    if pages:
        # 门面页来源优先级（门面页 = 3 + 章节数，章节数只能 3 或 4）：
        #   ① --facade 由调用方传入（6 或 7）—— **素材准备的正常路径**（此时 config 尚未生成）
        #   ② --chapters 由调用方传入（等价别名：facade = FACADE_FIXED + chapters）
        #   ③ 从 pipeline_config.json 数 003 过渡页张数 + 3（config 已存在时才可能）
        #   ④ 兜底门面 7 页（= 4 章）
        requested = facade_arg if facade_arg is not None else (
            FACADE_FIXED + chapters_arg if chapters_arg is not None else None)

        if requested is not None:
            # 与 ①-c 共用同一份解析逻辑（避免两处口径漂移）
            facade, chapters = resolve_facade(skill_root, facade_arg, chapters_arg)
            if not (FACADE_MIN <= facade <= FACADE_MAX):
                print(f"   ⚠️ 门面页 {facade} 超出常规范围 {FACADE_MIN}~{FACADE_MAX}"
                      f"（= 3~4 章）—— 仍按该值评估，请确认页面结构规划")
            desc = _facade_desc(pages, facade, chapters)
            # 若 config 恰好已存在，交叉核对一句（不一致时仍以传入值为准）
            _cf, cfg_chapters, _ = count_facade_pages(skill_root)
            if cfg_chapters is not None and cfg_chapters != chapters:
                print(f"   ⚠️ 传入的门面页 {facade}（{chapters} 章）"
                      f"与 config 实际的 {cfg_chapters} 章不一致"
                      f"（素材准备阶段通常无 config，此处以传入值为准）")
        else:
            facade, chapters, desc = count_facade_pages(skill_root)
            if facade is None:
                orig = desc
                facade = DEFAULT_FACADE
                chapters = facade - FACADE_FIXED
                desc = _facade_desc(pages, facade, chapters)
                print(f"   ⚠️ 未传 --facade / --chapters，且无法从 config 数章节数（{orig}）"
                      f" → 门面页按 {facade} 页（{chapters} 章）兜底")
                print(f"   💡 建议传入 --facade {FACADE_MIN} 或 {FACADE_MAX}"
                      f"（{FACADE_MIN} = 3 章；{FACADE_MAX} = 4 章）以精确评估")

        content_pages = max(0, pages - facade)
        need = max(1, content_pages * IMG_PER_PAGE)
        ratio = len(imgs) / need
        print()
        print(f"📊 图量评估: {desc}")
        print(f"   内容页 {content_pages} 页 × {IMG_PER_PAGE} 张/页 → 期望图量 {need} 张"
              f"（备选池，不一定全用），现有 {len(imgs)} 张，达成率 {ratio:.0%}")
        if ratio < ADEQUACY_THRESHOLD:
            gap = need - len(imgs)
            print(f"⚠️ 图量不足（达成率 {ratio:.0%} < {ADEQUACY_THRESHOLD:.0%}）—— 缺口 {gap} 张")
            print("    🔌 缺省 ON  → 按缺口补图（下方命令）；")
            print("    🔌 显式 OFF → 这是预期行为：不补图，Step 1 按现有图量规划（图少用无图骨架）。")
            print("   ── 开关为 ON（缺省）时，直接运行以下命令：")
            print(f"   python scripts/fetch_web_images.py --keywords \"主题A;主题B;主题C\" --count {gap}")
            print(f"   （--count 按缺口 {gap} 张传；先通读 文本.md 定 3~5 组主题词）")
            # 💡 门面页调节旋钮：总页数固定时，门面 +1 → 内容页 −1 → 期望图量 −2
            if facade < FACADE_MAX:
                alt = facade + 1
                alt_content = max(0, pages - alt)
                alt_need = max(1, alt_content * IMG_PER_PAGE)
                saved = need - alt_need
                print(f"   💡 调节旋钮：门面页 {facade} → {alt}"
                      f"（内容页 {content_pages} → {alt_content}）"
                      f"→ 期望图量 {need} → {alt_need}（省 {saved} 张）")
                print(f"      适用：素材主题冷门、网上搜不到足够图 ——"
                      f"用「内容页 −1」换「图需求 −{saved}」")
            elif facade == FACADE_MAX:
                print(f"   💡 门面页已是上限 {FACADE_MAX}（4 章），没有更省的调法了")
        else:
            print(f"✅ 图量充足（达成率 {ratio:.0%} ≥ {ADEQUACY_THRESHOLD:.0%}），无需补图")

    sys.exit(0)


if __name__ == "__main__":
    main()
