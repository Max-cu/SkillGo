"""
Step 1b 数照片 —— 统计 assets/sources/ 中可用照片数量，作为 Step 1 规划配图的素材清单。

用法：
- 009 创作页：照片直接写在正文 body_svg 片段里，可自由引用任意张、任意位置
  （裁剪策略见 contract_elements.md §4.3（图片））。
"""
import os, sys

VALID_EXTS = ('.jpeg', '.jpg', '.png', '.webp', '.gif', '.bmp')

def main():
    skill_root = os.getcwd()
    sources = os.path.join(skill_root, 'assets', 'sources')

    if not os.path.isdir(sources):
        print(f'❌ sources 目录不存在: {sources}')
        print('   请先执行素材准备。')
        sys.exit(1)

    photos = sorted([
        f for f in os.listdir(sources)
        if f.lower().endswith(VALID_EXTS) and os.path.isfile(os.path.join(sources, f))
    ])

    if not photos:
        print('⚠️ 没有可用照片（sources/ 中无 image_* 文件）')
        print('   009 创作页将无法配图，内容页只能走纯图形 / 文字 / 表格布局。')
        print(f'   sources/ 内容: {os.listdir(sources)[:20]}...' if os.listdir(sources) else '   sources/ 为空')
    else:
        n = len(photos)
        print(f'✅ 可用照片: {n} 张')
        for p in photos:
            print(f'   {p}')
        print('')
        print('📐 照片使用方式:')
        print('   009 创作页：自由引用任意张、任意位置，裁剪策略见契约 §4.3（图片）')
        print('   照片不够时，009 创作页可用图形 / 表格 / 文字补足版面（填充率 ≥25%）')

    sys.exit(0)

if __name__ == '__main__':
    main()
