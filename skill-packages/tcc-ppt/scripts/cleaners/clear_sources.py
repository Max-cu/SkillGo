#!/usr/bin/env python3
"""
§0a 清空 sources/ 中的旧素材。

删除 sources/ 目录的全部内容（文件 + 子目录），只保留 sources/ 空目录本身。
下次跑素材准备时 extract_all.py 会重新往里写。

使用方式:
  cd <SKILL_ROOT>
  python scripts/cleaners/clear_sources.py
"""

import os
import shutil
import sys


def main():
    skill_root = os.getcwd()
    sources = os.path.join(skill_root, 'assets', 'sources')

    if not os.path.isdir(sources):
        print('ℹ️  sources/ 不存在，无需清空')
        sys.exit(0)

    removed = 0
    for name in os.listdir(sources):
        path = os.path.join(sources, name)
        try:
            if os.path.isfile(path) or os.path.islink(path):
                os.remove(path)
            else:
                shutil.rmtree(path, ignore_errors=True)
            removed += 1
        except OSError as exc:
            print(f'⚠️  无法删除 {name}: {exc}')
            sys.exit(1)

    if removed:
        print(f'✅ 已清空 sources/（移除了 {removed} 个文件/目录）')
    else:
        print('ℹ️  sources/ 中无内容，无需清空')

    sys.exit(0)


if __name__ == '__main__':
    main()
