"""
§0e-① 素材概览 —— 读取 assets/sources/ 的**最终产物**，输出一份清单报告。

> 表头旧写 `§0c-①` / `§0d-①` —— 那是**改动前的旧步骤号**（本文件由 §0e 执行）；
> 2026-10-14 随「本地素材提纯」一并订正为 `§0e-①`。

定位：**清单（inventory）**，回答「手上有什么料」；
      与同一步的 `verify_extraction.py`（**校验 validation**，回答「数据对不对」）分工不同。

报告内容：
  - 文本量（字符数 + 来源段数）
  - 图片总数，按来源分类（文档内嵌 / zip 散图 / 联网补充）
  - 散图的目录分布
  - 编号连续性检查
  - 素材充足度提示

使用方式:
  cd <SKILL_ROOT>
  python scripts/validators/check_materials.py
"""

import os
import re
import sys

IMAGE_EXTS = ('.jpeg', '.jpg', '.png', '.gif', '.webp', '.bmp')

# 素材充足度参考线（用于给提示，不作为失败条件）
# ⚠️ 这里是**粗筛**：0e 自身不解析参数，不知道真实页数与门面页数，
#    所以按**门面 7 页 / 总 15 页**折算固定线。精确判定在同一步的 ② 校验：
#    `verify_extraction.py --pages <PPT总页数> --facade <6|7>`（门面页由调用方传入）
#    算法（两处一致）：扣除门面页（001+002+N×003+010）后，
#    · 图：内容页 × 2 张/页 = 备选池期望量；不足即报警（图片进池，不一定全用）。
#    · 文：内容页 × 200 字符/页 = 正文期望量；不足则提示转 §0d 定点补内容。
#      （200 是实测反推：10-08 那份「奥特曼」deck 净正文 1,959/8 页 ≈ 245 字符/页，
#        成品 8 个内容页全部通过校验 ⇒ 200 留余量又不误报。）
IMG_PER_PAGE = 2            # 每个内容页期望图片数 —— 必须与 verify_extraction.py 保持一致
TEXT_PER_PAGE = 200         # 每个内容页期望的正文字符数 —— 必须与 verify_extraction.py 一致
DEFAULT_FACADE = 7          # 门面页兜底（= 4 章，最常见、也最省图）—— 与 verify_extraction.py 一致
DEFAULT_TOTAL_PAGES = 15    # 粗筛参考总页数（门面 7 页 + 内容页 8 页）
SMALL_IMG = (DEFAULT_TOTAL_PAGES - DEFAULT_FACADE) * IMG_PER_PAGE   # = 16，与 ② 校验口径一致
# 文本提示线 = 期望 × 放行比例 —— 🔑 与 verify_extraction.TEXT_ADEQUACY_THRESHOLD 同源
# （2026-10-14：由「低于期望即报」改为「低于期望的 80% 才报」，避免把够用的素材误报成偏少）
TEXT_SUFFICIENT_RATIO = 0.8
SMALL_TEXT = int((DEFAULT_TOTAL_PAGES - DEFAULT_FACADE) * TEXT_PER_PAGE
                 * TEXT_SUFFICIENT_RATIO)   # = 1280，同上


def main() -> int:
    skill_root = os.getcwd()
    sources = os.path.join(skill_root, 'assets', 'sources')
    md_path = os.path.join(sources, '文本.md')

    if not os.path.isdir(sources):
        print('❌ sources/ 不存在 —— 请先执行 §0a / 0b')
        return 1

    # ── 图片文件 ──
    img_files = sorted([
        f for f in os.listdir(sources)
        if os.path.isfile(os.path.join(sources, f))
        and f.lower().endswith(IMAGE_EXTS)
    ])

    if not os.path.isfile(md_path):
        print('❌ 文本.md 不存在 —— 请先执行 §0b（extract_all.py）')
        print(f'   sources/ 现有: {len(img_files)} 张图片')
        return 1

    content = open(md_path, encoding='utf-8').read()

    # ── 解析占位符及其括注 ──
    # 形如：[[image_006.jpg]]（散图 | 包：X.zip | 目录：xxx | 文件名：yyy | 内容：…）
    #       [[image_001.jpg]]（网图 | 主题：kw | 标题：… | 来源：host | URL：…）
    #       [[image_001.jpeg]]                ← 文档内嵌（未提纯时）
    #       [[image_001.jpeg]]（内嵌图 | 内容：…）  ← 文档内嵌（§0c 提纯后统一登记）
    # 分类只看括注里的关键词：含「散图」→散图，含「网图/联网」→联网，其余→文档内嵌。
    pattern = re.compile(r'\[\[(image_\d+\.\w+)\]\](（[^）]*）)?')
    doc_embedded, loose, web = [], [], []
    loose_dirs = {}

    for m in pattern.finditer(content):
        name, note = m.group(1), m.group(2) or ''
        if '散图' in note:
            loose.append(name)
            # 提取目录或包标签
            label = ''
            dm = re.search(r'目录：([^|）]*)', note)
            pm = re.search(r'包：([^|）]*)', note)
            if pm:
                label = pm.group(1).strip()
            elif dm:
                label = dm.group(1).strip()
            loose_dirs[label or '(未标注)'] = loose_dirs.get(label or '(未标注)', 0) + 1
        elif '网图' in note or '联网' in note:
            web.append(name)
        else:
            doc_embedded.append(name)

    total_ph = len(doc_embedded) + len(loose) + len(web)

    # ── 来源段数（extract_all 用 '\n\n---\n\n' 连接各来源）──
    segments = [s for s in content.split('\n\n---\n\n') if s.strip()]

    # ── 编号连续性 ──
    nums = []
    for f in img_files:
        try:
            nums.append(int(re.match(r'image_(\d+)', f).group(1)))
        except Exception:
            pass
    nums.sort()
    consecutive = bool(nums) and nums == list(range(nums[0], nums[0] + len(nums)))

    # ── 报告 ──
    print('=== 素材概览 ===')
    print(f'文本.md      : {len(content):,} 字符，{len(segments)} 个来源段')
    print(f'图片总数     : {len(img_files)} 张')
    print(f'  ├─ 文档内嵌 : {len(doc_embedded)} 张')
    print(f'  ├─ zip 散图 : {len(loose)} 张')
    print(f'  └─ 联网补充 : {len(web)} 张')
    print(f'占位符总数   : {total_ph} 个')

    if loose_dirs:
        print('散图来源分布 :')
        for label, n in sorted(loose_dirs.items(), key=lambda x: -x[1]):
            print(f'  {label:32s} {n} 张')

    if nums:
        print(f'编号区间     : image_{nums[0]:03d} ~ image_{nums[-1]:03d}'
              f'  {"（连续 ✓）" if consecutive else "（⚠ 不连续）"}')

    # ── 充足度提示 ──
    tips = []
    if len(img_files) == 0:
        tips.append('无任何图片 —— 内容页只能走纯图形/文字/表格布局')
    elif len(img_files) < SMALL_IMG:
        tips.append(f'图片偏少（{len(img_files)} 张 < {SMALL_IMG} 张）'
                    f' —— 按 {DEFAULT_TOTAL_PAGES} 页 / 门面 {DEFAULT_FACADE} 页 折算的参考线'
                    f'（内容页 {DEFAULT_TOTAL_PAGES - DEFAULT_FACADE} × {IMG_PER_PAGE} 张）')
        tips.append('　　同一条命令的 ② 校验部分会按实际页数 / 门面页数精确评估并给出补图张数，以校验结果为准')
    if len(content) < SMALL_TEXT:
        tips.append(f'文本偏少（{len(content):,} 字符 < {SMALL_TEXT:,}）'
                    f' —— 按 {DEFAULT_TOTAL_PAGES} 页 / 门面 {DEFAULT_FACADE} 页 折算的粗筛参考线'
                    f'（内容页 {DEFAULT_TOTAL_PAGES - DEFAULT_FACADE} × {TEXT_PER_PAGE} 字符'
                    f' × 放行比例 {TEXT_SUFFICIENT_RATIO:.0%}）')
        tips.append('　　⇒ 精确判定看同一条命令 ② 校验的「正文体量评估」'
                    '（按实际页数、且只计净正文；本条是全文口径、含图片登记行）')
        tips.append('　　不足则转 §0d 定点补缺口补内容（或压缩页数），别硬做')
    if total_ph != len(img_files):
        tips.append(f'占位符 {total_ph} 个 ≠ 图片 {len(img_files)} 张'
                    f' —— 请跑 0e 校验确认')

    if tips:
        print()
        print('⚠ 提示:')
        for t in tips:
            print(f'  · {t}')
    else:
        print()
        print('✅ 素材量看起来充足')

    print()
    print('→ 同一条命令的 ② 校验部分会继续做「对不对」的校验')
    return 0


if __name__ == '__main__':
    sys.exit(main())
