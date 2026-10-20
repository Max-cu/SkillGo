# Step 3: build_svgs.py — 生成 SVG 并填充文字


> **输入**：`assets/pipeline_config.json` + `assets/templates/` 模板 + `assets/images/` 底图 + `assets/svg_body/`（009 正文片段）
> **输出**：`assets/svg_output/pageXX.svg`（模板合并 + 占位符已填充的整页 SVG）

从 `assets/pipeline_config.json` 读取页面列表，复制模板 SVG → 替换占位符 → 修正图片路径，产出到 `assets/svg_output/`。

---

## 执行步骤

### Step 3a：确认前置条件

**操作**：在执行脚本前，确认以下文件都存在且内容完整：

1. `assets/pipeline_config.json` — 数据源，`pages[]` 数组不能为空。
2. `assets/templates/` — 模板目录，分两处（2026-10-09）：
   - **门面页**：`assets/templates/<风格名>/`（风格名 = `_meta.template_set`，缺省 `default`）
     —— 该目录同时放着这个风格的 `pipeline_config.schema.json` + `template_capacity.md`
   - **009 创作页**：`assets/templates/009_content.svg`（**在根上，不分风格**）
   每个 `pages[].template` 对应的模板文件必须在对应位置存在。
3. `assets/images/` — 模板底图目录，模板中引用的底图文件（如 `image1.png`）必须存在。
4. `assets/svg_body/` — 009 创作页正文片段目录，每个 009 页的 `body_svg` 字段指向的片段文件必须存在（缺则 build 报错）。
5. `assets/templates/` **字体合规** — 模板是字体声明的**源头**（门面页直接复制它，009 页是「模板头部 + 正文片段」拼接），必须先过第 5 条自检。

**模板字体自检**（必须执行，不可跳过）：

```bash
cd <SKILL_ROOT>
python scripts/validators/check_templates.py
```

**合格标准**：exit 0，输出「全部模板字体合规」。

| 允许的写法（契约 §1.1） | 用途 |
|---|---|
| `font-family="黑体, Arial, sans-serif"` | 中英混排 —— 容器与中文元素统一用它 |
| `font-family="Arial, sans-serif"` | 纯英文文段 |
| `font-family="sans-serif"` | 仅根 &lt;svg&gt; |

**严禁**：微软雅黑 / 等线 / 宋体 / 楷体 / 仿宋 / Calibri / DengXian / Segoe UI 等任何其他字体。
（外部稿常带【等线】【微软雅黑】—— 那是过期的 Office 默认字体，映射到 pptx 后中文会变字体。）

> **为什么这一步要独立设卡**：模板是**人工放置**的受保护资源，**换机器 / 重新拷贝 / 从原版恢复时，字体改动会凭空丢失**。
> 这道自检就是防线 —— 模板一旦回退，**在这里立刻报警**，不必等导出 pptx、翻到某一页才发现字体变了。

**如果缺失或不合规**：返回 SKILL.md 从对应步骤重新执行，补全缺失文件后再继续。不要在缺少文件的情况下强行执行。

---

### Step 3b：清空旧中间产物

**每次进入 build 阶段前，必须清空上轮 run 留下的中间产物**。这防止旧 run 残留的同名/不同名 SVG 文件与新 run 的产物混在一起，导致后续 finalize 和 export 拿错文件。

> **中断恢复同样必须先清空**：若 build/finalize/export **任一步**出错中断，重跑前**都必须先执行本步（3b）清空**（build 是覆盖写、不删多余文件，跳过硬清空会残留上一轮的旧页 SVG 混入本轮）。
>
> **本规则对全流程通用**：Step 4/5 的失败处理里凡出现「返回 Step 3 重新执行」，意思都是「**先清空、再从 build 跑起**」——清空动作统一记在本步，后续步骤不重复写。

> 只清空 `svg_output/`、`svg_final/` 两个目录、`assets/` 下的 `.svg_final.candidate-*`/`.svg_final.publish-*` 临时目录，以及 `exports/` 里的 `.pptx-build-*` 临时目录。
> **`exports/` 里的历史 `.pptx` 版本保留**（`日期_标题` 命名；同名自动另存 `-02`，迭代时不覆盖、不丢失）。
> **绝不碰** `templates/`、`images/`、`sources/`、`svg_body/`（build 的输入，需保留）、`scripts/`、`references/`。

```bash
cd <SKILL_ROOT>
python scripts/cleaners/clear_downstream.py
```

**验证**：首次使用本 skill 时这几个目录不存在，脚本会自动创建。后续使用时，`svg_output/`、`svg_final/` 用 `rmtree` 先删旧再 `makedirs` 重建空目录；`exports/` 只删临时目录、保留 pptx。

---

### Step 3c：执行 build_svgs.py

**命令**：

```bash
cd <SKILL_ROOT>
python scripts/build_svgs.py
```

> 脚本无需参数，自动读取 `assets/pipeline_config.json`。

**脚本内部做什么**（了解即可，不用手动操作）：

1. 读 `_meta.palette`（配色方案，缺省 `default`）与 `_meta.template_set`（门面页风格）；
   **两者原子切换（方案 C-1）**：风格名缺省时从 `palette` 派生同名目录（存在才用）；
   **不存在则配色与门面页一起回退 `default`**（不是只退门面页）。
2. 遍历 `pages[]`，对每个 page 执行：
   - 复制模板 SVG 到 assets/svg_output/&lt;output&gt; —— **门面页从 `templates/<风格名>/` 取，009 从 `templates/` 根取**
   - **门面页**：用 `text` 对象的值替换 `{{PLACEHOLDER}}` 占位符
   - **009 创作页**：替换页头 `PAGE_TITLE` → 剥掉模板的 &lt;/svg&gt; → 拼接 assets/svg_body/&lt;body_svg&gt; 正文片段 → 补回 &lt;/svg&gt;（无 &lt;/svg&gt; 时直接 raise 兜底）
   - 所有占位符已扁平化，`build_svgs.py` 按占位符名精确匹配替换，无数组遍历逻辑
   - 所有文本内容自动 XML 转义（`&` → `&amp;`、`<` → `&lt;` 等）
   - 清理未被替换的残留 `{{...}}` 占位符
   - 修正图片引用路径为 ../images/&lt;basename&gt;
   - XML 兼容性修复（目录页 &lt;text&gt; 裸 `&` 补转义、根 &lt;svg&gt; 残留 native-ref 清理）
   - **配色替换** ——**只对 009 执行**（2026-10-09）。门面页跳过，永远输出模板原样。
3. 打印每页的处理结果

> 🔵 **模板里图片路径怎么写（2026-10-14 修正）**：**必须写成相对模板自身**的路径 ——
> `templates/&lt;风格&gt;/*.svg` 用 `../../images/&lt;name&gt;`；`templates/009_content.svg`（在根上）用 `../images/&lt;name&gt;`。
> 🔴 **不存在"以 skill 根目录为基准"的写法**：SVG（以及网页）里的 `href` **一律相对文档自身**解析。
> 写成 `assets/images/x.png` 会指到 `templates/&lt;风格&gt;/assets/images/`（不存在）⇒ **直接打开模板 SVG 时图片全部不显示**。
> 唯一能"既能预览、又出片正确"的形态就是相对自身——这也正是当年门面页还与 009 同层时的写法。
> 本步骤会**按 basename 归一化**为输出目录视角的 `../images/&lt;basename&gt;`，所以**模板即便写成别的形式，出片结果也一样** ——
> "模板移到别的子目录也不失效"这条保障，落在**本步骤**，不在路径写法上。

**预期输出**：

```
Building N pages from pipeline_config.json...
  模板风格: default（门面页 .../assets/templates/default）
  输出目录: .../assets/svg_output
  配色方案: 简约风（id=minimal）
  🔴 配色只作用于创作页（009_content.svg，取自 templates/ 根）；门面页原样输出

  ✓ page01 → slide_01_cover.svg  [门面页 · 不参与配色]
  ✓ page04 → slide_04_body.svg  颜色 ×N
  ...

✅ All N pages built → .../assets/svg_output/
🎨 配色替换合计 N 处（简约风，仅创作页）
   （另有 M 个门面页未参与配色，保持模板原样）
```

> **风格 vs 配色 —— 整套原子切换（方案 C-1）**：
> `_meta.template_set` 管**门面页整张版式**；`_meta.palette` 管**创作页色值**。**两者要么都换、要么都不换**：
> - `templates/<palette 值>/` **存在** ⇒ 整套一起换（选 minimal = 门面页 + 009 都变墨绿）；
> - **不存在** ⇒ **配色也一并回退 `default`** + 一行说明（**不中断**）
>   ⇒ 出的是**完整的 default 稿件**，**不会出现「蓝封面 + 橙正文」**。
> 某风格只做了配色、模板目录未就绪时收到它，**整份回退 default 色 —— 这是正确行为**（不是 bug）。
> 唯一例外：**显式声明** `template_set` 时不做原子回退（刻意混搭用）。
> 详见 `SKILL.md`「风格 vs 配色」表。

---

### Step 3d：验证输出

**操作**：执行以下命令，四项合检：

```bash
cd <SKILL_ROOT>
python scripts/validators/verify_build_output.py
```

| 检查项 | 合格标准 |
|--------|---------|
| 页数正确 | 文件数 = `pages[]` 数组长度 |
| 无残留占位符 | 无 `{{...}}` 残留 |
| 图片路径正确 | 无绝对路径，格式为 ../images/&lt;xxx&gt; |
| XML 合法 | 每页可被 XML 解析且根元素为 `svg`（兜底 009 拼接点） |

- exit 0 → 四项全部通过
- exit 1 → 有残留占位符或页数/路径问题，根据输出定位对应的 `text` 字段或路径

---

### Step 3e：失败处理

| 错误信息 | 原因 | 解决 |
|---------|------|------|
| `FileNotFoundError: .../templates/xxx.svg` | config 中 `template` 字段指定的文件不存在 | 检查拼写、确认文件在 `assets/templates/` 中 |
| `JSONDecodeError` | `pipeline_config.json` 格式有误 | 返回 SKILL.md 从 Step 2 重新执行，用 json.load 验证格式后继续 |
| `KeyError: 'pages'` | config 缺少 `pages` 数组 | 检查 JSON 结构是否正确 |
| 模板文件**存在但解析失败**（XML 报错） | 模板 SVG 内容损坏或格式非法 | 用 `python -c "import xml.etree.ElementTree as ET; ET.parse('assets/templates/xxx.svg')"` 定位；模板是**受保护资源、不可擅改**，确认损坏则需人工重新提供该模板 |

---

### Step 3f：完成收尾

**执行成功后，必须输出以下内容**（不可省略）：
- SVG 生成完成，N 页文字已填入，占位符全部替换无残留

**必须引导**：请返回 **SKILL.md**，继续执行 **Step 4**（finalize_svg.py 图片内联 + SVG 最终化）。

---

## 注意事项

- **build 会覆盖 `svg_output/`**：重跑 build 会用模板原版覆盖掉改过的文件。所以修改了内容后，必须 build → finalize → export 完整跑一遍。
- 脚本**不修改** `assets/templates/` 中的原始模板，始终只操作 `svg_output/`。
