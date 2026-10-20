# 常用操作

> **配置驱动原则**：改内容只需编辑 `assets/pipeline_config.json`，脚本保持不变。
>
> **每次修改 config 后，都必须逐条执行 build → finalize → export**，
> 每步检查输出确认无误后再跑下一步。不可跳过任何步骤，不可只跑 build，**禁止 `&&` 串联命令**。
>
> **小改 vs 大改**：小改（换照片/改标题/微调一两句话）直接改 config.json + 同步更新 design_spec.md → 返回 SKILL.md 从 Step 3 重新执行；
> 大改（增删页面/改章节/重新配图/大段改写）删除 config.json → 返回 SKILL.md 从 Step 1 重新执行。
> 拿不准算不算小改就按大改处理。

---

## 排查问题指南（遇事先看这里）

**标准排查顺序**：

1. **先查 `references/pitfalls.md`** —— 大部分常见问题都能在这里找到原因和解决方案。
2. **再查本文件** —— 确认操作流程是否正确。
3. **看脚本输出** —— build / finalize / export 每步都有清晰的终端输出，报错信息通常直指问题所在。
4. **用验证命令排查**：
   - JSON 格式：`python -c "import json; json.load(open('assets/pipeline_config.json','r',encoding='utf-8')); print('OK')"`
   - 残留占位符：`python -c "import glob,re; [print(f) for f in glob.glob('assets/svg_output/*.svg') if re.search(r'\{\{[A-Z_]+\}\}', open(f, encoding='utf-8').read())]"`
   - 文件数正确：对比 `pages[]` 数组长度和 `svg_output/` `svg_final/` 中的 SVG 文件数

---

## 只改文字

1. 打开 `assets/pipeline_config.json`，找到对应页的 `text` 对象。
2. 修改占位符的值（对照 `assets/templates/<风格名>/pipeline_config.schema.json` 确认不超 `max_chars`）。
3. **同步更新 `assets/design_spec.md`** 中对应页的 §IX 占位符内容（config 和 design_spec 必须一致）。
4. **返回 SKILL.md 从 Step 3 重新执行。** 逐条跑脚本，每步确认输出后再继续，禁止 `&&` 串联。

---

## 换照片

1. 将新照片放到 `assets/sources/` 目录中。

2. **改 009 页正文片段**：照片直接写在 `assets/svg_body/pageXX.svg` 片段里，改片段内的 &lt;image href="../sources/..."&gt; 指向新照片，改完重跑 `check_creative_svg.py` 校验。

3. **同步更新 `assets/design_spec.md`** 中 §VIII 照片清单（确保分配页与 config 一致），然后跑 `python scripts/write_photo_plan.py` 重写 `/workspace/_state/photo_plan.json`。不要手改这份 JSON。
4. **返回 SKILL.md 从 Step 3 重新执行。** 逐条跑脚本，每步确认输出后再继续，禁止 `&&` 串联。

> 如果只是 **交换照片的位置**（不改文件名）：改 009 页片段里 &lt;image&gt; 的顺序即可。

---

## 新增页面

1. 在 `assets/pipeline_config.json` 的 `pages[]` 中新增一个条目。通用必需字段（schema `required_fields`）：`id`、`template`、`output`、`text`。

2. 根据模板类型补齐字段：
   - **009 创作页**：`text` 只有 `PAGE_TITLE`；另写 `body_svg` 字段指向 `assets/svg_body/pageXX.svg`（正文片段先按**契约**创作并通过 `check_creative_svg.py`）。照片在片段里。
   - **门面页**（001/002/003/010）：`text` 按 schema 填占位符。

3. 其余字段：`page`（数字页码，**纯文档性、脚本不读**，可写可不写）、`output`（slide_NN_&lt;slug&gt;.svg，同模板复用时需唯一）。

4. 如果新页面用了需要 patch 的模板（001/002/003），确认 `export_flat.py` 的 `TEMPLATE_PATCH_MAP` 中已有映射。009/010 不需要 patch。

5. **返回 SKILL.md 从 Step 3 重新执行。** 逐条跑脚本，每步确认输出后再继续，禁止 `&&` 串联。

---

## 使用新模板（模板不在现有映射中）

> **先分清你要做的是「加模板」还是「换风格」**（2026-10-09 结构变更后）：
> - **加/改一个门面页模板**（如再要一个"3 章纯章节页"）→ 放进 `assets/templates/default/`（或你正在用的风格目录），按下面 1~5 走。
> - **整批换门面页风格**（如"换成暖色系版式"）→ 建**新风格目录** `assets/templates/<新风格名>/`，
>   放齐**同名门面页**（001/002/002_3ch/003/010）。config 里**一般不用写任何东西** ——
> **方案 C（2026-10-09）**：省略 `_meta.template_set` 时，脚本会**从 `_meta.palette` 派生同名风格目录**。
>   ⇒ 只要目录名与配色 id 同名（如 `templates/minimal/` ↔ `palettes/minimal.json`），
>   提示词写 `[PPT风格：minimal]` 就**自动整套切换**，无需改 config。
>   （只在"风格名 ≠ 配色 id"要混搭时，才显式写 `_meta.template_set: "<新风格名>"`。）
> **最快做法 = 复制整个 `assets/templates/default/` 再改名** —— 这样 5 个门面页 SVG、
>   `pipeline_config.schema.json`、`template_capacity.md` **一次性全带走**，不会漏；
>   随后把新 SVG 换上、并按新模板核对这两个文件里门面页 5 项的数值。
> **不要动 `assets/templates/009_content.svg`** —— 它是共享的，不属于任何风格。
> 门面页**不吃配色**（`_meta.palette` 管不到它们），换风格只走这条路。
>
> **`references/` 下没有这两个文件**（2026-10-09 已删）——
> `pipeline_config.schema.json` 与 `template_capacity.md` **只存在于 `assets/templates/<风格名>/` 下**。
> 它们在 `references/` 的老位置已不存在，**别去那里找、也别把内容写回那里**。

1. 确认 `assets/templates/<风格名>/`（门面页）或 `assets/templates/`（009）中有该 SVG 模板文件。
2. 在 `assets/templates/<风格名>/pipeline_config.schema.json` 的 `_template_reference` 中补充其占位符定义（含 `max_chars`）。
3. 在 `assets/templates/<风格名>/template_capacity.md` 中补充该模板的容量表。
4. 如模板导出需要 patch：在 `scripts/export_flat.py` 的 `TEMPLATE_PATCH_MAP` 字典中补充映射。
5. 若该模板无需 patch（对照 `assets/templates/<风格名>/pipeline_config.schema.json` 的 `_template_reference`：`patch: null`）：零配置，直接可用。

---

## 模板修改（改框宽、字体、颜色等）

1. 修改 `assets/templates/<风格名>/`（门面页）或 `assets/templates/`（009）中的对应 SVG 文件。
2. 如果涉及占位符框宽变化：同步更新 `assets/templates/<风格名>/template_capacity.md` 和 `assets/templates/<风格名>/pipeline_config.schema.json` 的 `max_chars`。
3. 如果形状带 `data-pptx-native-ref`（**原生记录会锁死它的尺寸**，改 SVG 坐标不生效）：走「**去 native-ref 化**」——
   把该形状改成**语义形状式**：内联原生 record 的**全部字段**（`data-pptx-frame` / `data-pptx-object` / `data-pptx-prst` / `data-pptx-av-*`）
   ＋ **删掉 `data-pptx-native-ref`**（⚠️ 只补 `data-pptx-frame` 会报 `native metadata cannot carry both inline fields and a record`）；
   carrier 子元素上的 ref 改成 `data-pptx-part="geometry"`。改完跑 `check_templates.py` 确认「语义形状 frame 完整」。
4. 如果修改涉及装饰图形（如渐变条平行四边形）：等比缩放 path 坐标。
5. **返回 SKILL.md 从 Step 3 重新执行验证效果。** 逐条跑脚本，每步确认输出后再继续，禁止 `&&` 串联。

> **改模板颜色前先想清楚是哪一类**（这决定它会不会被配色替换冲掉）：
> - **创作页（009）**的色值：**会被 `_meta.palette` 替换**（前提是该色在 `assets/palettes/roles.json` 里登记了角色）。
>   想加一个新语义色 → 两处一起改：`roles.json` 登记 + 每个 `palettes/*.json` 给对应 token。
> - **门面页**的色值：**不受配色影响**（`build_svgs.py` 已限定只对 009 替换），改了就生效。
> - 两边的合同：契约 §1.3 色表（约束 009 创作）+ `roles.json`（登记哪些色参与替换）。**它们是两回事**。

---

## 重置项目到干净状态

将 skill 恢复到刚安装时的样子——删除所有素材、中间产物与最终产物，保留模板、脚本和参考文档。

```bash
cd <SKILL_ROOT>
python scripts/cleaners/reset_project.py                # 全清（含 exports/）
python scripts/cleaners/reset_project.py --keep-exports # 保留最终 pptx
python scripts/cleaners/reset_project.py --dry-run      # 只预览，不删
```

**会删除**：`assets/sources/`、`assets/svg_body/`、`assets/svg_output/`、`assets/svg_final/`、
**`icons/`（skill 根目录的运行期图标池）**、`exports/`、`assets/design_spec.md`、`assets/pipeline_config.json`、
以及各类中断残留（`svg_final_bak*`、`.svg_final.candidate-*` / `publish-*`）

**会保留**：**`assets/icons/`（全局图标库，静态资产）**、`assets/templates/`、`assets/images/`、
`scripts/`、`references/`、`SKILL.md`

> **别把两个 icons 目录搞混**：
> - `assets/icons/` = **全局图标库**（5 库 12,027 个 SVG）—— **静态资产，永远保留**；
> - `icons/` = **运行期图标池**（`icon_sync.py` 从库里拷出来的那几十个）—— **产物，重置时清掉**。
>
> **打包发布时**，排除对象是**后者**（`icons/`），前者必须入包。

**这是破坏性操作：不带参数即执行删除。**
- 不确定会删什么，先加 `--dry-run` 看清单（它一个文件都不动）
- 想留下已导出的 pptx，用 `--keep-exports`
- 传了无法识别的参数会被拒绝并终止（exit 2），**不会误删**——但别拿参数做试探，不认识就查 `--help`
