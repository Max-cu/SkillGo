#!/usr/bin/env python3
"""
素材准备: 从 zip 素材包提取文本和图片。

入口：一个 .zip 文件（后端统一打包，可能含多层嵌套 zip 和任意格式混搭）

处理流程:
  1. 解压 zip → _extract_root/（临时目录）
  2. 递归扫描，嵌套 .zip 继续解压（防同名：相对路径拼入文件名前缀）
  3. 按后缀分类：
     - 文档类 (.docx/.pdf/.pptx/.xlsx) → 走 source_to_md.py 子进程转换
     - 文本类 (.txt/.md) → 直接读取文本
     - 图片类 (.jpg/.jpeg/.png/.gif/.webp/.bmp) → 散图编号收编到 sources/
  4. 合并所有 Markdown → sources/文本.md
  5. **原文另存** → sources/_local_source.md（§0c 提纯前的回溯底稿）
  6. 清理临时目录（_extract_root/）

使用方式:
  python scripts/extract_all.py <素材.zip>
"""

import os, sys, shutil, re, json, subprocess, zipfile, importlib.util
from pathlib import Path

# ─── 目录定位 ───
PROJECT = Path(__file__).resolve().parent.parent
SCRIPTS = PROJECT / "scripts"
PYTHON = sys.executable
SOURCE_TO_MD = str(SCRIPTS / "source_to_md.py")

# ─── 图片扩展名集 ───
IMAGE_SUFFIXES = {'.jpeg', '.jpg', '.png', '.gif', '.webp', '.bmp'}
# ─── 文档类扩展名 → source_to_md -t 参数 ───
DOC_TYPE_MAP = {
    '.docx': 'doc', '.doc': 'doc', '.odt': 'doc', '.rtf': 'doc',
    '.html': 'doc', '.htm': 'doc', '.epub': 'doc',
    '.pdf': 'pdf',
    '.pptx': 'pptx', '.pptm': 'pptx', '.ppsx': 'pptx', '.ppsm': 'pptx',
    '.potx': 'pptx', '.potm': 'pptx',
    '.xlsx': 'excel', '.xlsm': 'excel', '.xls': 'excel',
}
# ─── 文本类扩展名 ───
TEXT_SUFFIXES = {'.txt', '.text', '.md', '.markdown'}

# ─── 参数解析 ───
if len(sys.argv) < 2:
    print("用法: python scripts/extract_all.py <素材.zip>")
    print()
    print("⚠️ 没有素材包时【不要跑本脚本】—— 本脚本只负责『已有 zip』的转换。")
    print("   调用方没上传文件时根本不会有 zip，那属于【零素材】路径：")
    print("   ⇒ 请回到 SKILL.md 的 §0d 联网取材，直接用提示词里的主题检索取文。")
    print("   （判定见 references/step_0_extract.md 开头的「执行顺序总表」）")
    sys.exit(1)

ZIP_PATH = Path(sys.argv[1])
if not ZIP_PATH.is_file():
    print(f"❌ 输入文件不存在: {ZIP_PATH}")
    sys.exit(1)
if ZIP_PATH.suffix.lower() != '.zip':
    print(f"❌ 输入必须是 .zip 文件，收到: {ZIP_PATH.suffix}")
    sys.exit(1)

SOURCES = PROJECT / "assets" / "sources"
os.makedirs(SOURCES, exist_ok=True)

print(f"📦 输入: {ZIP_PATH}")
print(f"📂 输出: {SOURCES}")

# ═══════════════════════════════════════════════════════════════
# Phase 1: 递归解压 zip（含嵌套 zip）
# ═══════════════════════════════════════════════════════════════

EXTRACT_ROOT = SOURCES / "_extract_root"
if EXTRACT_ROOT.exists():
    shutil.rmtree(EXTRACT_ROOT, ignore_errors=True)
EXTRACT_ROOT.mkdir()

# ─── 递归解压函数 ───
# 记录「落盘文件名 → 原始路径信息」，供散图登记时还原语义上下文
# {flat_disk_name: {"origin": "照片/子目录A/场景A.jpg", "zip": "补充材料.zip"}}
ORIGIN_MAP = {}


def _safe_flattened_name(zip_path: Path, member_name: str) -> str:
    """将 zip 内相对路径扁平化为安全文件名（防同名覆盖）。
    例如 subdir/report.docx → subdir_report.docx"""
    parts = member_name.replace('\\', '/').split('/')
    # 过滤空段（zip 可能记录目录条目 /）
    parts = [p for p in parts if p]
    if not parts:
        return None  # 纯目录条目，跳过
    return '_'.join(parts)

def _recursive_extract(zip_file: Path, dest_dir: Path, prefix: str = "",
                       zip_chain: str = "", visited: set = None):
    """递归解压 zip 文件到 dest_dir。嵌套 zip 继续解压，防同名前缀。

    zip_chain: 外层 zip 名链（顶层为空），用于还原「这张图来自哪个嵌套包」。
    """
    if visited is None:
        visited = set()
    abs_path = str(zip_file.resolve())
    if abs_path in visited:
        return  # 防止循环引用
    visited.add(abs_path)

    try:
        with zipfile.ZipFile(zip_file, 'r') as zf:
            for member in zf.infolist():
                # 跳过目录条目和 Mac 资源文件
                if member.is_dir():
                    continue
                name = member.filename.replace('\\', '/')
                if name.startswith('__MACOSX/') or '/._' in name:
                    continue

                flat_name = _safe_flattened_name(zip_file, name)
                if flat_name is None:
                    continue

                # 加上前缀避免多层级同名覆盖
                if prefix:
                    flat_name = prefix + '_' + flat_name

                dest_path = dest_dir / flat_name

                # 防两文件同名冲突：加序号
                if dest_path.exists():
                    stem, ext = os.path.splitext(flat_name)
                    counter = 1
                    while dest_path.exists():
                        dest_path = dest_dir / f"{stem}_{counter}{ext}"
                        counter += 1

                # 提取到内存再写盘（兼容 zip 密码等异常）
                try:
                    data = zf.read(member)
                except Exception as e:
                    print(f"  ⚠ 读取失败 {name}: {e}")
                    continue

                dest_path.write_bytes(data)

                # ─── 记录原始路径（落盘名 → zip 内原始相对路径 + 嵌套包名链）───
                ORIGIN_MAP[dest_path.name] = {
                    "origin": name,        # zip 内原始相对路径，如 照片/子目录A/场景A.jpg
                    "zip": zip_chain,      # 嵌套包名链，如 补充材料.zip（顶层为空）
                }

                # 如果解压出的文件也是 zip → 递归解压
                if dest_path.suffix.lower() == '.zip':
                    # 用「完整相对路径」作前缀（含外层目录），保留"这个包在哪"的信息。
                    # 例：03_汇报C/附件包.zip → 前缀 03_汇报C_附件包
                    #     （若只用 basename 会退化成 附件包，丢失所在目录）
                    inner_flat = _safe_flattened_name(zip_file, name)
                    inner_prefix = os.path.splitext(inner_flat)[0]
                    full_prefix = (prefix + '_' + inner_prefix) if prefix else inner_prefix
                    # 包链同样保留完整路径，便于看出嵌套包的位置
                    full_chain = f"{zip_chain}/{name}" if zip_chain else name
                    _recursive_extract(dest_path, dest_dir, full_prefix, full_chain, visited)
                    # 递归完后删除中间 zip 文件
                    dest_path.unlink(missing_ok=True)
    except zipfile.BadZipFile:
        print(f"  ⚠ 损坏的 zip: {zip_file.name}")
    except Exception as e:
        print(f"  ⚠ 解压失败 {zip_file.name}: {e}")

print()
print("🔍 Phase 1: 递归解压...")
_recursive_extract(ZIP_PATH, EXTRACT_ROOT)

# 列出解压后的文件
all_files = sorted(EXTRACT_ROOT.iterdir())
print(f"   解压得到 {len(all_files)} 个文件")
for f in all_files[:20]:
    print(f"     {f.name}")
if len(all_files) > 20:
    print(f"     ... +{len(all_files) - 20} more")

# ═══════════════════════════════════════════════════════════════
# Phase 2: 按后缀分类
# ═══════════════════════════════════════════════════════════════

print()
print("🔍 Phase 2: 分类...")

doc_files = []    # .docx/.pdf/.pptx/.xlsx 等
text_files = []   # .txt/.md
image_files = []  # .jpg/.png 等散图

for f in all_files:
    suf = f.suffix.lower()
    if suf in DOC_TYPE_MAP:
        doc_files.append(f)
    elif suf in TEXT_SUFFIXES:
        text_files.append(f)
    elif suf in IMAGE_SUFFIXES:
        image_files.append(f)
    else:
        print(f"  ⊘ 跳过未知类型: {f.name}")

print(f"  文档类: {len(doc_files)} 个")
print(f"  文本类: {len(text_files)} 个")
print(f"  散图  : {len(image_files)} 张")

if not doc_files and not text_files and not image_files:
    print("❌ 未找到任何支持的素材类型")
    print()
    print("⚠️ 注意：这是【空包】分支，不是故障 ——")
    print("   · 若调用方确实没给素材（只给了主题）⇒ 请走 §0d 联网取材，别在此终止；")
    print("   · 若本该有素材 ⇒ 请调用方确认打包是否正确（zip 是否为空 / 是否传错文件）。")
    shutil.rmtree(EXTRACT_ROOT, ignore_errors=True)
    sys.exit(1)

# ═══════════════════════════════════════════════════════════════
# Phase 2.5: 依赖预检（缺依赖就别往下跑）
# ═══════════════════════════════════════════════════════════════
#
# ⚠️ 为什么必须有这一步：
# 转换是「边跑边发现失败」的——缺 PyMuPDF / openpyxl 时，Phase 3 里对应文件会逐篇
# 转换失败并 continue，跑完只留下一个看不出原因的「⚠ 转换失败」，素材已经静默丢了。
# 所以在真正开始转换之前，按【已识别出的文件类型】预检依赖，缺就立即终止并给出
# 可直接复制的安装命令。（pandoc 类的外部二进制不在本检查范围。）

# 扩展名 → [(import 名, pip 名)]
DEP_BY_SUFFIX = {
    '.docx': [('mammoth', 'mammoth')],
    '.html': [('bs4', 'beautifulsoup4'), ('markdownify', 'markdownify')],
    '.htm':  [('bs4', 'beautifulsoup4'), ('markdownify', 'markdownify')],
    '.epub': [('ebooklib', 'EbookLib')],
    '.pdf':  [('fitz', 'PyMuPDF')],
    '.pptx': [('pptx', 'python-pptx')],
    '.pptm': [('pptx', 'python-pptx')],
    '.ppsx': [('pptx', 'python-pptx')],
    '.ppsm': [('pptx', 'python-pptx')],
    '.potx': [('pptx', 'python-pptx')],
    '.potm': [('pptx', 'python-pptx')],
    '.xlsx': [('openpyxl', 'openpyxl')],
    '.xlsm': [('openpyxl', 'openpyxl')],
    '.xls':  [('openpyxl', 'openpyxl')],
    '.ipynb': [('nbformat', 'nbformat'), ('nbconvert', 'nbconvert')],
}

print()
print("🔍 Phase 2.5: 依赖预检...")

needed = {}          # import 名 → (pip 名, [需要的文件名])
for doc_path in doc_files:
    for imp, pip in DEP_BY_SUFFIX.get(doc_path.suffix.lower(), []):
        needed.setdefault(imp, (pip, []))[1].append(doc_path.name)

missing = []
for imp, (pip, files) in sorted(needed.items()):
    if importlib.util.find_spec(imp) is None:
        missing.append((imp, pip, files))
        print(f"  ✗ {imp:<12} (pip: {pip:<16}) ← 处理 {len(files)} 个文件需要它")
    else:
        print(f"  ✓ {imp:<12} (pip: {pip})")

if missing:
    print()
    print("❌ 素材里有文件需要以下依赖，但当前 Python 环境未安装：")
    for imp, pip, files in missing:
        sample = "、".join(files[:3]) + (f" 等 {len(files)} 个" if len(files) > 3 else "")
        print(f"   · {pip:<16} 用于 {sample}")
    print()
    print("   请先安装（国内可加 -i https://mirrors.aliyun.com/pypi/simple/）：")
    print(f'     "{PYTHON}" -m pip install ' + " ".join(p for _, p, _ in missing))
    print()
    print("   ⚠️ 缺少依赖会让对应文件【转换失败并被跳过】——素材会静默丢失。")
    print("      装齐后重跑本步骤（重跑前先执行 §0a 清空 sources/）。")
    shutil.rmtree(EXTRACT_ROOT, ignore_errors=True)
    sys.exit(1)

# ═══════════════════════════════════════════════════════════════
# Phase 3: 处理文档类 → source_to_md.py
# ═══════════════════════════════════════════════════════════════

print()
print("🔍 Phase 3: 文档类转换...")

global_image_id = 0
all_md_parts: list[str] = []
failed_docs: list[str] = []      # 转换失败的文档名（供收尾汇总区分成功/失败）

for doc_path in doc_files:
    stem = doc_path.stem
    suffix = doc_path.suffix.lower()
    conv_type = DOC_TYPE_MAP.get(suffix, 'doc')
    print(f"  → {doc_path.name}  (类型: {conv_type})")

    tmp_md = SOURCES / f"_tmp_{stem}.md"

    result = subprocess.run(
        [PYTHON, SOURCE_TO_MD, str(doc_path), "-t", conv_type, "-o", str(tmp_md)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(PROJECT),
    )

    # ⚠️ stderr 的首行是 source_to_md.py 打的 `[>>] 脚本名 <完整命令行>` 回显
    #（含两个绝对路径，约 190 字符），真正的错误排在其后。必须先剔掉回显行再截断，
    # 否则截断窗口会被回显占满，真错误恰好落在窗口之外、完全看不到。
    if result.returncode != 0:
        lines = [ln for ln in result.stderr.strip().splitlines()
                 if not ln.startswith('[>>]')]
        detail = "\n".join(lines).strip()
        if not detail:                      # 剔完为空 → 退回原始 stderr 的尾部
            detail = result.stderr.strip()[-300:]
        print(f"    ⚠ 转换失败: {detail[:500]}")
        failed_docs.append(doc_path.name)
        continue

    md_text = tmp_md.read_text(encoding="utf-8")

    # ─── 处理文档产出的图片 ───
    # doc_to_md / pdf_to_md 等将图片放在 md 输出路径的同目录下
    # 即 SOURCES/_tmp_<stem>_files/（因为 tmp_md = SOURCES/_tmp_<stem>.md）
    media_dir = SOURCES / f"_tmp_{stem}_files"

    if media_dir.exists():
        img_files = sorted(
            [f for f in media_dir.iterdir() if f.is_file() and f.suffix.lower() in IMAGE_SUFFIXES],
            key=lambda f: f.name
        )

        if img_files:
            state = [0]

            def replace_match(m, files=img_files, st=state):
                idx = st[0]
                if idx >= len(files):
                    return m.group(0)
                img_file = files[idx]
                global global_image_id
                global_image_id += 1
                new_name = f"image_{global_image_id:03d}{img_file.suffix.lower()}"
                dest = SOURCES / new_name
                shutil.copy2(img_file, dest)
                print(f"    📷 {new_name}  (from {img_file.name})")
                st[0] += 1
                return f"[[{new_name}]]"

            md_text, n_refs = re.subn(r'!\[[^\]]*\]\([^)]*\)', replace_match, md_text)
            n_copied = state[0]

            # 🔴 收编缺口检查（不许静默丢图）
            # Phase 7 会把 _tmp_* 整个删掉，所以"没被收编的图"= **永久丢失**。
            # 此前这一步完全静默：源文档里万一有"存进了 media 目录、但正文没引用"的图
            #（页眉图 / 文本框里的图），丢了没有任何人知道。
            if n_copied < len(img_files):
                dropped = [f.name for f in img_files[n_copied:]]
                print(f"    ⚠ 该文档产出 {len(img_files)} 张图，"
                      f"其中 {len(dropped)} 张未被正文引用、未收编：")
                print(f"      {'、'.join(dropped[:8])}{'…' if len(dropped) > 8 else ''}")
                print("      （不会进入 sources/，将随临时目录一并删除）")
            if n_copied < n_refs:
                print(f"    ⚠ 正文有 {n_refs} 处图片引用，但只找到 {n_copied} 个图片文件"
                      f"——{n_refs - n_copied} 处引用无对应文件（引用路径可能解析失败）")

    all_md_parts.append(md_text)

    # 清理临时 md 和 profile
    tmp_md.unlink(missing_ok=True)
    for p in SOURCES.glob(f"_tmp_{stem}.*"):
        p.unlink(missing_ok=True)


# ═══════════════════════════════════════════════════════════════
# Phase 4: 文本类直读
# ═══════════════════════════════════════════════════════════════

print()
print("🔍 Phase 4: 文本类直读...")

for txt_path in text_files:
    try:
        content = txt_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            content = txt_path.read_text(encoding="gbk")
        except Exception:
            print(f"  ⊘ 无法读取: {txt_path.name}")
            continue

    if content.strip():
        all_md_parts.append(content)
        print(f"  ✓ {txt_path.name}  ({len(content)} 字符)")
    else:
        print(f"  ⊘ 空文件: {txt_path.name}")


# ═══════════════════════════════════════════════════════════════
# Phase 5: 散图编号收编
# ═══════════════════════════════════════════════════════════════

print()
print("🔍 Phase 5: 散图收编...")

loose_image_placeholders = []

for img_path in image_files:
    global_image_id += 1
    new_name = f"image_{global_image_id:03d}{img_path.suffix.lower()}"
    dest = SOURCES / new_name
    shutil.copy2(img_path, dest)
    print(f"  📷 {new_name}  (散图: {img_path.name})")

    # ─── 还原原始路径：拆成「嵌套包 / 目录 / 文件名」三段语义标签 ───
    info = ORIGIN_MAP.get(img_path.name, {})
    origin = info.get("origin", img_path.name)
    zip_chain = info.get("zip", "")
    dir_part, file_part = os.path.split(origin)

    segs = []
    if zip_chain:
        segs.append(f"包：{zip_chain}")
    segs.append(f"目录：{dir_part or '/'}")
    segs.append(f"文件名：{file_part}")
    loose_image_placeholders.append(f"[[{new_name}]]（散图 | {' | '.join(segs)}）")


# ═══════════════════════════════════════════════════════════════
# Phase 6: 合并 Markdown
# ═══════════════════════════════════════════════════════════════

print()
print("🔍 Phase 6: 合并...")

# 如果散图未被任何文本引用，在文末统一登记（合并为一段，避免多切分出一个来源段）
if loose_image_placeholders:
    all_md_parts.append("\n## 附加图片\n" + "\n".join(loose_image_placeholders))

merged = "\n\n---\n\n".join(all_md_parts)

# 清理残留的临时文件路径引用
merged = re.sub(r'_tmp_[^/\\]+_files/', '', merged)
merged = re.sub(r'_extract_root[/\\]', '', merged)

merged_md = SOURCES / "文本.md"
merged_md.write_text(merged, encoding='utf-8')
print(f"📝 合并文本: {merged_md} ({len(merged)} 字符)")

# ─── 原文另存（§0c 提纯前的「回溯底稿」）───
# 本地素材进 §0c 会被重组为「关系块 + 要点」，原文不再出现在 文本.md 里。
# 留一份**一字不改**的原文，供回溯 / 与提纯结果比对 —— 它是调用方「既定内容」的凭证。
if merged.strip():
    archive = SOURCES / "_local_source.md"
    archive.write_text(merged, encoding='utf-8')
    print(f"🗄 原文另存: {archive} ({len(merged)} 字符，§0c 提纯前底稿)")

# ─── 汇总 ───
# 文档数报「成功 / 总数」，不能只报输入数 —— 否则有文件转换失败时，
# 汇总行仍是一个 ✅ + 完整的输入篇数，把失败完全掩盖（实测曾丢 4/11 篇而摘要毫无异样）。
total_docs = len(doc_files)
converted_docs = total_docs - len(failed_docs)
total_texts = len(text_files)
total_images = global_image_id
print()
if failed_docs:
    print(f"⚠️ 提取完成: 文档 {converted_docs}/{total_docs} 篇成功"
          f"（失败 {len(failed_docs)} 篇） + {total_texts} 篇文本 → {total_images} 张图片")
    for name in failed_docs:
        print(f"     ⊘ {name}")
    print("   ⚠️ 上述文件的内容未进入 文本.md，请先排查原因（最常见是缺依赖）再继续。")
else:
    # 与失败分支同构，一律报「成功/总数」——成功路径下两者相等，
    # 但统一口径后摘要里始终能看到分母，不会再出现"看不出查了几篇"的情况。
    print(f"✅ 提取完成: 文档 {converted_docs}/{total_docs} 篇 + {total_texts} 篇文本 → {total_images} 张图片")

# ═══════════════════════════════════════════════════════════════
# Phase 7: 清理临时目录
# ═══════════════════════════════════════════════════════════════

# 清理 _extract_root 和残留 _tmp_* 目录
shutil.rmtree(EXTRACT_ROOT, ignore_errors=True)
for tmp_dir in SOURCES.glob("_tmp_*"):
    if tmp_dir.is_dir():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    else:
        tmp_dir.unlink(missing_ok=True)
print("🧹 清理完成")
