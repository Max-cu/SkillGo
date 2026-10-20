#!/usr/bin/env python3
"""
§0e：素材概览 + 提取校验 —— 统一入口。

把素材准备的两个只读脚本合并为一条命令（同一步内、互不依赖、均无副作用）：

  ① 素材概览（清单）→ check_materials.py     —— 回答「手上有什么料」
  ② 提取输出校验    → verify_extraction.py   —— 回答「数据对不对」

🔴 **两项全部执行**：① 是纯清单（永远 exit 0），② 才可能失败；任一非零 → 整体 exit 1。

用法：
    python scripts/validators/check_step0.py --pages 15 --facade 7

参数（原样透传给 ② verify_extraction.py）：
    --pages N        PPT **总页数**（含门面页）—— 给了才做图量评估
    --facade 6|7     **门面页数** —— **6 = 3 章；7 = 4 章**（门面页 = 3 + 章节数）
                     🔑 素材准备时 pipeline_config.json 还没生成，门面页只能由调用方
                        **自己定好后传入**；不传则回退查 config（通常读不到）→ 按 7 页兜底。
                     🔄 门面页是**调节旋钮**：它 +1 → 内容页 −1 → 期望图量 −2。
                        素材主题冷门、网上搜不到足够图时，可把 6 升到 7。
    --chapters 3|4   `--facade` 的**等价别名**（facade = 3 + chapters），两者都传时以 `--facade` 为准。

🔵 两个子脚本保持独立、可单独运行排错；本脚本只是统一入口，**不改动它们的内部逻辑**。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# (显示标题, 脚本文件, 是否把命令行参数透传给它)
CHECKS = [
    ('① 素材概览（文本量 / 图片来源三分类 / 散图分布 / 编号连续性 / 充足度提示）',
     'check_materials.py', False),
    ('② 提取输出校验（文本非空 / 正文体量 / 图片数 / 占位符一一对应 / 图量是否够用）',
     'verify_extraction.py', True),
]


def main():
    skill_root = os.path.abspath(os.path.join(HERE, '..', '..'))
    try:
        os.chdir(skill_root)
    except OSError:
        pass

    py = sys.executable or 'python3'
    passthrough = list(sys.argv[1:])
    failed = []
    n = len(CHECKS)

    for i, (label, script, forward) in enumerate(CHECKS, 1):
        path = os.path.join(HERE, script)

        sys.stdout.flush()
        print()
        print('─' * 64)
        print(f'  [{i}/{n}] {label}')
        print('─' * 64)
        sys.stdout.flush()

        if not os.path.isfile(path):
            print(f'❌ 子校验脚本不存在: {path}')
            failed.append(label)
            continue

        cmd = [py, path] + (passthrough if forward else [])
        rc = subprocess.run(cmd).returncode
        if rc != 0:
            failed.append(label)

    sys.stdout.flush()
    print()
    print('=' * 64)
    if failed:
        print(f'❌ 素材准备提取校验未通过 —— {len(failed)}/{n} 项失败：')
        for f in failed:
            print(f'   · {f}')
        sys.exit(1)

    print(f'✅ 素材准备提取校验全部通过（{n} 项）')
    sys.exit(0)


if __name__ == '__main__':
    main()
