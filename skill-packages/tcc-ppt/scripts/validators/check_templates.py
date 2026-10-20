"""
模板合规校验 —— 守住 assets/templates/<风格名>/*.svg 的三件事：
    ① font-family 声明   ② 语义形状的 frame 完整性   ③ 多 <text> 语义形状是否有 patch 兜底。

**为什么需要这个校验器**
    模板是字体声明的**源头**：门面页直接复制模板，009 页是「模板头部 + 正文片段」拼接。
    源头一旦不干净，下游每个环节都得打补丁，且补丁各有各的坑（HTML 转义 `&quot;`、
    带引号/不带引号两种写法、export 阶段还有备份-回滚机制）。

    而模板是**人工放置**的受保护资源（SKILL.md 铁律 5）：换机器、重新拷贝、
    从原版恢复时，字体改动会丢失，微软雅黑/等线就会悄悄回来。

    所以本校验器只做**检查**、不做修复 —— 一旦模板被恢复成原版，立刻报警，
    不用等到导出 pptx 翻到某一页才发现字体变了。

**目录结构（2026-10-09 起）**
    assets/templates/
      ├── 009_content.svg            ← 🔑 009 创作页在**根**（共享，不分风格）
      ├── native_payloads.json.gz    ← 门面页原生属性回填数据（路径写死在 native_payloads.py）
      └── <风格名>/                  ← 每个子目录是一**套门面页**（如 default/）
            ├── 001_cover.svg
            ├── 002_toc.svg
            ├── 002_toc_3ch.svg
            ├── 003_toc.svg
            └── 010_ending.svg
    ⇒ 本校验器**同时扫描根上的 009 和所有风格子目录的门面页**，
      确保加新风格时不会漏检。

**规则**（依据 creative_contract.md §1.1「字体」）
    | 场景     | 允许的写法                  |
    |---------|---------------------------|
    | 中英混排  | `黑体, Arial, sans-serif`  ← 推荐，容器/中文元素统一用它 |
    | 纯英文文段 | `Arial, sans-serif`        |
    | 根 <svg>  | `sans-serif`（可省略）       |

    严禁：微软雅黑 / 等线 / 宋体 / 楷体 / 仿宋 / Calibri / DengXian / Segoe UI 等。
    （过期的 Office 默认字体是重灾区：外部稿里常见，映射到 pptx 后中文会变字体。）

使用方式:
  cd <SKILL_ROOT>
  python scripts/validators/check_templates.py            # 扫描全部风格
  python scripts/validators/check_templates.py default    # 只扫描某个风格
"""

import os
import re
import sys
import xml.etree.ElementTree as ET

# ── 语义形状的判据 / 白名单来自 patches/common.py（**单一真相源**，避免两处录入漂移）──
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from patches.common import UNFRAMED_SEMANTIC_SHAPE_IDS, find_multi_text_shape_ids
except ImportError:      # 包结构异常时退化（只影响语义形状相关判定）
    UNFRAMED_SEMANTIC_SHAPE_IDS = set()
    find_multi_text_shape_ids = None

# 模板 → patch 映射（唯一的真相源在 export_flat.py）。导入失败则**跳过** ⑤ 检查，避免误报。
try:
    from export_flat import TEMPLATE_PATCH_MAP
except Exception:        # noqa: BLE001 —— 导入环境异常时只跳过该检查
    TEMPLATE_PATCH_MAP = None

# ── 允许的 font-family 取值 ──
# ⚠️ 必须写成**归一化后**的形态（无空格、小写），因为 _norm 会去掉所有空格再比对。
#    对照人眼形态：'黑体, Arial, sans-serif' → '黑体,arial,sans-serif'
ALLOWED_VALUES = {
    '黑体,arial,sans-serif',        # 中英混排（推荐：容器与中文元素统一用这个）
    'arial,sans-serif',             # 纯英文文段
    'sans-serif',                   # 仅根 <svg> 允许
}

# 根 <svg> 上允许的取值（只能是 sans-serif，或干脆不声明）
ROOT_ALLOWED = {'sans-serif'}

# 语义形状：带 data-pptx-semantic-object 的元素（<g> / <text> 都算）
SEMANTIC_TAG = re.compile(
    r'<(?:ns0:)?([a-zA-Z]+)\b([^>]*data-pptx-semantic-object[^>]*)>')

# ── 黑名单：出现即报错（不区分大小写）──
BLACKLIST = [
    '微软雅黑', 'microsoft yahei', 'yahei',
    '等线', 'dengxian',
    '宋体', 'simsun', 'nsimsun',
    '楷体', 'kaiti', 'simkai',
    '仿宋', 'fangsong', 'simfang',
    'calibri', 'segoe', 'cambria', 'consolas', 'roboto', 'helvetica',
]

# 中文字体白名单（用于给出「缺 Arial 兜底」的定向提示）
CJK_FONTS = ('黑体', 'simhei', 'arial')

FONT_ATTR = re.compile(r'font-family="([^"]*)"')
ROOT_TAG = re.compile(r'<\s*(?:ns0:)?svg\b[^>]*>', re.I)


def _norm(value: str) -> str:
    """归一化取值：解 HTML 转义、去空格、转小写，便于与允许集比对。"""
    v = value.replace('&quot;', '"').replace('&#34;', '"')
    v = re.sub(r'\s+', '', v)
    return v.lower()


def _display(value: str) -> str:
    """还原成便于人眼阅读的形态。"""
    return value.replace('&quot;', '"').strip()


def check_file(path: str, name: str):
    """检查单个模板。返回 (errors, warnings)。"""
    errors, warnings = [], []

    try:
        text = open(path, encoding='utf-8').read()
    except OSError as e:
        return [f'{name}: 读取失败 {e}'], warnings

    # ① XML 合法性
    try:
        ET.fromstring(text)
    except ET.ParseError as e:
        errors.append(f'{name}: XML 解析失败 → {e}')
        return errors, warnings     # 解析都不过，后续检查无意义

    # ② 逐处 font-family 检查
    root_span = ROOT_TAG.search(text)
    root_end = root_span.end() if root_span else 0

    for m in FONT_ATTR.finditer(text):
        value = m.group(1)
        norm = _norm(value)
        line = text[:m.start()].count('\n') + 1
        shown = _display(value)

        # 黑名单优先报（信息更具体）
        hit = [b for b in BLACKLIST if b in norm]
        if hit:
            errors.append(
                f'{name}:{line} 禁用字体 {shown}（命中 {hit[0]}）'
            )
            continue

        if norm not in ALLOWED_VALUES:
            hint = ''
            if '黑体' in norm and 'arial' not in norm:
                hint = ' → 建议写成「黑体, Arial, sans-serif」（中英混排形态，英文自动落 Arial）'
            elif 'arial' in norm and '黑体' not in norm:
                hint = ' → 若该文本可能含中文，请写成「黑体, Arial, sans-serif」'
            errors.append(
                f'{name}:{line} 取值不规范 {shown}{hint}'
            )
            continue

        # 根 <svg> 上不允许出现具体字体
        if m.start() < root_end and norm not in ROOT_ALLOWED:
            errors.append(
                f'{name}:{line} 根 <svg> 的 font-family 应为 sans-serif，实际为 {shown}'
            )

    # ③ 根 <svg> 是否声明了 font-family（缺失不报错，仅提示）
    if root_span:
        head = text[root_span.start():root_span.end()]
        if 'font-family' not in head:
            warnings.append(f'{name}: 根 <svg> 未声明 font-family（缺省不阻断，建议显式写 sans-serif）')

    # ④ 语义形状的 frame 完整性（2026-10-15 新增）——
    #    带 data-pptx-semantic-object 的元素必须有**一条**取到 frame 的路径，否则导出时炸：
    #      ① 自带 data-pptx-frame                → 常规做法（minimal / vibrant / 009 都是这样）
    #      ② 有 data-pptx-native-ref             → hydrate 会回填属性（default 的多数形状）
    #      ③ 在 UNFRAMED_SEMANTIC_SHAPE_IDS 里   → patch_001_cover 会移除其 semantic-object
    #    三层皆无 ⇒ converter 抛「Semantic shape text requires data-pptx-frame on its owner」
    #    ⇒ 加新风格时若漏写 frame，本检查当场拦下（不再等到导出才发现，也不再靠 id 白名单侥幸兜底）。
    for m in SEMANTIC_TAG.finditer(text):
        attrs = m.group(2)
        sid_m = re.search(r'\bid="([^"]*)"', attrs)
        sid = sid_m.group(1) if sid_m else '(无 id)'
        if ('data-pptx-frame' in attrs
                or 'data-pptx-native-ref' in attrs
                or sid in UNFRAMED_SEMANTIC_SHAPE_IDS):
            continue
        line = text[:m.start()].count('\n') + 1
        errors.append(
            f'{name}:{line} 语义形状 {sid} 取不到 frame（无 data-pptx-frame / 无 data-pptx-native-ref / '
            f'不在 patches/common.py 白名单）→ 导出时转换器抛「Semantic shape text requires '
            f'data-pptx-frame」。修法：**首选**给该 shape 补 data-pptx-frame（新风格都这么做）；'
            f'若确实无法补，才加进 UNFRAMED_SEMANTIC_SHAPE_IDS 并配套 patch'
        )

    # ⑤ 多 <text> 语义形状「有没有 patch 兜底」（2026-10-15 新增）——
    #    语义形状内**直接含 ≥2 个 <text>** ⇒ converter 抛
    #    「Semantic shape requires at most one paragraph-based text component」。
    #    三个 toc patch 已改为**条件式**（剥「直接含 ≥2 个 <text>」的形状），
    #    但 patch 是按**模板名**挂载的 ⇒ 新模板若带多 text 形状而没在 `TEMPLATE_PATCH_MAP` 注册，
    #    导出必崩。这里当场拦下，别等到 Step 5。
    if find_multi_text_shape_ids and TEMPLATE_PATCH_MAP is not None:
        multi = find_multi_text_shape_ids(text)
        tpl = os.path.basename(name)
        if multi and tpl not in TEMPLATE_PATCH_MAP:
            errors.append(
                f'{name}: 语义形状 {multi} 直接含 ≥2 个 <text>（converter 只允许 1 个文本主体），'
                f'但模板 {tpl} **未在 export_flat.py 的 TEMPLATE_PATCH_MAP 注册** → 导出必崩。'
                f'修法：在 TEMPLATE_PATCH_MAP 里给 {tpl} 挂一个 patch 模块'
                f'（照 patches/patch_002_toc.py 写，preprocess 调 patches.common.find_multi_text_shape_ids；'
                f'不要硬编码 id —— id 会跨风格撞号）'
            )

    return errors, warnings


def collect_templates(tpl_root: str, only_style: str = None):
    """收集待检模板：根上的共享模板（009）+ 指定风格的门面页。

    返回 [(标签, 文件绝对路径)]，标签形如 `009_content.svg`（根）或 `default/001_cover.svg`。
    only_style 非空时只收该风格的门面页（根上的 009 仍然收 —— 它是共享的）。
    """
    out = []
    if not os.path.isdir(tpl_root):
        return out

    # ① 根上的共享模板（009_content.svg）
    for f in sorted(os.listdir(tpl_root)):
        p = os.path.join(tpl_root, f)
        if os.path.isfile(p) and f.lower().endswith('.svg'):
            out.append((f, p))

    # ② 各风格子目录的门面页
    for entry in sorted(os.listdir(tpl_root)):
        style_dir = os.path.join(tpl_root, entry)
        if not os.path.isdir(style_dir) or entry.startswith('.'):
            continue
        if only_style and entry != only_style:
            continue
        for f in sorted(os.listdir(style_dir)):
            p = os.path.join(style_dir, f)
            if f.lower().endswith('.svg') and os.path.isfile(p):
                out.append((f'{entry}/{f}', p))
    return out


def main() -> int:
    skill_root = os.getcwd()
    tpl_root = os.path.join(skill_root, 'assets', 'templates')

    only_style = sys.argv[1] if len(sys.argv) > 1 else None

    if not os.path.isdir(tpl_root):
        print('❌ assets/templates/ 不存在 —— 模板是受保护资源，请检查 skill 完整性')
        return 1

    items = collect_templates(tpl_root, only_style)
    if not items:
        print('❌ assets/templates/ 下没有找到任何 .svg 模板')
        print('   期望结构: assets/templates/009_content.svg（根）+ <风格>/001_cover.svg 等')
        return 1

    styles = sorted({
        label.split('/')[0] for label, _ in items if '/' in label
    })
    print('=== 模板合规校验（字体 + 语义形状）===')
    print('契约依据: creative_contract.md §1.1（只准 黑体 + Arial）')
    scope = f'风格 {", ".join(styles)}' if styles else '（无风格子目录）'
    print(f'检查范围: assets/templates/  {scope}  + 根上共享模板  {len(items)} 个')
    print()

    all_errors, all_warnings = [], []
    for label, path in items:
        errs, warns = check_file(path, label)
        all_errors += errs
        all_warnings += warns
        mark = '✅' if not errs else '❌'
        print(f'  {mark} {label}')
        for e in errs:
            print(f'       {e}')

    print()

    if all_errors:
        print(f'❌ 发现 {len(all_errors)} 处违规（字体 / 语义形状）—— 模板须先修好再进 Step 3')
        print()
        print('修复参考（creative_contract.md §1.1）：')
        print('  · 中文/中英混排元素 → font-family="黑体, Arial, sans-serif"')
        print('  · 纯英文文段       → font-family="Arial, sans-serif"')
        print('  · 根 <svg>         → font-family="sans-serif"')
        print('  注：模板内 HTML 转义写法 &quot;黑体&quot; 也应改成不带引号的 黑体')
        return 1

    for w in all_warnings:
        print(f'⚠️  {w}')

    print('✅ 全部模板合规（字体：黑体 + Arial 无禁用字体；语义形状：frame 完整）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
