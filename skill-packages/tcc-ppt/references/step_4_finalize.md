# Step 4: finalize_svg.py — 图片内联 + SVG 最终化


> **输入**：`assets/svg_output/*.svg`
> **输出**：图片已内联为 base64 的 `assets/svg_final/*.svg`（完全自包含）

将 `assets/svg_output/` 中所有外部图片引用内联为 base64，产出完全自包含的 SVG 到 `assets/svg_final/`。

---

## 执行步骤

### Step 4a：确认前置条件

**操作**：在执行前确认 Step 3 已成功执行，`assets/svg_output/` 中所有 SVG 文件就绪。

---

### Step 4b：理解 finalize 的三步内部处理

`finalize_svg.py` 内部执行三个子步骤，按顺序自动运行。了解它们有助于诊断问题：

| 子步骤 | 做什么 | 影响范围 |
|--------|--------|---------|
| **1. embed-icons** | 展开 &lt;use&gt; 引用，内联项目图标 | 有图标则这一步会展开「标题前小图标 / 流程节点图标」等；无图标时显示 `No icons` |
| **2. align-images** | 对齐裁剪/适配 + base64 内联所有 &lt;image&gt; 标签 | **核心步骤**——所有外部图片引用（含 009 正文片段的 sources/ 照片）转为内联 base64 |
| **3. flatten-text** | 将 &lt;tspan&gt; 展开为独立 &lt;text&gt;（兼容性） | 文字样式标准化 |

> 这三个子步骤是全自动的，不需要选择执行哪些。

---

### Step 4c：执行 finalize_svg.py

**命令**：

```bash
cd <SKILL_ROOT>
python scripts/finalize_svg.py
```

> 脚本**无需必填参数**，自动读取 &lt;SKILL_ROOT&gt;/assets/svg_output/，输出到 &lt;SKILL_ROOT&gt;/assets/svg_final/。
> 可选调参（一般用不到）：`--dry-run`（只预览不写）、`--only <步骤>`
> （只跑指定子步骤，取值 `embed-icons` / `align-images`）、
> `--quiet` / `--no-compress` / `--max-dimension <px>` / `--image-scale <倍率>`
> （控制内联图片压缩与缩放）。完整列表见 `python scripts/finalize_svg.py --help`。

**预期输出**：

```
[DIR] Project: <项目目录名>
[FILE] N SVG file(s)

[1/3] Expanding icons + local use references...
      No icons
      No local use references

[2/3] Aligning + embedding images...
      X image(s) aligned + embedded

[3/3] Flattening text...
      X file(s) processed

[OK] Done!

Next steps:
  python scripts/export_flat.py
```

> 有两行**随内容变化**，别误判成异常：
> - `[PREP] X inline geometry declaration(s) materialized` **仅当存在 inline geometry 时才打印**（在 `[FILE]` 行之后），没有就整行不出现；
> - `[3/3]` 那行在**无需扁平化**时打印 `No processing needed`，而不是 `X file(s) processed`。

---

### Step 4d：验证输出

**操作**：执行以下命令，三项合检：

```bash
cd <SKILL_ROOT>
python scripts/validators/verify_finalize_output.py
```

| 检查项 | 合格标准 |
|--------|---------|
| 文件数正确 | 等于 `pages[]` 数组长度 |
| 图片已内联 | 每个 SVG 含 `data:image/` |
| 无外部图片引用 | 无 `../sources/` 或 `../images/` 残留 |

- exit 0 → 全部通过
- exit 1 → 有页面内联失败或仍有外部引用，根据输出定位

---

### Step 4e：失败处理

| 错误信息 | 原因 | 解决 |
|---------|------|------|
| `svg_output directory not found` | Step 3 未执行，或不在 &lt;SKILL_ROOT&gt; 下运行 | 确认在 &lt;SKILL_ROOT&gt; 下执行、且 `assets/svg_output/` 里有 Step 3 的产物；需要时先重跑 Step 3。**本脚本无需任何参数**，不要传 `.` 或目录名——会报 `unrecognized arguments` 并以 rc=2 退出 |
| `No SVG files in svg_output` | svg_output 为空 | 返回 SKILL.md 从 Step 3 重新执行（**先执行 3b 清空**，见 `step_3_build.md` §3b）。逐条执行脚本，禁止 `&&` 串联 |
| `Image alignment/embedding failed for X image(s)` | 某个图片文件缺失或格式问题 | 检查对应的 sources/ 照片是否完整、未被损坏 |
| `svg_final publish failed` | 磁盘空间不足或权限问题 | 检查磁盘空间 |

---

### Step 4f：完成收尾

**执行成功后，必须输出以下内容**（不可省略）：
- SVG 最终化完成，N 页的所有图片已内联，无需外部依赖即可独立打开

**必须引导**：请返回 **SKILL.md**，继续执行 **Step 5**（export_flat.py 导出 PPTX 文件）。

---

## 注意事项

- `svg_final/` 是**中间产物**，和 `svg_output/` 一样，随后会被 Step 5 读取。不要手动编辑。
- finalize 后，SVG 完全自包含——可以独立拷贝到其他位置打开，图片不会丢失。
- `image_scale=2.0`（默认）意味着图片以 2 倍分辨率编码，确保导出 PPTX 后的清晰度。
