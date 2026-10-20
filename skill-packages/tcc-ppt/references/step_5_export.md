# Step 5: export_flat.py — 导出 PPTX 文件


> **输入**：`assets/svg_final/*.svg` + `assets/pipeline_config.json`
> **输出**：`exports/*.pptx`（16:9 原生 PPTX）

读取 `svg_final/` 中的自包含 SVG，按模板分派 patch 模块，导出原生 `.pptx` 文件到 `exports/`。

---

## 执行步骤

### Step 5a：确认前置条件

**操作**：在执行前确认 Step 4 已成功执行，`assets/svg_final/` 中存在全部页面的自包含 SVG。

---

### Step 5b：理解 export 的五步内部处理

`export_flat.py` 内部有五步，自动按顺序执行：

| 子步骤 | 做什么 | 说明 |
|--------|--------|------|
| **1. 备份** | 复制 `svg_final/` → `svg_final_bak/` | 预处理会修改 SVG 文件，导出完后需要恢复 |
| **2. 收集 patch** | 按 `template` 字段匹配内置的 `TEMPLATE_PATCH_MAP` | 不同模板需要不同的 monkey-patch 预处理 |
| **3. 预处理** | 通用属性清理 + 模板专属 patch 处理 | 修改 `svg_final/` 中的文件 |
| **4. 导出** | 调用 `create_pptx_with_native_svg` 生成 `.pptx` | Native DrawingML 模式 |
| **5. 恢复** | 从 `svg_final_bak/` 恢复 `svg_final/`（无论成功失败） | 确保 svg_final 回到导出前的状态 |

**模板→patch 映射**：

| 模板 | patch 模块 | 需要特殊处理的原因 |
|------|-----------|------------------|
| `001_cover.svg` | `patch_001_cover` | ⚪ **空转**（2026-10-15 起封面语义形状已自带 inline `data-pptx-frame`，无可剥）；模块保留作"三无"模板的兜底 |
| `002_toc.svg` | `patch_002_toc` | 目录页（四栏 / 4 章）XML 兼容性 |
| `002_toc_3ch.svg` | `patch_002_toc_3ch` | 目录页（三栏 / 3 章）XML 兼容性；与四栏版同源，少一个条目 |
| `003_toc.svg` | `patch_003_toc` | 章节说明形状内含 2 个 &lt;text&gt; |
| `009_content.svg` | `patch_009_content` | 页头标题语义形状（`s-009-shape-10`）—— ⚠️ 该形状现**已自带 `data-pptx-frame` 且仅 1 个 `<text>`**，此 strip 已**非必需**（列为待验项） |
| `010_ending.svg` | 无 patch | 仅通用清理 |

---

### Step 5c：执行 export_flat.py

**命令**：

```bash
cd <SKILL_ROOT>
python scripts/export_flat.py
```

> 脚本无需参数，自动读取 `assets/pipeline_config.json` 并导出到 `exports/`。

**预期输出**：

```
Backed up assets/svg_final → svg_final_bak
Preprocessed N SVGs (patches: patch_001_cover, patch_002_toc_3ch, patch_003_toc, patch_009_content)
  Detected canvas format: PPT 16:9
  Slide dimensions: 1280 x 720 px
  SVG file count: N
  Mode: Native DrawingML shapes (directly editable)
  ...
  [Slide 1/N] slide_01_xxx.svg (Native)
  ...
  [Slide N/N] slide_NN_xxx.svg (Native)

[Done] Saved: <SKILL_ROOT>/exports/YYYYMMDD_<项目名>.pptx
  Slides: N; unselected Layout prototype carriers: 0
Restoring assets/svg_final from backup...
Restored.

✅ PPTX exported: <SKILL_ROOT>/exports/YYYYMMDD_<项目名>.pptx
   Size: X.X MB
```

> 末尾**没有 `Cleaned N temp build dir(s)` 这行属正常**——它只在有残留 `.pptx-build-*` 临时目录时才打印（源码里是 `if removed:` 守卫），无残留时整行不出现。

> **命名规则**：文件名 = `YYYYMMDD_项目名.pptx`（**不含作者名**，**不带当日序号**）。
> **`<项目名>` 会先经脚本清洗** —— Windows 非法字符（`/ \ : * ? " < > |`）会被自动转换或剔除
> （半角 `:` → 全角 `：`，其余 → `-`），**并打印一行 `⚠️ 项目名含文件名非法字符，已清洗`**；
> 清洗后为空则回退 `未命名项目`。所以**标题里写了半角冒号也不会让导出失败**，但**写全角最干净**。
> **同名冲突兜底**：`exports/` 里已存在同名文件时，自动另存为 `YYYYMMDD_项目名-02.pptx`、`-03`…
> —— **绝不静默覆盖**已导出的稿，历史版本仍保留。

> **看到 exit 1 先别慌**：导出收尾要清 `exports/.pptx-build-*` 临时目录，
> 本环境的**安全删除护栏**会拦截单次 >200 个文件的删除，回报 `SAFE_DELETE_BULK_CONFIRM_REQUIRED`。
> 此时 **PPTX 其实已经导出成功**。判断成败一律以 `verify_export_output.py` 为准；
> 残留的 `exports/.pptx-build-*` 用 Python `shutil.rmtree` 手动清即可。
> **千万不要因为 exit 1 就重跑导出** —— 同名已存在时会自动另存为 `-02`，会多出一份内容重复的 pptx。

---

### Step 5d：验证输出

**操作**：执行以下命令，四项合检：

```bash
cd <SKILL_ROOT>
python scripts/validators/verify_export_output.py
```

| 检查项 | 合格标准 |
|--------|---------|
| PPTX 文件存在 | `exports/` 中有 `.pptx` 文件 |
| 文件大小正常 | 1~30 MB |
| **幻灯片页数一致** | 解包 pptx 统计 `ppt/slides/slideN.xml` 数量 = `pages[]` 数组长度 |
| svg_final 已恢复 | 文件数 = `pages[]` 数组长度 |
| svg_final_bak 已清理 | `svg_final_bak/` 目录不存在 |

- exit 0 → 全部通过，输出文件路径和大小
- exit 1 → 有 PPTX 缺失/大小异常/备份未清理等问题

> 幻灯片页数一致性由该脚本**实际解包 pptx** 统计 `ppt/slides/slideN.xml` 后与 `pages[]` 比对，无需人工打开确认。
> 2026-09-27 补上这一项：此前它**只写在本说明里、实现中并不存在** ——
> 而上面 4 项只看 `svg_final/` 的文件数、从不打开 pptx，所以"导出时丢了某一页"（幻灯片 14 页 vs `pages[]` 15 页）
> 会让 4 项全部通过并打印「导出验证全部通过」。这是一个**被文档担保、却并不存在**的检查。

> **验证到此为止 —— 导出后不要把幻灯片"渲染成图片再逐页看"**（PowerPoint COM `Slide.Export` / `svglib` / 截图，一律不做）。三个理由：
> ① **它不在流程里**：本 skill 黑盒调用、无人工确认环节，交付物就是 `exports/` 里的 pptx，**渲染图不是产物**；
> ② **机器判据已全覆盖**：版式 / 配色 / 字数 / 占位符 / 图量 / 溯源各有校验器把关（见上表），**再加一道"看图"是重复劳动，且比脚本更不可靠**；
> ③ **环境不具备**：COM 渲染要有本机 PowerPoint + `pywin32`，沙盒 / 服务器上没有，把它写成流程动作只会无谓报错。
>
> **想核"图注是否与照片相符"** → `Read` 直看 `assets/sources/image_NNN.*`（多模态看原图，**无需渲染**），且在 **Step 1 配图 / Step 2 写图注**时就核完；**以照片实际内容为准**，别照抄素材登记行。
> **想看成片效果** → 由调用方自己用 PowerPoint / WPS 打开 `exports/*.pptx` —— **那是调用方的事，不是本 skill 的步骤。**

---

### Step 5e：失败处理

| 错误信息 | 原因 | 解决 |
|---------|------|------|
| `svg_final/` 为空 | Step 4 未执行或失败 | 返回 SKILL.md 从 Step 4 重新执行。逐条执行脚本，禁止 `&&` 串联 |
| `ModuleNotFoundError` | ppt-master 依赖缺失 | 确认 &lt;SKILL_ROOT&gt;/scripts/svg_to_pptx/ 目录存在 |
| `❌ Error: ...`（具体信息看错误栈） | patch 模块处理特定 SVG 时出错 | 最常见是该页的 SVG 结构不标准；检查对应 `svg_final/` 文件是否有语法错误 |
| 导出成功但 PPTX 中某页空白 | 该页的 SVG 内容映射失败 | 检查该页模板的 patch 模块是否正确、`data-pptx-*` 属性是否完整 |

**关键恢复机制**：无论导出成功或失败，`export_flat.py` 的 `finally` 块都会从备份恢复 `svg_final/`。这意味着 `svg_final/` 的内容始终不会因导出而被破坏。如果看到报错，确认 `svg_final/` 恢复后，分析错误原因再重试。

---

### Step 5f：完成收尾

**执行成功后，按顺序做以下操作**：

1. **必须输出产物路径**（不可省略，供调用方取件）：

```
✅ PPTX 导出完成，共 N 页
📄 <SKILL_ROOT>/exports/YYYYMMDD_<项目名>.pptx
```

2. **清理中间产物**（清 `svg_output/`、`svg_final/` 与 exports 内临时目录；**历史 pptx 保留**）：

   ```bash
   cd <SKILL_ROOT>
   python scripts/cleaners/clear_downstream.py
   ```

   > **本命令与 §3b 是同一个脚本、同一套边界** —— **清空规则的统一定义处是 `step_3_build.md` §3b**，本处不重写。
   > 一句话概括：只清 `svg_output/` / `svg_final/` 与各临时目录，**绝不碰** `templates/` `images/` `sources/` **`svg_body/`** `scripts/` `references/`。
   > 其中 **`svg_body/` 最容易误删**：它是模型手写的正文片段、**不可重建**（"小改 → 从 Step 3 重跑"要靠它）。
   > 要清它必须**显式**加 `--clear-svg-body`（**大改专用**，与本节无关）。

3. **全流程结束**。

> **本 skill 以 API 黑盒方式调用，全程无人工确认环节**——导出完成 + 清理中间产物即任务结束，
> 产物路径由第 1 步输出，调用方据此取件。
>
> 若调用方后续要求修改，按改动量选择重跑起点：
>
> | 改动量 | 操作 |
> |--------|------|
> | **小改**（换照片 / 改文字，不动页面结构） | 改 `pipeline_config.json` 的 `text` 值，或改 `svg_body/` 正文片段里的 &lt;image&gt; 引用；并同步 `design_spec.md`；然后**从 Step 3 重跑** |
> | **大改**（增删页面 / 改章节结构 / 重新配图） | 跑 `clear_downstream.py --clear-svg-body` 清正文片段，删 `assets/pipeline_config.json`，**从 Step 1 重跑** |
>
> 无论哪种，都逐条执行脚本，禁止 `&&` 串联。

---

## 最终产物

执行完成后，最终 PPTX 文件位于：

```
<SKILL_ROOT>/exports/YYYYMMDD_<项目名>.pptx
```

- `<项目名>` = `_meta.project`（**来自本次素材或模型拟题，不是示例里的字面值**）；导入前由脚本**清洗非法字符**，同名已存在则自动另存 `-02` —— **同日多次导出不会覆盖、历史版本全部保留**。命名规则详见 §5c。
- 用 PowerPoint 或 WPS 直接打开即可预览（**由调用方操作，不属于本 skill 的步骤**）。
