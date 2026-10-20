#!/usr/bin/env python3
"""
build_svgs.py — Step 3: 从 pipeline_config.json 生成 SVG（含 XML 兼容性修复 + 配色替换）

读取 assets/pipeline_config.json，遍历 pages[]：
  1. 复制模板 SVG（assets/templates/）→ assets/svg_output/
  2. 用 text{} 中的值替换 {{PLACEHOLDER}}
  3. 清理残留占位符
  4. 修正模板底图路径 → assets/images/<basename>
  5. XML 兼容性修复（目录页 <text> 裸 & 补转义、根 <svg> 的 native-ref 清理）
  6. 🎨 配色替换（读 _meta.palette，默认 default=商务风）—— **只作用于创作页**
     ⚠️ 只有 template 为 009_content.svg 的页会走配色替换；门面页一律原样输出。

配色说明（2026-10-08 接入 / 2026-10-09 收窄）：
  * 方案名取自 config 的 `_meta.palette` 字段；缺省 → "default"（商务风，= 模板原值，
    替换后与原样完全一致 ⇒ 行为向后兼容）。
  * 真正的替换逻辑复用 `apply_palette.py`，本脚本不重复实现。
  * 🔴 仍遵守「不动模板」原则：只读 assets/templates/，只写 assets/svg_output/。
  * 🔴🔴 **2026-10-09 收窄：配色只服务创作页（009）**。
    理由：门面页（001/002/003/010）的风格本质是**整张版式设计**（底图 + 光效 + 构图），
    不是几个十六进制色值 —— 想给门面页换风格，正确做法是**换一整套门面页模板**
    （`assets/templates/<风格名>/`），而不是给旧模板刷漆。
    ⇒ 门面页跳过配色替换，永远原样输出。
  * 🔵 **无效方案名**（文件不存在，如前端拼错成 `pink`）→ **回退 default + 醒目告警**、
    **不中断流程**（2026-10-08 用户拍板）。
  * 🔴 **角色缺色** → 报错退出（不静默降级，避免"以为换了其实没换"）—— 那是**配置损坏**，不是用户输入问题。

模板目录结构（2026-10-09 新增）：
  * 🔑 **009 创作页在根，门面页在风格子目录** —— 两者不是一个层级：
      assets/templates/
        ├── 009_content.svg            ← 共享（所有风格都用它）+ 走配色
        ├── native_payloads.json.gz    ← 009 页头回填（路径写死）
        └── <风格名>/                  ← 只装门面页
              ├── 001_cover.svg
              ├── 002_toc.svg
              ├── 002_toc_3ch.svg
              ├── 003_toc.svg
              └── 010_ending.svg
  * 当前唯一风格：`default`（用户默认路由到此）。加新风格 = 新建一个兄弟目录、放同名门面页。
  * 风格名取自 `_meta.template_set`（缺省 `default`）⇒ 向后兼容（旧 config 无此字段）。
  * 🔴 009 **不在**风格目录里 —— 它的配色替换与风格无关，永远从根取、永远走配色。

使用方式:
  python scripts/build_svgs.py
  python scripts/build_svgs.py --no-palette      # 跳过配色，输出模板原生色
"""

import json, os, sys, shutil, re
import argparse
from pathlib import Path

# ─── 目录定位 ───
PROJECT = Path(__file__).resolve().parent.parent             # skill 根目录
TEMPLATES_ROOT = PROJECT / "assets" / "templates"            # 模板根（内含 <风格名>/ 子目录）
TEMPLATES = TEMPLATES_ROOT / "default"                       # 当前风格目录（由 resolve_template_set 决定）
IMAGES = PROJECT / "assets" / "images"                       # 底图
BODY_DIR = PROJECT / "assets" / "svg_body"                   # 009 创作页正文片段
SVG_OUT = PROJECT / "assets" / "svg_output"
CONFIG_PATH = PROJECT / "assets" / "pipeline_config.json"

# 🔴 配色替换只作用于这个模板（2026-10-09 用户拍板）
PALETTE_TEMPLATE = '009_content.svg'

DEFAULT_TEMPLATE_SET = 'default'

os.makedirs(SVG_OUT, exist_ok=True)

# 配色模块（同目录，复用其替换逻辑，避免两份实现漂移）
sys.path.insert(0, str(Path(__file__).resolve().parent))


# ═══════════════════════════════════════════════════════════════
# 图片路径修正
# ═══════════════════════════════════════════════════════════════

def fix_image_paths(svg_text):
    """将所有图片引用路径修正为 ../images/<basename>。

    从 assets/svg_output/ 的视角，../images/ 指向 skill 的 assets/images/。
    """
    def replace_href(m):
        full = m.group(0)
        attr_match = re.match(r'(xlink:)?href=(["\'])(.*?)\2', full, re.DOTALL)
        if not attr_match:
            return full
        quote = attr_match.group(2)
        old_path = attr_match.group(3)

        if old_path.startswith('data:'):
            return full

        basename = os.path.basename(old_path)
        if not os.path.exists(os.path.join(IMAGES, basename)):
            return full

        new_path = f'../images/{basename}'
        return f'href={quote}{new_path}{quote}'

    svg_text = re.sub(r'(?:xlink:)?href=(["\'])(?!data:)(.*?)\1', replace_href, svg_text)
    return svg_text


# ═══════════════════════════════════════════════════════════════
# XML 兼容性修复（已合并进 build）
# ═══════════════════════════════════════════════════════════════

# 目录页：<text> 里的裸 & 需补转义（四栏版与三栏版同源，都要处理）
AMP_TEMPLATES = ('002_toc.svg', '002_toc_3ch.svg')


def fix_xml_compatibility(svg_text, template_name):
    """修复 XML 兼容性问题。

    - 目录页（002_toc）：<text> 里的裸 & 补转义（build 已对占位符值做 xml_escape，
      这里只是兜底）。
    - 任意页：若**根** <svg> 标签上残留 data-pptx-native-ref，就地清理。
      🔴 必须只取文档里第一个 <svg>（根标签）。用全文 re.sub 会跳过根、
         误伤内嵌的装饰 <svg>（它们带 data-pptx-native-ref="rNN"，是原生还原
         标记，删掉会破坏封面/致谢的内嵌图形还原）。
      💡 现模板的根标签都没有该属性 → 正常情况**静默跳过**。
    """
    # 目录页：<text> 里的裸 & 补转义
    if template_name in AMP_TEMPLATES:
        svg_text, _ = re.subn(
            r'(<[^>]*text[^>]*>[^<]*?)&(?!amp;|lt;|gt;|quot;|apos;)([^<]*</[^>]*text>)',
            r'\1&amp;\2', svg_text)

    # 根 <svg> 标签残留的 native-ref 清理（只动文档第一个 <svg>）
    tag = re.search(r'<(?:ns\d+:)?svg\b[^>]*>', svg_text)
    if tag:
        raw_tag = tag.group(0)
        new_tag = re.sub(r'\s+data-pptx-native-ref="[^"]*"', '', raw_tag)
        if new_tag != raw_tag:
            svg_text = svg_text[:tag.start()] + new_tag + svg_text[tag.end():]

    return svg_text


# ═══════════════════════════════════════════════════════════════
# 单页构建
# ═══════════════════════════════════════════════════════════════

def xml_escape(text: str) -> str:
    """将文本中的特殊字符转义为 XML 实体，防止破坏 SVG 结构。"""
    text = text.replace('&', '&amp;')
    text = text.replace('<', '&lt;')
    text = text.replace('>', '&gt;')
    text = text.replace('"', '&quot;')
    return text


def normalize_template(template_name):
    """规范化模板名：漏了 .svg 自动补，错误后缀（如 .png）统一纠正为 .svg。"""
    base, ext = os.path.splitext(template_name)
    if ext != '.svg':
        return base + '.svg'
    return template_name


def resolve_template_set(config, templates_root=None, palette_name=None):
    """解析模板风格目录，返回 (风格名, 风格目录 Path)。

    结构：assets/templates/<风格名>/<门面页>.svg
          assets/templates/009_content.svg      ← 009 在根，不分风格

    取值优先级（🔴 2026-10-09 方案 C 用户拍板 —— 配色与门面页「同步切换」）：
    1. `_meta.template_set` **显式声明** → 用它（最高优先级，供将来「不同名混搭」用）。
    2. 未显式声明 → **从 `_meta.palette` 派生**：配色 id 与风格同名时自动同步。
       - `templates/<palette值>/` **存在** ⇒ 用它 ⇒ 「选 minimal = 整套 minimal」。
       - 不存在 ⇒ **回退 `default` + 醒目告警**（🔑 **不中断** —— 这是"配色做了一半、
         门面页还没做"的正常中间态，不是配置损坏）。
    3. 都没有 → `default`。
    - 旧 config 无这两个字段 ⇒ 走 default ⇒ 向后兼容。
    - 🔑 派生逻辑集中在 `apply_palette.resolve_style_name()`，与两个校验脚本**共用一份**。

    ⚠️ **仅当显式声明的 `template_set` 指向不存在的目录时才报错退出**（那是真的写错了，
    属配置/资源损坏，必须炸 —— 见下方 FileNotFoundError）。
    """
    root = Path(templates_root) if templates_root else TEMPLATES_ROOT
    meta = config.get('_meta', {}) or {}

    from apply_palette import resolve_style_name
    # ⚠️ warn=False：回退提示统一由 resolve_palette 的**原子回退**分支打印一次（避免重复刷屏）
    name, _derived = resolve_style_name(meta, palette_name=palette_name,
                                        templates_root=root, warn=False)

    style_dir = root / name
    if not style_dir.is_dir():
        available = sorted(
            d.name for d in root.iterdir()
            if d.is_dir() and not d.name.startswith('.')
        ) if root.is_dir() else []
        raise FileNotFoundError(
            f"模板风格目录不存在: assets/templates/{name}/\n"
            f"  可用风格: {', '.join(available) if available else '（无）'}\n"
            f"  提示: _meta.template_set 应写 assets/templates/ 下的子目录名，如 \"default\""
        )

    # 硬校验：009 必须在根（它是共享模板，不在任何风格目录里）
    if not (root / PALETTE_TEMPLATE).exists():
        raise FileNotFoundError(
            f"创作页模板缺失: assets/templates/{PALETTE_TEMPLATE}\n"
            f"  🔴 009 创作页是**共享模板**，必须放在 templates/ 根目录（不在风格子目录里）。\n"
            f"  它同时是配色替换的唯一作用对象，缺失则无法出片。"
        )
    return name, style_dir


def resolve_template_path(template_name, style_dir, root=None):
    """按模板名定位实际文件：009 在根，门面页在风格目录。"""
    tpl_root = Path(root) if root else TEMPLATES_ROOT
    if template_name == PALETTE_TEMPLATE:
        return tpl_root / template_name
    return Path(style_dir) / template_name


def build_page(page, palette_mapping=None, templates_dir=None, templates_root=None):
    """构建单个页面。

    Args:
        page: dict with keys: id, output, template, text
            text 中值为字符串 → 精确替换（占位符已全部扁平化，无同名冲突）
            text 中值为列表 → 兼容旧版本，按出现顺序逐个替换（一般不再使用）
        palette_mapping: 可选的「旧色值→新色值」表；None 表示跳过配色替换。
            🔴 只会作用于 009_content.svg（见 PALETTE_TEMPLATE）。
        templates_dir: 门面页所在目录（风格目录）。
        templates_root: 模板根目录（009 从这里取）。
    """
    tpl_root = Path(templates_root) if templates_root else TEMPLATES_ROOT
    style_dir = Path(templates_dir) if templates_dir else (tpl_root / DEFAULT_TEMPLATE_SET)

    template_name = normalize_template(page['template'])
    src = str(resolve_template_path(template_name, style_dir, tpl_root))
    dst = os.path.join(SVG_OUT, page['output'])

    # 严格校验：规范化后仍不存在 → 友好报错并列出可用模板
    if not os.path.exists(src):
        search_dir = tpl_root if template_name == PALETTE_TEMPLATE else style_dir
        available = sorted(f for f in os.listdir(search_dir) if f.endswith('.svg'))
        raise FileNotFoundError(
            f"模板文件不存在: '{page['template']}'（规范化后: '{template_name}'）\n"
            f"  查找路径: {src}\n"
            f"  该目录可用模板: {', '.join(available) if available else '（无）'}\n"
            f"  提示: template 字段应写纯文件名（可省略 .svg 后缀），如 '001_cover.svg'"
        )

    with open(src, 'r', encoding='utf-8') as f:
        svg = f.read()

    # 1. 替换占位符（文本内容自动 XML 转义）
    for key, value in page.get('text', {}).items():
        placeholder = '{{' + key + '}}'
        if isinstance(value, list):
            for v in value:
                svg = svg.replace(placeholder, xml_escape(v), 1)
        else:
            svg = svg.replace(placeholder, xml_escape(value))

    # 1b. 009 创作页：拼接正文片段（头部去掉 </svg> + 正文 + </svg>）
    if template_name == '009_content.svg':
        body_svg = page.get('body_svg')
        if not body_svg:
            raise ValueError(f"009 创作页缺少 body_svg 字段: {page['id']}")
        body_path = os.path.join(BODY_DIR, body_svg)
        if not os.path.exists(body_path):
            raise FileNotFoundError(
                f"正文片段不存在: {body_svg}（assets/svg_body/ 下无此文件）\n"
                f"  提示: Step 2 创作正文片段后写入 assets/svg_body/{body_svg}"
            )
        with open(body_path, 'r', encoding='utf-8') as f:
            body = f.read()
        # 硬保险：闭合标签必须是不带前缀的 </svg>，否则 rsplit 找不到会静默把正文拼到 </svg> 之后
        # （产出的 XML 非法，而 Step 3d 的三项检查查不出来）。
        if '</svg>' not in svg:
            raise ValueError(
                f"009 模板 '{template_name}' 的闭合标签不是 </svg>"
                f"（可能带命名空间前缀，如 </ns0:svg>），无法安全拼接正文片段: {page['id']}"
            )
        svg = svg.rsplit('</svg>', 1)[0] + '\n' + body + '\n</svg>'

    # 2. 清理残留占位符
    svg = re.sub(r'\{\{[A-Z_0-9]+\}\}', '', svg)

    # 3. 修正模板底图路径
    svg = fix_image_paths(svg)

    # 4. XML 兼容性修复
    svg = fix_xml_compatibility(svg, template_name)

    # 5. 🎨 配色替换 —— 🔴 只作用于创作页（009）
    #    门面页（001/002/003/010）的风格由「整套模板」承载，不在这里刷漆（2026-10-09 用户拍板）。
    replaced = 0
    if palette_mapping and template_name == PALETTE_TEMPLATE:
        from apply_palette import apply_mapping
        svg, counts = apply_mapping(svg, palette_mapping)
        replaced = sum(counts.values())

    with open(dst, 'w', encoding='utf-8') as f:
        f.write(svg)

    return dst, replaced


# ═══════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════

def resolve_palette(config, force_default=False, atomic_style=None,
                    requested=None, declared_style=None):
    """从 config 的 `_meta.palette` 解析配色方案，返回 (方案名, 方案中文名, 替换表)。

    回退规则（2026-10-08 用户拍板 / 2026-10-09 加**原子性**）：
    - 缺省 / 未声明 → "default"（商务风，替换后与原样一致）。
    - **声明了但方案文件不存在**（如前端拼错成 `pink`）→ **回退 default + 醒目告警**，
      **不中断流程**。⚠️ 告警是必需的：否则"用户选了简约风、却拿到商务风"会无人知晓，
      成品交付后才发现配色不对。
    - 🔴🔴 **原子回退（方案 C-1，2026-10-09 用户拍板）**：配色与门面页风格是**一整套**，
      **要么都换、要么都不换**。
      当 `_meta.template_set` **未显式声明**、而请求的配色值（如 `minimal`）
      **没有对应的 `templates/<值>/` 目录**时 ⇒ `atomic_style` 已被门面页解析回退成 `default`
      ⇒ **配色也一并回退 `default`**（打印一行说明，不中断）。
      🔑 否则会出现「蓝封面 + 橙正文」的**半成品混搭** —— 那是观感割裂的未完成状态。
      ⚠️ 若调用方**显式声明**了 `template_set`（想混搭），则**不做原子回退**，配色照常生效。
    - force_default=True → 强制用 default（`--no-palette`）。
    - 方案文件**存在但角色缺色** → 仍然抛异常：那是**配置损坏**，不是用户输入问题，必须炸。

    Args:
        atomic_style: 门面页实际解析出的风格名（由 `resolve_template_set` 返回）。
            与 `requested` 不一致 ⇒ 说明发生了回退 ⇒ 配色跟着回退。
        requested: 用户请求的配色名（来自 `_meta.palette`，未做回退）。
        declared_style: `_meta.template_set` 的显式声明值（非空则不触发原子回退）。
    """
    from apply_palette import PALETTE_DIR, ROLES_PATH, build_mapping, load_json

    name = 'default' if force_default else (config.get('_meta', {}).get('palette') or 'default')

    # ── 🔴 原子性：门面页风格回退了 ⇒ 配色一并回退（方案 C-1）──
    if (not force_default and not declared_style and atomic_style
            and requested and requested != atomic_style):
        print()
        print('=' * 64)
        print(f'ℹ️  「{requested}」尚未就绪 —— 整套回退 default（商务风）')
        print(f'    门面页风格 {requested!r} 缺失: assets/templates/{requested}/')
        print(f'    ⇒ 配色也一并回退 default（配色与门面页**要么都换、要么都不换**）')
        print(f'    成品将是一份完整的 default（商务风）稿件。')
        print(f'    提示: 待该目录放齐同名门面页后，同一条提示词将自动整套升级为 {requested}')
        print('=' * 64)
        print()
        name = 'default'

    palette_path = PALETTE_DIR / f'{name}.json'
    if not palette_path.exists():
        available = sorted(p.stem for p in PALETTE_DIR.glob('*.json')
                           if p.stem != 'roles')
        # ⚠️ 醒目告警（不是静默降级）—— 让"发生了回退"这件事看得见，但流程照常继续
        print()
        print('=' * 64)
        print(f'⚠️  配色方案不存在: {name!r}')
        print(f'    → 已回退 default（商务风），流程继续。')
        print(f'    可用方案: {", ".join(available)}')
        print(f'    提示: _meta.palette 应写方案文件名（不含 .json），如 "minimal"')
        print(f'    注意: 若调用方本意是其它配色，本次成品配色将与预期不符 ——')
        print(f'          请检查提示词标记 [PPT风格：<风格 id>] 的拼接。')
        print('=' * 64)
        print()
        name = 'default'
        palette_path = PALETTE_DIR / 'default.json'

    roles_doc = load_json(ROLES_PATH)
    palette = load_json(palette_path)
    mapping = build_mapping(roles_doc['roles'], palette)
    return name, palette.get('name', name), mapping


def main():
    parser = argparse.ArgumentParser(
        description='Step 3: 从 pipeline_config.json 生成 SVG（含配色替换）')
    parser.add_argument('--no-palette', action='store_true',
                        help='跳过配色替换，输出模板原生色（等效 default）')
    args = parser.parse_args()

    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        config = json.load(f)

    # ── 🔴 顺序很重要（方案 C-1 原子性，2026-10-09）──
    # 先解析门面页风格；若风格回退了 default，则**配色也必须一起回退** ——
    # 用户口径：minimal 是「一整套」，门面页与 009 配色**要么都做、要么都不做**。
    meta = config.get('_meta', {}) or {}
    declared_style = meta.get('template_set')
    requested_palette = 'default' if args.no_palette else (meta.get('palette') or 'default')

    try:
        set_name, tpl_dir = resolve_template_set(config, palette_name=requested_palette)
    except FileNotFoundError as e:
        print(f'\n❌ {e}\n')
        return 1

    # 风格是否发生了「从 palette 派生失败」的回退？⇒ 若是，配色一并回退
    palette_name, palette_label, palette_mapping = resolve_palette(
        config, force_default=args.no_palette,
        atomic_style=set_name, requested=requested_palette,
        declared_style=declared_style)

    pages = config['pages']
    # 不做替换的两种情形（都避免打印误导性的"颜色 ×N"、也省一次全文扫描）：
    #   ① `--no-palette`：显式跳过；
    #   ② 映射表**全是恒等**（如原子回退后的 default）⇒ 替换了也等于没换。
    is_identity = all(k == v for k, v in palette_mapping.items())
    skip_swap = args.no_palette or is_identity
    effective_mapping = None if skip_swap else palette_mapping

    print(f'Building {len(pages)} pages from pipeline_config.json...')
    print(f'  模板风格: {set_name}（门面页 {tpl_dir}）')
    print(f'  输出目录: {SVG_OUT}')
    print(f'  配色方案: {palette_label}（id={palette_name}）'
          f'{"  [--no-palette 已跳过]" if args.no_palette else ("  [无需替换]" if is_identity else "")}')
    print(f'  🔴 配色只作用于创作页（{PALETTE_TEMPLATE}，取自 templates/ 根）；门面页原样输出')
    print()

    total_replaced = 0
    face_pages = 0
    for page in pages:
        dst, replaced = build_page(page, effective_mapping,
                                   templates_dir=tpl_dir, templates_root=TEMPLATES_ROOT)
        total_replaced += replaced
        is_face = normalize_template(page['template']) != PALETTE_TEMPLATE
        if is_face:
            face_pages += 1
            mark = '  [门面页 · 不参与配色]'
        elif args.no_palette:
            mark = '  [--no-palette 已跳过]'
        else:
            mark = f'  颜色 ×{replaced}' if replaced else ''
        print(f'  ✓ {page["id"]} → {os.path.basename(dst)}{mark}')

    print(f'\n✅ All {len(pages)} pages built → {SVG_OUT}/')
    if args.no_palette:
        print(f'🎨 配色已跳过（--no-palette），全部页面输出模板原生色')
    elif palette_mapping:
        print(f'🎨 配色替换合计 {total_replaced} 处（{palette_label}，仅创作页）')
        print(f'   （另有 {face_pages} 个门面页未参与配色，保持模板原样）')

    return 0


if __name__ == '__main__':
    sys.exit(main())
