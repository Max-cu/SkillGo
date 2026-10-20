"""
Step 2e-② JSON 格式 + 字段完整性 + 页序结构校验 —— 六合一。

① JSON 语法验证（json.load 自动检查）
② 顶层结构：必须存在**非空**的 pages[] 数组
③ pages[] 中每页必须包含 id/template/output/text 四个必填字段
④ template 值必须存在于 templates/（规范化后）
⑤ 009 创作页必须带 body_svg，且片段文件必须存在于 svg_body/
⑥ 页序结构：封面第一 / 致谢最后 / 目录紧跟封面 /
   每个 003_toc（章节过渡页）之后必须有该章的内容页（⑥-3）
   + 过渡页的紧邻下一页必须是内容页（⑥-5）
   + 目录声明的章数必须等于实际 003_toc 张数（⑥-4，堵"改 template 绕过检查"）

🔴 ② 是防「假通过」的闸门：pages 缺失或为空时，③④⑤ 的逐页检查会全部空转、
   统统打印 ✅，把问题一路放行到 build 才崩（KeyError: 'pages'）。
   典型触发场景：JSON 顶层键名写错（如写成 "slides"）、或整个 pages 数组漏写。

🔴 ⑥ 是**唯一的"结构级"检查**：其余各项都只看"单页合不合格"，
   于是"章节过渡页全被堆到末尾"这种整份片子逻辑错乱的问题，
   原先会一路交付到用户手里（2026-09-27 实测踩中）。
"""
import json, sys, os

# 常见模板名误写 → 正确名（用于给出针对性提示）
TEMPLATE_ALIASES = {
    '009_creative': '009_content',
}


def check_page_order(pages):
    """页序结构检查（⑥）—— 返回 ``(问题列表, 各章内容页数)``；空列表 = 通过。

    抽成模块级函数是为了**能脱离完整 config 单独测**：喂一串 template 序列即可验证
    判据拦不拦得住「整章无内容页」与「把过渡页 template 改掉以绕过检查」这两种改法。

    为什么必须有结构级检查：此前没有任何校验器看"页序" —— check_required_slots /
    check_design_spec / build_svgs 全都只判断"单页合不合格"，于是"章节过渡页全被堆到末尾"
    这类**整份片子逻辑错乱**的问题会一路交付到用户手里（2026-09-27 实测踩中）。
    """
    seq = [os.path.splitext(str(p.get('template', '')))[0] for p in pages]
    errors = []
    per_chapter = []

    # ⑥-1 首尾固定：封面第一、致谢最后
    if seq and seq[0] != '001_cover':
        errors.append(f"首页是 {seq[0]}，应为 001_cover（封面）")
    if seq and seq[-1] != '010_ending':
        errors.append(f"末页是 {seq[-1]}，应为 010_ending（致谢）")

    # ⑥-2 目录页必须紧接封面
    if len(seq) >= 2 and seq[1] not in ('002_toc', '002_toc_3ch'):
        errors.append(f"第 2 页是 {seq[1]}，应为 002_toc / 002_toc_3ch（目录）")

    # ⑥-3 每个章节过渡页之后、下一个过渡页之前，必须至少有一个内容页
    toc_idx = [i for i, t in enumerate(seq) if t == '003_toc']
    if not toc_idx:
        errors.append('没有任何 003_toc（章节过渡页）—— 每章应各有一张')
    for k, i in enumerate(toc_idx):
        nxt = toc_idx[k + 1] if k + 1 < len(toc_idx) else len(seq)
        n_content = sum(1 for t in seq[i + 1:nxt] if t == '009_content')
        per_chapter.append(n_content)
        if n_content == 0:
            errors.append(
                f"第 {i + 1} 页的 003_toc（第 {k + 1} 章过渡）之后没有任何内容页 —— "
                '章节过渡页必须紧跟该章的内容页'
            )

    # ⑥-4 目录声明的章数 ↔ 实际 003_toc 张数
    # 为什么单靠 ⑥-3 不够：把某一章的过渡页 template 改成别的类型，它就从 ⑥-3 的视野里
    # 消失、那条判据随之失效，而目录页仍承诺 N 章。2026-10-13 实测踩中 —— 交付物里
    # 第 3 章整章无内容页（它的过渡页紧邻第 4 章过渡页），目录却写着 4 章，全程无人报警。
    declared = {'002_toc': 4, '002_toc_3ch': 3}.get(seq[1] if len(seq) > 1 else '')
    if declared is not None and len(toc_idx) != declared:
        errors.append(
            f"目录页是 {seq[1]}（声明 {declared} 章），但片子里只有 {len(toc_idx)} 个 "
            '003_toc —— 每章应各有一张过渡页；缺的那章要么被删、'
            '要么 template 被改成了别的类型（后者会绕过页序检查）'
        )

    # ⑥-5 章节过渡页的**紧邻下一页**必须是内容页（比 ⑥-3 的"之间"更严）。
    # ⑥-3 只要求"这一段里有内容页"，两个过渡页相邻时才会报警；本判据把"紧邻"也钉死，
    # 使"把内容页往前挪、空出一整章"这类改法无法蒙混过关。
    for k, i in enumerate(toc_idx):
        if i + 1 < len(seq) and seq[i + 1] != '009_content':
            errors.append(
                f"第 {i + 1} 页的 003_toc（第 {k + 1} 章过渡）的下一页是 {seq[i + 1]}，"
                '不是 009_content（内容页）—— 章节过渡页必须紧邻该章内容页'
            )

    return errors, per_chapter


def main():
    skill_root = os.getcwd()
    config_path = os.path.join(skill_root, 'assets', 'pipeline_config.json')

    if not os.path.isfile(config_path):
        print(f'❌ pipeline_config.json 不存在: {config_path}')
        sys.exit(1)

    # ── ① JSON 语法验证 ──
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        print(f'❌ JSON 语法错误: {e}')
        print('   检查：中文引号 → 「」、字符串值内未转义的 "、尾随逗号、单引号')
        sys.exit(1)

    print('① JSON 语法: ✅')

    # ── ② 顶层结构（防假通过的闸门）──
    if not isinstance(cfg, dict):
        print(f'② 顶层结构: ❌ 根节点必须是对象，当前是 {type(cfg).__name__}')
        sys.exit(1)
    if 'pages' not in cfg:
        print(f"② 顶层结构: ❌ 缺少 'pages' 键（顶层现有键: {sorted(cfg.keys())}）")
        print('   正确结构: {"_meta": {"project": "...", "author": "..."}, "pages": [ ... ]}')
        sys.exit(1)

    pages = cfg['pages']
    if not isinstance(pages, list) or not pages:
        n = len(pages) if isinstance(pages, list) else 0
        print(f"② 顶层结构: ❌ 'pages' 必须是非空数组（当前: {type(pages).__name__}，{n} 项）")
        print('   ⚠️ 空 pages 会让后续逐页检查全部空转、打印假 ✅')
        sys.exit(1)

    print(f'② 顶层结构: ✅ (pages 存在，共 {len(pages)} 页)')

    # ── ③ pages[] 字段完整性 ──
    required = ('id', 'template', 'output', 'text')
    missing = [str(p.get('id', '?')) for p in pages if not all(k in p for k in required)]

    if missing:
        print(f'③ 字段完整性: ❌ 以下页面缺少必填字段 (id/template/output/text): {missing}')
        sys.exit(1)

    print(f'③ 字段完整性: ✅ ({len(pages)} 页齐全)')

    # ── ④ template 值合法性校验（防止写错模板名闯到 build 才崩）──
    # 目录结构（2026-10-09）：门面页在 assets/templates/<风格名>/，009 在 assets/templates/ 根。
    templates_root = os.path.join(skill_root, 'assets', 'templates')
    meta = cfg.get('_meta', {}) or {}
    # 🔑 共用 apply_palette.resolve_style_name()（方案 C：未显式声明时从 palette 派生）
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from apply_palette import resolve_style_name
    style_name, _derived = resolve_style_name(
        meta, palette_name=meta.get('palette'), warn=False)
    style_dir = os.path.join(templates_root, style_name)

    valid_root = set()
    if os.path.isdir(templates_root):
        valid_root = {f for f in os.listdir(templates_root)
                      if f.endswith('.svg') and os.path.isfile(os.path.join(templates_root, f))}
    valid_style = set()
    if os.path.isdir(style_dir):
        valid_style = {f for f in os.listdir(style_dir)
                       if f.endswith('.svg') and os.path.isfile(os.path.join(style_dir, f))}

    bad = []
    for p in pages:
        tpl = p.get('template', '')
        base, ext = os.path.splitext(tpl)
        normalized = (base + '.svg') if ext != '.svg' else tpl
        # 009 从根取，门面页从风格目录取
        pool = valid_root if normalized == '009_content.svg' else valid_style
        if normalized not in pool:
            hint = ''
            if base in TEMPLATE_ALIASES:
                hint = (f'\n     ← 注意: design_spec.md 里叫 "{base}"，但模板文件名是 '
                        f'"{TEMPLATE_ALIASES[base]}.svg"')
            elif not os.path.isdir(style_dir):
                hint = (f'\n     ← 风格目录不存在: assets/templates/{style_name}/'
                        f'（_meta.template_set 写的风格名要真实存在）')
            bad.append(f"{p.get('id', '?')}: '{tpl}'{hint}")

    if bad:
        avail = sorted(valid_style | valid_root)
        print(f'④ template 值: ❌ 以下页面 template 不合法（可用: {", ".join(avail)}）:')
        for b in bad:
            print(f'   - {b}')
        sys.exit(1)

    print(f'④ template 值: ✅ ({len(pages)} 页合法；'
          f'门面页风格 {style_name}，009 取自模板根)')

    # ── ⑤ 009 创作页的 body_svg（字段 + 片段文件存在性）──
    # 原先没有任何校验器查这里：缺 body_svg 会一路闯到 build 才报
    # ValueError("009 创作页缺少 body_svg 字段")。
    svg_body_dir = os.path.join(skill_root, 'assets', 'svg_body')
    problems = []
    n_009 = 0
    for p in pages:
        tpl = p.get('template', '')
        if os.path.splitext(tpl)[0] != '009_content':
            continue
        n_009 += 1
        bs = (p.get('body_svg') or '').strip()
        if not bs:
            problems.append(f"{p.get('id', '?')}: 缺少 body_svg 字段")
        elif not os.path.isfile(os.path.join(svg_body_dir, bs)):
            problems.append(f"{p.get('id', '?')}: body_svg 指向的片段不存在 '{bs}'")

    if problems:
        print('⑤ 009 页 body_svg: ❌')
        for x in problems:
            print(f'   - {x}')
        print('   （片段应放在 assets/svg_body/ 下，文件名与 body_svg 字段值一致，如 page04.svg）')
        sys.exit(1)

    print(f'⑤ 009 页 body_svg: ✅ ({n_009} 个片段齐全)')

    # ── ⑥ 页序结构（判据本体见模块级 check_page_order，便于脱离完整 config 单测）──
    order_errors, per_chapter = check_page_order(pages)
    if order_errors:
        print('⑥ 页序结构: ❌')
        for x in order_errors:
            print(f'   - {x}')
        print('   正确结构: 封面 → 目录 → 【第1章: 过渡页 + 内容页×N】→ 【第2章: …】→ … → 致谢')
        print('   ⚠️ 最常见错误：内容页全部连排、把章节过渡页堆在末尾；'
              '或某章只留过渡页、内容被挪进上一章')
        if per_chapter:
            print(f'   各章内容页数: {per_chapter}（0 = 该章是空壳）')
        sys.exit(1)

    print(f'⑥ 页序结构: ✅ ({len(per_chapter)} 个章节过渡页均插在该章内容之前；'
          f'各章内容页数 {per_chapter})')

    sys.exit(0)


if __name__ == '__main__':
    main()
