#!/usr/bin/env python3
"""
Step 2e 配置全量校验 —— 三合一统一入口。

把原先需要**分三次调用**的校验合并为一条命令，模型只需一轮即可拿到全部结论：

  ① 设计稿复核     → check_design_spec.py    （design_spec §IX 占位符字数 + 版式骨架反重复 + 布局框坐标自洽：含收口 ≥608）
  ② JSON 完整性    → check_json_integrity.py（语法 / 顶层结构 / 字段 / template / body_svg / 页序结构）
  ③ 槽位完整性     → check_required_slots.py（各页模板要求的占位符是否填全）

🔴 设计原则：**三项全部执行**，不因前一项失败而跳过后续 ——
   一次把问题报全，避免"修好一个又冒一个"的多轮返工。
   任一项 exit≠0 → 本脚本整体 exit 1。

🔵 子脚本保持独立、可单独运行排错；本脚本只是统一入口，**不改动它们的内部逻辑**。
   （`check_design_spec.py` 也是 Step 1f 的校验器 —— 同一步骤号下跨步复用，故必须保留。）

用法（工作目录无所谓，脚本按自身位置反推 SKILL_ROOT）：

    python scripts/validators/check_step2_config.py
"""
import os
import subprocess
import sys

# (显示标题, 脚本文件名)
CHECKS = [
    ('① 设计稿复核（§IX 占位符字数 + 版式骨架反重复 + 布局框坐标自洽：含收口 ≥608）', 'check_design_spec.py'),
    ('② JSON 完整性（语法 / 顶层结构 / 字段 / template / body_svg / 页序结构）',
     'check_json_integrity.py'),
    ('③ 槽位完整性（各页模板要求的占位符是否填全）', 'check_required_slots.py'),
]


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    skill_root = os.path.abspath(os.path.join(here, '..', '..'))

    # 子脚本内部用 os.getcwd() 反推 SKILL_ROOT，这里统一把 cwd 定到根目录
    try:
        os.chdir(skill_root)
    except OSError:
        pass

    py = sys.executable or 'python3'
    failed = []
    n = len(CHECKS)

    for i, (label, script) in enumerate(CHECKS, 1):
        path = os.path.join(here, script)

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

        rc = subprocess.run([py, path]).returncode
        if rc != 0:
            failed.append(label)

    sys.stdout.flush()
    print()
    print('=' * 64)
    if failed:
        print(f'❌ Step 2e 配置全量校验未通过 —— {len(failed)}/{n} 项失败：')
        for f in failed:
            print(f'   · {f}')
        print('   → 按上方各段报错逐项修复后，重跑本命令')
        sys.exit(1)

    print(f'✅ Step 2e 配置全量校验全部通过（{n} 项）')
    sys.exit(0)


if __name__ == '__main__':
    main()
