#!/usr/bin/env python3
"""
export_flat.py — Step 5: SVG→PPTX 导出（flat 模式）

从 pipeline_config.json 读取页面列表，按 template 类型自动匹配 patch 模块：
  - 001_cover → patch_001_cover
  - 002_toc → patch_002_toc（四栏目录，4 章 deck）
  - 002_toc_3ch → patch_002_toc_3ch（三栏目录，3 章 deck）
  - 003_toc → patch_003_toc
  - 009_content → patch_009_content（页头标题语义形状；该形状现自带 frame，strip 已非必需）
  - 010_ending → 通用清理即可

预处理流程：
  1. 备份 assets/svg_final/ → svg_final_bak/
  2. 按页面匹配 patch 模块，应用 monkey-patch + SVG 预处理
  3. 调用 create_pptx_with_native_svg 导出 PPTX
  4. 恢复 assets/svg_final/

使用方式：
  python scripts/export_flat.py
"""

import json, sys, os, re, shutil
from pathlib import Path
from datetime import datetime

# ─── 目录定位 ───
PROJECT = Path(__file__).resolve().parent.parent            # skill 根目录
SCRIPT_DIR = PROJECT / "scripts"

# ─── 本地依赖 ───
sys.path.insert(0, str(SCRIPT_DIR))
from console_encoding import configure_utf8_stdio
configure_utf8_stdio()

CONFIG_PATH = PROJECT / 'assets' / 'pipeline_config.json'
SVG_SRC = PROJECT / 'assets' / 'svg_final'
EXPORT_DIR = PROJECT / 'exports'
EXPORT_DIR.mkdir(parents=True, exist_ok=True)


# ─── 导出文件名清洗 ───
_BAD_FILENAME_CHARS = {
    ':': '：',                       # 半角冒号 → 全角（Windows 合法，且保留可读性）
    '/': '-', '\\': '-', '|': '-',
    '*': '-', '?': '-',
    '"': '', '<': '', '>': '',
}


def _sanitize_filename(stem: str) -> str:
    """把项目名清洗成 Windows / macOS 都合法的文件名主体。

    🔴 为什么必需：`_meta.project` 现在可能来自**模型拟的封面标题**（可含冒号），
    而 Windows 文件名禁用 `/ \\ : * ? " < > |` —— 不清洗会让导出**直接失败**。
    另：控制字符、首尾空白、结尾句点（Windows 不允许文件名以 `.` 或空格结尾）一并处理。
    """
    out = []
    for ch in stem:
        if ch in _BAD_FILENAME_CHARS:
            out.append(_BAD_FILENAME_CHARS[ch])
        elif ord(ch) < 32:           # 换行 / 制表等控制字符
            out.append(' ')
        else:
            out.append(ch)
    cleaned = re.sub(r'\s+', ' ', ''.join(out)).strip().rstrip('.')
    return cleaned or '未命名项目'


def _resolve_output_path(date: str, safe_name: str):
    """确定输出路径：`YYYYMMDD_<项目名>.pptx`。

    同名已存在时**追加 `-02` / `-03` …**（绝不静默覆盖已导出的稿）。
    返回 `(路径, 是否因同名而另存)`。
    """
    base = EXPORT_DIR / f'{date}_{safe_name}.pptx'
    if not base.exists():
        return base, False
    n = 2
    while (EXPORT_DIR / f'{date}_{safe_name}-{n:02d}.pptx').exists():
        n += 1
    return EXPORT_DIR / f'{date}_{safe_name}-{n:02d}.pptx', True

# ─── 模板 → patch 模块映射 ───
TEMPLATE_PATCH_MAP = {
    '001_cover.svg':  'patch_001_cover',
    '002_toc.svg':    'patch_002_toc',
    '002_toc_3ch.svg': 'patch_002_toc_3ch',
    '003_toc.svg':    'patch_003_toc',
    '009_content.svg': 'patch_009_content',
}


def load_pages():
    """从 pipeline_config.json 加载页面列表（保持顺序）。"""
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        config = json.load(f)
    return config['pages']


def resolve_patch(template_name):
    """根据模板文件名查找对应的 patch 模块名。

    Returns:
        str | None: patch 模块名（如 'patch_002_toc'），无则返回 None
    """
    return TEMPLATE_PATCH_MAP.get(template_name)


def _cleanup_build_dirs():
    """兜底清理 export 过程遗留的 .pptx-build-* 临时目录。

    builder.py 内部虽在 finally 里 rmtree，但 Windows 下文件句柄可能未释放导致删除失败。
    这里在导出返回后（句柄已释放）再清一次，作为兜底，避免临时目录残留堆积。
    """
    removed = 0
    for name in os.listdir(EXPORT_DIR):
        if name.startswith('.pptx-build-'):
            p = EXPORT_DIR / name
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
                removed += 1
    if removed:
        print(f'   Cleaned {removed} temp build dir(s)')


def main():
    pages = load_pages()

    # ── Step 1: 备份（自愈式原子备份）──
    SVG_BAK = PROJECT / 'svg_final_bak'
    SVG_BAK_TMP = PROJECT / 'svg_final_bak.tmp'

    # 1a. 若存在上次 export 硬中断残留的 svg_final_bak，说明 svg_final 可能被预处理改坏，
    #     先用这份干净备份恢复 svg_final（自愈，无需回 Step 5）。
    if SVG_BAK.exists():
        print('⚠️  检测到上次 export 中断残留的 svg_final_bak，先用它恢复 svg_final...')
        shutil.rmtree(SVG_SRC, ignore_errors=True)
        os.rename(str(SVG_BAK), str(SVG_SRC))
        print('    已恢复 svg_final。')

    # 1b. 原子备份：先复制到临时名，再 rename 为正式名，避免 copytree 半成品污染 bak。
    shutil.rmtree(SVG_BAK_TMP, ignore_errors=True)
    shutil.copytree(SVG_SRC, SVG_BAK_TMP)
    os.rename(str(SVG_BAK_TMP), str(SVG_BAK))
    print('Backed up assets/svg_final → svg_final_bak')

    # ── Step 2: 收集页面 → patch 分派 ──
    # 收集需要的 patch 模块并去重
    needed_patches = set()
    page_patch_map = {}  # {filename: patch_module_name | None}

    for page in pages:
        fname = page['output']
        patch_name = resolve_patch(page['template'])
        page_patch_map[fname] = patch_name
        if patch_name:
            needed_patches.add(patch_name)

    # 动态加载 patch 模块（从 scripts/patches/ 目录）
    from patches.common import strip_flat_forbidden_attrs
    patch_modules = {}
    for patch_name in needed_patches:
        full_name = f'patches.{patch_name}'
        patch_modules[patch_name] = __import__(full_name, fromlist=[patch_name])

    # 已注册 monkey-patch 的模块
    patched = set()

    # ── Step 3: 预处理 svg_final ──
    svg_files = []
    for fname in sorted(os.listdir(SVG_SRC)):
        if not fname.endswith('.svg'):
            continue

        fpath = SVG_SRC / fname
        with open(fpath, 'r', encoding='utf-8') as f:
            content = f.read()

        # 通用清理
        content = strip_flat_forbidden_attrs(content)

        # 按页面匹配 template patch
        patch_name = page_patch_map.get(fname)
        if patch_name:
            mod = patch_modules[patch_name]
            if patch_name not in patched:
                mod.register_monkey_patches()
                patched.add(patch_name)
            content = mod.preprocess_content(content)

        with open(fpath, 'w', encoding='utf-8') as f:
            f.write(content)

        svg_files.append(fpath)

    print(f'Preprocessed {len(svg_files)} SVGs (patches: {", ".join(sorted(patched)) if patched else "none"})')

    # ── Step 4: 导出 PPTX ──
    from svg_to_pptx.pptx_package.builder import create_pptx_with_native_svg

    config = json.load(open(CONFIG_PATH, 'r', encoding='utf-8'))
    project_name = config['_meta']['project']
    date = datetime.now().strftime('%Y%m%d')
    # 🔴 先清洗：拟题可能含 Windows 非法字符（半角冒号等），不清洗会让导出直接失败
    safe_name = _sanitize_filename(project_name)
    if safe_name != project_name:
        print(f'⚠️  项目名含文件名非法字符，已清洗：{project_name!r} → {safe_name!r}')
    # 命名 = YYYYMMDD_<项目名>.pptx（2026-10-09 起**去掉当日序号**）
    output_path, renamed = _resolve_output_path(date, safe_name)
    if renamed:
        print(f'⚠️  同名已存在，本次另存为：{output_path.name}')
    success = False

    try:
        success = create_pptx_with_native_svg(
            svg_files=[Path(f) for f in svg_files],
            output_path=output_path,
            resource_root=PROJECT,
            verbose=True,
            use_native_shapes=True,
            pptx_structure='flat',
            enable_notes=False,
            transition='fade',
            image_optimize=False,
            use_layout_placeholder_frames=False,
            dangerous_nonconforming_export=True,
            text_flow='reflow',
            structured_baseline=False,
        )
    except Exception as e:
        print(f'\n❌ Error: {e}', file=sys.stderr)
    finally:
        # 恢复 svg_final
        print('Restoring assets/svg_final from backup...')
        shutil.rmtree(SVG_SRC, ignore_errors=True)
        shutil.move(str(SVG_BAK), str(SVG_SRC))
        print('Restored.')

    if success:
        size_mb = output_path.stat().st_size / 1024 / 1024
        print(f'\n✅ PPTX exported: {output_path}')
        print(f'   Size: {size_mb:.1f} MB')
        _cleanup_build_dirs()
    else:
        print('\n❌ Export failed', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
