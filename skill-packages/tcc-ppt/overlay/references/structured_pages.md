# 结构化页面协议

计划保存内容和创作选择，`render_deck_plan.py` 计算排版。不生成事实、不调用模型、不联网、不截正文、不缩字号、不放宽原有校验。

## 文档结构

写入 `assets/deck_plan.json`。以下只是格式示例，不是事实来源；实际填 3 或 4 个章节和完整页面内容。

```json
{
  "schema":"tcc.deck-plan.v1",
  "project":"项目报告",
  "title":"项目报告",
  "title_en":"Project Report",
  "author":"TCC",
  "date":"2026年10月12日",
  "palette":"default",
  "icon_library":"tabler-filled",
  "chapters":[{
    "title":"项目概况",
    "title_en":"Overview",
    "summary":["素材中的第一句","素材中的第二句"],
    "pages":[{
      "title":"能力与范围",
      "layout":"grid",
      "source_refs":["assets/sources/文本.md#项目概况"],
      "items":[{"heading":"第一能力","body":["已有事实段落","已有事实段落"]}]
    }]
  }]
}
```

每章至少一个内容页。总页数 = 内容页数 + 章节数 + 3；4 章、8 内容页为 15 页。其他结构走手工路径，不擅自改变用户要求。封面标题 ≤18 字、章名 ≤6 字、内容页标题 ≤15 字；英文目录短语也须填。作者来自素材，未署名填 `TCC`；用实际任务日期，不照抄示例。

`source_refs` 指向真实存在的 `assets/sources/` 文件，可附 `#章节`。引用网页仍需取材记录和来源标注。来源声明不证明每句表述正确，文案须忠实于已读材料。

## 版式与字段

`body` 为字符串或段落数组；数组保留分段，脚本按栏宽换行。保留事实、单位和中英混排，不写坐标、字体属性或 Python/SVG 代码。

| layout | 字段与用途 |
|---|---|
| grid | `items` 4/6 个 `{heading,body,icon?}`；卡片矩阵 §5.6 |
| columns | `items` 6/8 个，按上排→下排；多栏 §5.2 |
| two_columns | 两个 `items`，左 800px、右 360px；主次分区 §5.9 |
| table | `columns` 2–4 列、`rows` 4–7 行、可选 `column_weights`、必填 `summary`；§5.4 |
| numbered | `items` 4/5 个，建议各 3/2 个短段落；§5.10 |
| timeline | `items` 3 个，各用多个来源明确的短段落；§5.3 |
| metrics | 三个 `{value,label}` 放在 `metrics`，说明用 `detail:{heading,body}`；§5.5 |
| quote | 两个 `items`，全文宽；新增 §5.20 文字双带，沿用全部字体、配色、间距和密度规则 |
| image_text | 两个 `items` + 实际 `assets/sources/` 文件路径 `image`；§5.1 |
| custom | 复杂的自由 SVG，见下节 |

段落越长越需要宽布局。脚本会拒绝超长字段或空框；内容少时改用紧凑布局、合并页面或调配已有事实，不重复话、不造素材。相邻内容页不重复家族，任一家族不超过内容页的一半。

图标默认 `tabler-filled`；可按介质选择 `tabler-outline/chunk-filled/phosphor-duotone`，一份 deck 只用一套库。tabler 卡片默认内置 `layout-grid`，也可指定实际 `库/名`，如 `tabler-filled/info-circle`；其他库需明确具体图标。纯数表或纯文本可显式选 `icon_library:"None"`。

## 特殊创作

只为复杂的一页准备 `assets/custom_body/pageXX.svg`，根为一组 `<g>`，按契约三册创作，并填写真实框位：

```json
{"title":"关系示意","layout":"custom","svg":"assets/custom_body/pageXX.svg",
 "layout_family":"5.19","boxes":[[40,104,1200,544]],"content_heights":[500],
 "source_refs":["assets/sources/文本.md#关系"]}
```

自定义片段仍走完整原校验。编译器做语义不变的 canonical 归一化，不替代创作。不能用虚假框位、低字号或空装饰掩盖内容不足。

## 修正与验证

运行 `python scripts/render_deck_plan.py assets/deck_plan.json`。

- 错误指出具体页面、段落或表格单元格；只修这些计划字段，不重新生成其他 SVG。
- 原 SVG 检查通过后才发布文件，失败时原正文和配置保留。
- 成功报告有页面数、计划/SVG 字符数、变化文件和警告；同计划重跑不重写原页面。
- 运行 `check_creative_svg.py`、`check_step2_config.py`、`inject_image_credits.py`，再执行原 build→finalize→export。
- SkillGo 最终验证用 `run_verifier.artifacts` 声明原导出路径，不手工复制产物。
