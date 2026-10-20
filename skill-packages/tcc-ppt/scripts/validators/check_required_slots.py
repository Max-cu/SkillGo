"""
Step 2e-③ 槽位完整性校验 —— 检查 pipeline_config.json 中每页的 text 对象是否包含模板要求的**所有**占位符。

占位符清单**直接派生自** `<风格目录>/pipeline_config.schema.json` 的
`_template_reference.<模板>.placeholder_schema` —— 那是"模板需要哪些槽"的**唯一定义处**
（与 check_design_spec.py 取 max_chars 用的是同一个节点）。

🔴 2026-10-09：schema 改为**按风格各存一份**（与模板文件同构）——
   定位规则 = `assets/templates/<_meta.template_set>/pipeline_config.schema.json`，
   缺省 `default`（与 build_svgs.py 的 resolve_template_set() 同一套回退逻辑）。
   原因：门面页的占位符/字数上限是随风格变的，必须跟那一套门面页模板放在一起。

🔴 2026-09-29：**不再读** `_pages_schema.required_slots`（该节点已删除）。
   原因：它和 placeholder_schema 描述同一件事、是**两次手工录入**，必然漂移 ——
   实测 `002_toc` 模板有 12 个占位符，那份手工清单只列了 4 个（`_NUM` / `_EN` 全漏），
   `002_toc_3ch` 漏 6 个、`003_toc` 漏 2 个。漏填时本校验抓不到，`{{...}}` 会一路印到成品。
   改为"只保留一份、另一份派生"后，模板增删占位符这边**自动跟上**，无需手工同步。
"""
import json, sys, os


def main():
    skill_root = os.getcwd()
    config_path = os.path.join(skill_root, 'assets', 'pipeline_config.json')

    if not os.path.isfile(config_path):
        print(f'❌ pipeline_config.json 不存在: {config_path}')
        sys.exit(1)

    with open(config_path, 'r', encoding='utf-8') as f:
        cfg = json.load(f)

    # ── schema 按风格定位（缺省 default，与 build_svgs 同规则）──
    # 🔑 共用 apply_palette.resolve_style_name()（方案 C：未显式声明时从 palette 派生）
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from apply_palette import resolve_style_name
    _meta = cfg.get('_meta') or {}
    style_name, _derived = resolve_style_name(
        _meta, palette_name=_meta.get('palette'), warn=False)
    schema_path = os.path.join(skill_root, 'assets', 'templates', style_name,
                               'pipeline_config.schema.json')
    if not os.path.isfile(schema_path):
        print(f'❌ schema 不存在: {schema_path}')
        print(f'   ← _meta.template_set = "{style_name}"，'
              f'该风格目录下应有自己的 pipeline_config.schema.json')
        avail = os.path.join(skill_root, 'assets', 'templates')
        if os.path.isdir(avail):
            styles = [d for d in sorted(os.listdir(avail))
                      if os.path.isdir(os.path.join(avail, d))]
            print(f'   可用风格: {", ".join(styles) if styles else "(无)"}')
        sys.exit(1)

    with open(schema_path, 'r', encoding='utf-8') as f:
        schema = json.load(f)

    # 占位符清单 = placeholder_schema 的键（唯一来源，不再手工维护第二份）
    tref = schema.get('_template_reference', {})
    rs = {t: list((m or {}).get('placeholder_schema', {}).keys())
          for t, m in tref.items()}

    pages = cfg.get('pages', [])

    # 🔴 空 pages 会让下面的逐页检查全部空转、打印假 ✅ —— 必须先拦住
    if not isinstance(pages, list) or not pages:
        n = len(pages) if isinstance(pages, list) else 0
        print(f"❌ 'pages' 缺失或为空（当前: {type(pages).__name__}，{n} 项）—— 无页面可校验")
        print('   ⚠️ 若配置顶层键名写错（如写成 "slides"），本校验会空转通过，问题会拖到 build 才爆')
        sys.exit(1)

    errors = []

    for p in pages:
        t = p.get('template', '').replace('.svg', '')
        required = rs.get(t, [])
        text = p.get('text', {})
        for slot in required:
            if slot not in text or (isinstance(text[slot], str) and text[slot].strip() == ''):
                errors.append(f'{p.get("id", "?")} ({t}): 缺少模板要求的占位符 {slot}')

    if errors:
        print('❌ 槽位校验失败:')
        for e in errors:
            print(f'  - {e}')
        sys.exit(1)
    else:
        print(f'✅ 槽位校验通过 ({len(pages)} 页，全部模板要求的占位符均已填充；'
              f'schema 取自风格 {style_name})')
        sys.exit(0)


if __name__ == '__main__':
    main()
