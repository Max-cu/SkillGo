---
name: tcc-ppt-skill
description: >-
  当用户需要生成 .pptx 演示文稿时使用本 skill —— **两种入口都接**：
  ① 提供**素材**（文档 / 图片包，任意格式混搭）；② **只给一句主题**
  （或贴一段需求文字，如"帮我做个高处作业安全的 PPT"）—— 后者走联网取材。
  覆盖完整流程：从素材包提取内容、编写 design_spec.md 内容大纲、
  生成 pipeline_config.json、执行 build→finalize→export 自动化流程。
  内容页（009 创作页）支持在合规约束下自由创作布局。
  触发词包括"生成 PPT"、"导出 pptx"、"跑 pipeline"、"做一份演示文稿"、
  "生成幻灯片"，以及**直接报一个主题名**（如"做一份茶马古道的 PPT"）。
  【能力边界】成品内容必须**可溯源**：要么来自调用方素材，要么来自**带来源标注的公开网络资料**；
  本 skill 不生成、不推断、不「合理发挥」。
  只给一句主题时走联网取材（Step 0 §0r）；但**搜不到实质内容就终止**，绝不编造。
  不适用于 Ardot 画布幻灯片设计——本 skill 仅负责 .pptx 文件导出。
agent_created: true
---
# 中国天辰 PPT Skill

通过标准化模板与数据配置，一键生成专业 .pptx 演示文稿。门面页（封面/目录/章节/致谢）由 `assets/pipeline_config.json` 统一管理；内容页统一走 009 创作页，在合规约束下自由创作布局，正文 SVG 片段存放于 `assets/svg_body/`。脚本固定不变，改内容永不改脚本。

## 🎯 能力边界（先确认输入合法，再动手）

**一句话定位**：本 skill 是「**把资料做成 PPT**」，不是「**编资料**」——
资料可以来自调用方，也可以来自**公开网络**（但**必须带来源标注**）。

### ✅ 做什么

把**素材**（文档 / 图片，任意格式混搭）**或**联网取回的**公开资料**，组织成一份符合公司模板规范的 `.pptx`：
取材 → 提炼大纲 → 设计版式 → 排入模板 → 导出。
**成品里的每一个字、每一张图都可溯源** —— 要么来自素材，要么带来源标注。

### 🧭 输入分流（按「调用方给了什么」走哪条路）

| 调用方给的 | 走什么 | 联网 |
|---|---|---|
| **素材**（上传文件 → 后端打包成 zip），且够用 | `0b` 提取 →（无缺口，跳过 `0r`）→ Step 1 | ❌ |
| **素材**，但有**外部可验证的事实缺口**，或**正文撑不满目标页数** | `0b` 提取 → 通读 → `0r` **定点补缺口** | ✅ |
| **素材**，但**只有图片、没有文字**（zip 里只放了照片） | `0b` 提取（图片照收）→ `0r` **全量取材**补文字；**图片保留作实拍配图** | ✅ |
| **只给一句主题**（或**贴了一段文字**） | 直接 `0r` **联网取材** | ✅ |
| 🔴 **什么都没给**（无素材 + 无主题） | **终止**，回报「请提供素材，或指明主题」 | — |
| 🔴 **素材与主题明显不相干**（说 A 却给了 B / 传错文件） | **终止**，回报「素材内容与主题不符，请确认是否传错文件」 | — |

> 🔴 **最后两条是硬终止，别硬做。**
> · **无素材 + 无主题**：`0r` 要先「定检索词」，**素材和主题都没有就没有输入** ——
>   继续跑只能靠模型自己编一个主题，那正是本 skill 的红线（边界 1）。**别等到 `0r` 才失败，入口就终止。**
> · **素材与主题明显不相干**：传错文件 / 说 A 给 B。硬做出来的成品**用户要翻到最后才发现**，比早问一句糟得多。
>   判据是「**主体是否明显不相干**」—— **素材一半以上板块都对不上主题**才算；只有一两块没覆盖是"缺口"（走第 2 条），不是"不相干"。

> 🔑 **顺序很重要**（详见 `step_0_extract.md` §0r）：**零素材要立刻取材**；
> **有素材必须先转换、读完，再判缺口** —— 否则不知道缺什么。
>
> 🔌 **没有检索工具时怎么找 URL**（沙箱常态）：**搜索接口常常不可用**（2026-10-08 实测：百度网页反爬；
> 必应 / 360 / 搜狗上 `site:` 语法失效）⇒ 🔴 **别猜文章 URL**，改用
> **「栏目页 → `--list-links` 提链 → 批量抓」**（见 §0r B 段的「栏目页路线」）。
>
> 📌 **上游形态由前端保证**：**只要用户上传了文件（哪怕只有一张图片），后端一律打包成 zip** ⇒ Step 0 的素材入口**只认 `.zip`**，不需要处理"单个散文件 / 多文件 / 目录"。
>
> 💡 **"贴了一段文字"怎么归**：**缺省当「主题来源」** —— 读它 → **总结出一个主题** → 走 `0r` 全量取材（用户贴的通常是一句需求，如"帮我做个高处作业安全的 PPT"）。
> ⚠️ **但若它本身是成篇正文**（有分段 / 条目的成品文字、篇幅明显超过一段话）⇒ **落成 `assets/sources/文本.md` 当素材用**（走上面第 1 条路线），🚫 别把调用方给的内容丢掉。
>
> ⚠️ **"缺口"只认外部可验证的事实**。要讲行业现状 / 国家标准 / 技术参数 → 是缺口，去查；
> 要讲"我们公司 Q3 进展" → **不是缺口，是素材没给**，网上也查不到 ⇒ 必须回报、请调用方补素材。

### ⛔ 不做什么（硬边界）

| # | 边界 | 说明 |
|---|------|------|
| 1 | **不生成、不推断** | 素材里没有、**网上也查不到**的信息**不得出现在成品里** —— 不补充、不推断、不"合理发挥"。不足时**宁可少讲**。 |
| 2 | **不编造来源** | 联网取到的每条外部事实**必须留下 `source_url`**（`_web_text_facts.json`），引用了它的页**必须带内容来源标注**。🚫 **禁止**把搜到的零散内容拼成一份"看着很满"的 PPT。 |
| 3 | **搜不到就终止** | 联网取材若**搜不到实质相关内容**，**终止**并回报「该主题无法从公开信息获取」——**不拿无关内容凑数**。 |
| 4 | **不做事实核查** | 素材 / 网文里的数据、事实、结论由调用方负责；本 skill 只负责组织与呈现，不判断真伪、不改写原意。⚠️ 网络内容**未经核实**，对外材料请自行复核。 |
| 5 | **不越出模板规范** | 版式、配色、字体、间距一律服从 **009 创作契约**（`references/` 下契约三册），不因"更好看"而破例。 |

> 🔴 **为什么边界 1~3 是红线**：排版出错，用户**一眼能看出来**；而**编造的内容看起来同样专业**，
> 用户没有对照物，**发现不了**。一份"看着像样、其实是编的"PPT 交出去，后果远重于排版难看。
> ⇒ 本 skill 的选择始终是：**宁可交回「请提供素材」或「查不到」，也不交回一份编出来的成品。**
>
> 💡 **增强路径（产品层）**：若调用方希望在素材之外再给一句**主题/要求**（如"给班组讲的安全培训，不是技术汇报"），
> 应作为**附加信息**传入（如素材包内放 `要求.md`），用于提升章节取舍的准确度 ——
> 但它**只影响"怎么组织内容"，不能作为"内容来源"**，不能改变边界 1~3。

## 📦 依赖安装

首次使用前，安装核心 Python 依赖（覆盖 `.docx` 提取 → `.pptx` 导出的完整链路）：

```bash
pip install mammoth
pip install Pillow
pip install python-pptx
pip install reportlab
pip install svglib
pip install requests
pip install beautifulsoup4
pip install curl_cffi
```

> ⚠️ **镜像源提示**：若某包安装时报 `from versions: none` 或 `No matching distribution`（默认清华 tuna 源偶发缺包，尤其 `reportlab`、`svglib`），换阿里云镜像重试：
> ```bash
> pip install reportlab -i https://mirrors.aliyun.com/pypi/simple/
> ```
> 装完后用 `python -c "import reportlab, svglib, requests, bs4"` 验证核心包都 import 成功再继续。

| 包名 | 用途 | 备注 |
|------|------|------|
| `mammoth` | .docx → Markdown | 纯 Python |
| `Pillow` | 图片处理 | 纯 Python，import 名 `PIL` |
| `python-pptx` | .pptx 读写 | 纯 Python，import 名 `pptx` |
| `reportlab` | PDF/图形生成 | 纯 Python |
| `svglib` | SVG → PNG 转换 | 纯 Python，部分渐变可能丢失，但无需系统库 |
| `requests` | **§0r 联网取材**的 HTTP 请求 | 纯 Python，import 名 `requests` |
| `beautifulsoup4` | 网页正文解析 / `.html` 提取 | 纯 Python，import 名 `bs4` |
| `curl_cffi` | §0r 抓取的 TLS 指纹伪装 | 纯 Python；**不装会自动降级 `requests`**，但抓公众号等站可能被 JA3 拦 |

> 以上 8 个包为**必需依赖**，任何素材都装。
>
> 🔴 **为什么后 3 个也是「必需」而不是「按需」**（2026-10-08 改）：**§0r 联网取材是无开关的固定路径**，
> 零素材或有事实缺口时都会走它。而**平台沙箱是一次性环境**，每次任务都得重装 —— 实测**每次都在这里卡一轮**
> （先跑脚本 → `web_to_md.py` 报 `exit 1` → 回头装依赖，白费一轮加等待）。
> ⇒ **开工前一次性装齐**，别等它报错。

### 按需依赖 —— 素材里出现对应格式就必须装

**素材格式由调用方决定，事先不可知。** 缺对应依赖时 `extract_all.py` 会在 Phase 2.5
**预检并直接终止**（不会静默丢素材），但提前装齐能避免流程中断。

| 依赖 | import 名 | 素材里出现…就必须装 | 安装命令 |
|------|----------|-------------------|---------|
| `PyMuPDF` | `fitz` | `.pdf` | `pip install PyMuPDF` |
| `openpyxl` | `openpyxl` | `.xlsx` `.xlsm` `.xls` | `pip install openpyxl` |
| `markdownify` | `markdownify` | `.html` `.htm` 的部分转换路径 | `pip install markdownify` |
| `ebooklib` | `ebooklib` | `.epub` | `pip install ebooklib` |
| `nbformat` / `nbconvert` | `nbformat` | `.ipynb` | `pip install nbformat nbconvert` |
| `cairosvg` | `cairosvg` | 想要更好画质的 SVG→PNG（需系统装 Cairo 库；不装则自动降级 `svglib`） | `pip install cairosvg` |

> 🔴 **建议一次性装齐**（平台沙箱是一次性的，每次任务都要重装）：
> ```bash
> pip install mammoth Pillow python-pptx reportlab svglib requests beautifulsoup4 curl_cffi PyMuPDF openpyxl markdownify
> ```
> ⚠️ **`PyMuPDF`(fitz) 和 `openpyxl` 是最容易漏的两个** —— 分别对应 pdf 与 xlsx，
> 缺了会让那几篇文档在 Step 0 提取时转换失败、内容进不了 `文本.md`。

## ⚠️ 首次加载 —— 定位 Skill 根目录

本 skill 可能安装在 SkillGo 平台（如 `/workspace/skills/01-tcc-ppt/tcc-ppt-skill/`）、
用户级（`~/.workbuddy/skills/`）或项目级（`.workbuddy/skills/`），
目录名也可能是任意字符串（不一定是 `tcc-ppt-skill`）。

**首次使用本 skill 时，必须先确认 skill 根目录的实际路径**：

1. **最优先**：本 SKILL.md 已被加载，取其所在目录即为 skill 根目录。例如：
   - SkillGo 平台：`/workspace/skills/01-tcc-ppt/tcc-ppt-skill/SKILL.md` → &lt;SKILL_ROOT&gt; = `/workspace/skills/01-tcc-ppt/tcc-ppt-skill`
   - 本机：`~/.workbuddy/skills/tcc-ppt-skill/SKILL.md` → &lt;SKILL_ROOT&gt; = `~/.workbuddy/skills/tcc-ppt-skill`
2. **备用方案**（如果 SKILL.md 的文件路径未知）：用 `glob` 在 `~/.workbuddy/skills/`、`.workbuddy/skills/` 和 `/workspace/skills/` 中搜索 `scripts/build_svgs.py`，提取其父目录的父目录。
3. 将找到的路径记为一个变量（如 `SKILL_ROOT`），后续所有 `cd`、`python scripts/...` 和
   文件读写都基于此路径。
4. 如果以上都找不到，**报错终止**并输出：「无法定位 skill 根目录 —— 请确认 skill 已正确安装」。

> 🔴🔴 **探索文件时【绝不做全量递归列举】—— 尤其别碰 `assets/icons/`。**
> 它有 **8,354 个 svg**，占整个 skill 文件数的 **98%**（其余所有文件加起来只有 **175** 个）。
> 平台/沙盒的「列出工作区文件」类工具会因为结果过大而**直接报错**
> （实测：`Sandbox returned an invalid file list: invalid JSON`，自动重试 3 次全失败，白耗 20+ 秒）。
>
> ✅ **正确做法 —— 按需、按路径读：**
> - **定位 skill 根**：直接取本 SKILL.md 的所在目录（见上），**根本不需要列举**；
> - **找脚本 / 文档**：用**限定子树**的搜索（如 `Glob("scripts/**/*.py")`），或直接 `Read` 已知路径；
> - **要图标名**：用 `Glob("assets/icons/<lib>/*关键词*")` **先按名字过滤**；或 `python -c "import os; print(os.listdir('assets/icons/tabler-filled')[:50])"`。
>   🔴 **别用 shell 的 `ls` / `head`** —— 本环境禁用 Git Bash（见 `references/pitfalls.md`）。
>   **永不整目录 dump、也不搜全库。**
>
> ⚠️ 这不只是"慢"：**工具报错会让人误以为流水线坏了**，而真正该做的是按需读取。

## 目录结构

```
<SKILL_ROOT>/
├── assets/
│   ├── templates/       ← 模板根目录
│   │   ├── 009_content.svg          ← 🔑 创作页（**共享，不分风格**）+ 走配色
│   │   ├── native_payloads.json.gz  ← 009 页头回填数据（路径写死，勿移动）
│   │   └── default/                 ← 🔑 门面页**风格**子目录（一套风格 = 一个文件夹）
│   │       ├── 001_cover.svg / 002_toc.svg / 002_toc_3ch.svg
│   │       ├── 003_toc.svg / 010_ending.svg
│   │       ├── pipeline_config.schema.json ← 🔑 占位符定义（**按风格各一份**）
│   │       └── template_capacity.md        ← 🔑 容量表（**按风格各一份**）
│   │                    ⚠️ 换风格 = 复制整个 `default/` 文件夹改名（如 `warm/`）
│   │                       再替换 5 个门面页 SVG 与其 schema/capacity 数值，
│   │                       **不是改色** —— 见下方「风格 vs 配色」
│   ├── images/          ← 模板底图
│   ├── palettes/        ← 🎨 配色方案（roles.json 角色表 + default/orange 方案）
│   │                    ⚠️ **只服务创作页（009）**；门面页不吃配色
│   ├── icons/           ← 🔴 全局图标库（4 库 8,352 图标）⚠️ 勿做全量列举（见上）
│   ├── sources/         ← 用户素材（extract_all.py 提取到这里）
│   ├── svg_body/        ← 009 创作页正文 SVG 片段（Step 2 创作）
│   ├── svg_output/      ← [中间产物] build 输出
│   ├── svg_final/       ← [中间产物] finalize_svg 输出（自包含）
│   ├── design_spec.md   ← PPT 内容大纲（AI 编写）
│   └── pipeline_config.json ← 🔑 流程配置入口（门面页占位符 + 009 页引用 body_svg）
├── icons/               ← 本项目已选图标池（由 icon_sync.py 从 assets/icons/ 拷入）
├── scripts/             ← 自动化脚本
├── references/          ← 参考文档
│                          ⚠️ **不在这里**：`pipeline_config.schema.json` 与
│                          `template_capacity.md` 已改为**按风格各一份**，
│                          位置 = `assets/templates/<风格名>/`（见上）；本目录不再有同名文件
└── exports/             ← [最终产物] YYYYMMDD_项目名.pptx（同名自动另存 -02）
```

> 🗂️🔑 **风格 vs 配色 —— 一整套，原子切换**（2026-10-09 确立 / 方案 C-1）：
>
> | | **风格**（template_set） | **配色**（palette） |
> |---|---|---|
> | 管什么 | **门面页**（封面/目录/章节/致谢）的整张版式 | **创作页（009）**的矢量色值 |
> | 怎么换 | **复制整个 `templates/default/` 文件夹改名**，替换 5 个门面页 SVG + 同步其 schema/capacity | 改 `assets/palettes/*.json` 色值 |
> | config 字段 | `_meta.template_set`（**一般省略**） | `_meta.palette`（缺省 `default`） |
> | 生效范围 | 只换门面页 | 只换 009 |
>
> 🔑🔑 **缺省行为（方案 C-1）= 整套原子切换**：
> `_meta.template_set` **省略**时，脚本从 `_meta.palette` **派生同名风格目录**，且**两者要么都换、要么都不换** ——
> - `templates/<palette 值>/` **存在** ⇒ **整套用它**（`[PPT主题：orange]` ⇒ 门面页 + 009 **一起变橙**）；
> - **不存在** ⇒ 🔴 **配色也一并回退 `default`**（打印一行说明，**不中断**）
>   ⇒ 成品是一份**完整的 default（商务蓝）稿件**，**不会出现"蓝封面 + 橙正文"的半成品混搭**。
> - ⇒ **同一句提示词，在 `templates/orange/` 建好前后自动切换**，不必改 config、不必改提示词。
>
> ⚠️ **唯一例外**：若**显式声明**了 `_meta.template_set`（想刻意混搭「某门面风格 + 某配色」），
> 则**不做原子回退**，配色照常生效。这是给高级用法留的口子，一般不用。
>
> 🔑 **为什么门面页不吃配色**：门面页的风格本质是**整张版式设计**（底图 + 光效 + 构图），
> 不是几个十六进制色值 —— 它的观感主色来自**照片**，改矢量色值改变不了整体风格。
> 想给门面页换风格，正确做法是**换一整套模板**。
>
> ⚠️ **别拿 `[PPT主题：xxx]` 的值直接拼资源路径**：配色 id **不保证**对应同名风格目录。
> `templates/default/` 永远是安全兜底。

## 工作流程

> **所有 `cd` 和 `python scripts/...` 命令均在 &lt;SKILL_ROOT&gt; 目录下执行。**
> 执行每步前先 `cd` 到上面首次加载时确定的 skill 根目录。
>
> **Python 命令兼容性**：优先用 `python3`，若不可用则回退 `python`。
> `python -c "..."` 一行命令（文件清空、删除等）同理。
> ⚠️ **`python -c "…"` 只用于「只读查看」**（如 `os.listdir` 数文件）；
> 🔴 **凡是「写操作」（删文件 / 建目录 / 改文件）一律不要内联** ——
> **平台上每次调用都要走一轮工具确认**，内联写操作实测让模型连撞 4 次「纠正工具参数」（C919 轮，2026-10-10）。
> ✅ **正解：能用现成脚本就用脚本**（如清理走 `scripts/validators/clean_research.py`）。
>
> **🚫 本环境禁止用 Git Bash / PowerShell 执行 shell 命令**（唯一例外见下）：
> 这个 Windows 环境里 Git Bash 的 shim 本身已损坏（`dirname` / `tail` 等报 `command not found`）、还会吞反斜杠（`C:\Users\...` → `C:Users...`），
> PowerShell 的 `Remove-Item` 通配符可能误删。
> **所以连 `ls`、`cat`、`find`、`grep`、`unzip -l`、`echo` 这类「只读查看」也一律不要用**，统一用 Python 一行命令。
> ✅ **唯一例外**：用 shell 启动 `python xxx.py` / `python -c "..."`——shell 只是启动器，文件操作在 Python 内部完成，安全。

按顺序执行以下步骤。每步有对应的 reference 文件提供完整说明——执行该步骤时加载。

**🔄 出错怎么回退 —— 两套模型别混**

| 情况 | 怎么判断 | 回退到哪 | 要先清空吗 |
|------|---------|---------|-----------|
| **① 脚本报错**（运行时故障） | 脚本打印 ❌ / 抛异常 | 修好问题 → 回**最近能重跑的步骤**（查当前 step 的「失败处理」表） | ✅ 回 Step 3 时**必先清空** |
| **② 内容要改**（想换东西） | 你主动想改 | 小改 → 回 **Step 3**；大改 → 回 **Step 1** | ✅ 同上（历史 pptx 保留） |

> 🔑 **共同铁律**：凡回到 Step 3，**都要先清空**：`python scripts/cleaners/clear_downstream.py`（build 是覆盖写、不删多余文件，不清空会残留旧页混入本轮）。清空规则统一记在 `step_3_build.md` §3b。
>
> ⚠️ 不确定该回哪步时：**先查当前 step 的「失败处理」表 → 再查 `references/pitfalls.md`**。

### Step 0-pre: 解析提示词标记

> 🔑 **进入 Step 0 之前，先把提示词末尾的「方括号标记」读出来记下。** 后面几步都要用到这两个值。

调用方会把**用户输入的指令**与**两个标记**拼成一条，格式固定为：

```
<用户输入的指令原文> [联网搜图：ON/OFF][PPT主题：<配色 id>]
```

> 🔴 **你读到的就是最终形态** —— 前端模板里那段 `#用户提示词#` **已被用户指令替换掉了**，
> **发给你的内容里没有 `#用户提示词#` 这个字符串**。它只是前端模板的占位符，不是要解析的内容。
> ⚠️ **别去找它、也别把它当输入标志**：直接读「用户指令 + 末尾两个方括号标记」即可。
>
> 📌 例：用户写了「请以"茶马古道"为主题，创作一份15页的PPT。」⇒ 你实际收到的就是
> `请以"茶马古道"为主题，创作一份15页的PPT。 [联网搜图：ON][PPT主题：default]`

两个标记**按此顺序紧挨着**写在最末尾；方括号为**半角 `[ ]`**，冒号为**全角 `：`**。

| 标记 | 取值 | 含义 | 取不到时（缺省） |
|---|---|---|---|
| `[联网搜图：ON]` | `ON` | 允许联网补图（§0d 才会执行） | ✅ **缺省 = ON**（2026-10-08 起） |
| `[联网搜图：OFF]` | `OFF` | **禁止**联网补图 | — |
| （整个标记缺失） | — | — | **按 `ON` 处理**（要禁必须显式写 OFF） |
| `[PPT主题：<配色 id>]` | `default` / `orange` | 选哪套**创作页（009）配色方案** | **按 `default`（商务蓝）处理** |
| `[PPT主题：其它值]`（如 `pink`） | — | **无效 id** ⇒ **按 `default` 处理**，但要**在收尾告知调用方** | — |

> ⚠️ **`[PPT主题：xxx]` 装的是「配色方案 id」，不是内容主题。**
> 它只可能是 `default`（商务蓝）或 `orange`（活力橙）—— 前端会把用户看到的**中文配色名翻译成这两个英文 id** 再拼进来。
> 📌 将来若还要传"内容主题提示"，那是**另一件事**，不属于本标记。
>
> 🔴 **它只管创作页（009）的配色，管不到门面页**（2026-10-09 用户拍板）。
> 门面页（封面/目录/章节/致谢）的风格由**模板本身**决定 —— 那是 `_meta.template_set` 的事，与本标记无关。
> ⇒ 收到 `orange` 时，**封面仍是蓝的、目录圆环仍是蓝的**，这是**正确行为、不是 bug**。
>
> 🔴 **只认这两个值。写了其它值（如 `pink`）→ 一律按 `default` 处理，不报错、不终止**；
> 但**必须在收尾时告知调用方**（如「收到无效配色 id `pink`，已按商务蓝出片」）——
> 否则"用户选了活力橙、却拿到商务蓝"会**无人知晓**，交付之后才发现。
> 不放心目前有哪些方案 → `ls assets/palettes/*.json`（`roles.json` 是角色表，不是方案）。
>
> 🔴 **记在脑子里的应是修正后的 `default`，别把无效值原样往下传**：Step 2 写 `_meta.palette` 时就要写对。
> Step 3 虽有兜底回退 + 告警（见下方 Step 3 区块），但让 config 里留个假值，会让**数据与实际行为不一致**。
>
> 🔴 **判定只看提示词 / 任务描述这一处**，**不要**去素材包里找配置文件（没有那种东西）。
> 🔴 **两个标记都缺失时一律取默认值，不要报错、不要终止流程** —— 它们是**可选增强项**，不是必填项。
>
> 📤 **记住这两个值**（后续会用到）：
> - `联网搜图` → 决定 Step 0 的 **§0d 跑不跑**（**缺省 ON**；显式 OFF 时整步跳过，走无图版式）。
> - `配色 id` → 决定 **Step 2 写 `_meta.palette`** 时填什么。
>
> 💡 建议在开始正式步骤前**用一句话复述**解析结果（如「已解析：联网搜图 = ON（缺省），配色 = default（商务蓝）」），便于自查。

> 🔌 **联网搜图开关默认开启**（2026-10-08 起）：图量不足时**会**去联网补图；
> 只有调用方**显式写** `[联网搜图：OFF]` 才走**无图版式**
> （§5.2 多栏并列 / §5.4 表格式 / §5.6 卡片矩阵 / §5.10 编号条目列等 —— 契约 §5.10.1 已解决"纯条目列填充率 0%"）。
>
> 🔴 **"能联网"不等于"该配图"**：**抽象概念页**（对标 / 生态 / 路线 / 战略 / 机制 / 方法论 / 架构 / 评估 / 认证…）
> **即便联网也不配图** —— 这类词搜回来只有示意图与资质证书（证书上还印着别家公司名，会造成误导）
> ⇒ 一律走**无图骨架**。详见 §0d「取词铁律」与契约 §1.7。

### Step 0: 素材提取

> 📥 输入：调用方提供的 `.zip` 素材包（后端统一打包，内含任意格式混搭）**，或只给一句主题**（零素材 → §0r 联网取材）  |  📤 输出：`assets/sources/文本.md` + `assets/sources/image_NNN.*`（图片文件）

> ⚠️ **本步骤有完整的子流程，严禁只跑 `extract_all.py` 一行命令。**
> **一次性全文加载 `references/step_0_extract.md`**（约 970 行 / 约 62 KB，一次读完即可，分段读反而容易死循环）。子步骤命令和验证标准都在里面。
> 📖 **§0r 的背景/实测记录在 `references/pitfalls.md` 的「联网取材（§0r）的坑与实测」一节** —— 执行时不必读，换主题取材效果差 / 想改选源策略时再查。
> ⚠️ 若单次读取被截断（少见），**从断点继续往下读**；判据是**读到 §0g 收尾**。
> 🔴 **子步骤不是全部必跑** —— **§0b 要有 zip 才跑**、**§0r 是有条件的**（判据见下表「走什么」列 + 开头「输入分流」表）；其余必跑。**执行顺序总表**在 `step_0_extract.md` 开头。

| 子步骤 | 做什么 | 读什么 |
|--------|--------|--------|
| **0a** | 准备干净的 `assets/sources/`（旧素材自动清空） | step_0_extract.md §0a — 一条 `python scripts/validators/check_sources_dir.py` |
| **0b** | 执行提取（zip 递归解压 + 按类型分发 + 散图收编）**+ 收尾量一次正文体量** | step_0_extract.md §0b — **两条命令一起跑/一起跳**（没 zip 都跳过）：① `python scripts/extract_all.py "<素材.zip>"`（提取）→ ② `python scripts/validators/verify_extraction.py --pages 15 --facade 7`（**只看 `📊 正文体量评估` 段**；达成率 < 100% ⇒ 体量缺口，记下缺口字符数，供 §0r 用） |
| **0r** | **联网取材（文本素材）** —— 🔌 **不能被提示词关闭，但也不是每次都跑**：跑不跑看「**调用方给了什么**」+「**§0b ② 的达成率 + 通读检查表**」（**五种情形**见下） | step_0_extract.md §0r — **从上往下照做（A→F，无编号）**：**A** 定位补什么（缺口/体量/全量）→ **B** 找 URL（`--search`，🔴🔴 **起手式 = 1 个短核心词（2~4 字），默认就从"能少"开始** —— **别把主题原样搬进去**：`LNG 接收站 工艺流程` ⇒ **`LNG 接收站`**（`工艺流程`/`分类`/`工法`/`系统构成` 这类**看着像主题一部分、实为通用词**，一加就把专有实体冲掉，实测多烧 3 次检索 / 约 7 分钟；🚫 **`报告`/`综述`/`分析` 是体裁词，直接删**）。🔴 **剥光后太泛（只剩缩写/泛称如 `LNG`）⇒ 再补 1 个「专有实体」落地**（`LNG` ⇒ `LNG 接收站`），🚫 **补的绝不许是通用词** —— 判据：**该词单独搜能不能指向同类实体？**；命中若打 `[WARN] 查询词疑似被切碎` ⇒ **照它给的建议词重试**。🔴🔴 **换通道的唯一门槛 = 先试过「剥光修饰语的核心词」**（`城市黑臭水体治理：成因分析…` ⇒ **`黑臭水体`**；`LNG工艺流程报告` ⇒ **`LNG 接收站`**）—— **没试过就不许换** 360 / 栏目页；每条通道**最多试 2 条词**）→ **C** 抓（`source_to_md.py "<URL>" -o "assets/sources/_research/研究_<slug>.md" --no-images`，🔴 **抓一篇判一篇，连丢 2~3 篇就换通道**；🔴 **看脚本 WARN 判质量**：`shell page` = 太短、**`read as prose` = 栏目壳页**（长度够但全是链接导航），**命中任一就丢弃换源**）→ **D** 合成 `文本.md` + `_web_text_facts.json`（🔴 **facts 用脚本生成，🚫 不许手写 JSON**：先把事实抄成纯文本一行一条，再 `python scripts/emit_facts.py --topic "<主题>" --mode full facts.txt`）→ **E** 🔴 **清理**（删 `_research/` 与 `*.conversion_profile.json`）→ **F** 回 §0c 复验。📖 背景/实测见 `pitfalls.md`「联网取材（§0r）的坑与实测」 |
| **0c** | 素材概览（清单）**+** 提取校验（**一条命令跑两项** —— 同一步内、只读、互不依赖） | step_0_extract.md §0c — 一条 `python scripts/validators/check_step0.py --pages <PPT总页数> --facade <6 或 7>`（文本非空 / **正文非空（只有图片时会提示转 §0r）** / **正文体量是否撑得住页数** / 图片数 / 占位符一一对应 / **图量是否够用**）。🔴 **调用方没给页数时一律传 `--pages 15 --facade 7`**（缺省规模 15 页 / 4 章） |
| **0d** | **图量不足时的联网补图** —— 🔌 **缺省 ON**（2026-10-08 起联网）；仅当显式写 `[联网搜图：OFF]` 时**整步跳过** | step_0_extract.md §0d — **智能体先通读 `文本.md` 定 3~5 组主题词**，再 `python scripts/fetch_web_images.py --keywords "词A 词B;词C 词D" --count <缺口张数>` |
| **0e** | 了解占位符约定 | step_0_extract.md §0e — 一段说明文字（含三类图片的上下文形态） |
| **0f** | 失败处理 | step_0_extract.md §0f — 症状表（🔴 **无素材+无主题 → 终止** / 🔴 **素材与主题不相干 → 终止** / **空包 → 转 §0r** / **正文偏少 → 转 §0r 补内容** / **联网取材搜不到** / **素材包无法读取** / **依赖缺失** / 转换失败 / 图片 0 张 / 文档加密 等） |
| **0g** | 收尾引导下一步 | step_0_extract.md §0g — 中间产物已由 `extract_all.py` 自动清理（**零素材路径无中间产物**），仅需输出完成语并转 Step 1 |

> ⚠️ **0c 报「图片: 0 张」/「图量不足」/「正文偏少」都不是失败** —— 它 exit 0，只提示"料不够用"；按提示去 §0d（补图）或 §0r（补内容）即可，**别硬做**。
>
> 🔴 **§0r 跑不跑 = 开头「输入分流」表的五条**（不能被提示词关掉，但也不是每次都跑，别两种误读都犯）：
> 什么都没给 → **终止** ｜ 够用 → **跳过** ｜ 有缺口 → **定点补缺口** ｜ 只有图片 / 只给主题 → **全量取材**。
> 🔑 **"够不够用"**：**体量**看 §0b ② 的 `📊 正文体量评估`（达成率 <100% ⇒ 缺口，**别靠通读估**）；**事实缺口**通读 `文本.md` 照 §0r「**通读检查表**」过（脚本查不出）。
>
> 🔴 **图量与门面页**（口诀，细节见 §0c）：**期望图量 = 内容页数 × 2**（备选池，不一定全用）；内容页数 = 总页数 − 门面页数，**门面页数 = 3 + 章节数**。
> **章节数只能 3 或 4 ⇒ 门面页只能 6 或 7**（4 章用 `002_toc.svg`、3 章用 `002_toc_3ch.svg`）；**`--facade 6|7` 传的就是门面页数**（`--chapters 3|4` 等价别名）。
> 🔑 **Step 0 时 config 还没生成 ⇒ 必须由你先定好再传**，不传则回退兜底 7。**门面页还是「省图旋钮」：6 → 7 可让期望图量 −2 张**。章节数在 **§1b 定死**（判定表见 `step_1_design_spec.md` §1b）。
>
> 🔌 **0d 的关键是「智能体先定主题词」**：**提不出主题就不搜**（宁缺毋滥，Step 1 走纯图形/表格页）；给 **3~5 组**（`;` 分隔）；`--count` 是总张数、脚本组间均分。
> 🔴 **只给「具体实物 / 设备 / 材料 / 工艺动作」取词**，**抽象概念页（对标/生态/评估/认证/架构/机制/方法论…）不取词、不配图** → 走无图骨架（契约 §1.7）；取词公式与**禁用词表**见 §0d「取词铁律」。
>
> 🔴 **"文本为空 / 料不够"都不等于失败** —— 先按开头「**输入分流**」表判能不能联网取材，🚫 别自行编造。
> ⚠️ 若联网取材**搜不到实质相关内容**（尤其主题属**私域**，如"我们公司 Q3 进展"）→ **终止**并回报「该主题无法从公开信息获取」。

### Step 1: 编写 design_spec.md

> 📥 输入：`assets/sources/文本.md` + `references/design_spec_reference.md`  |  📤 输出：`assets/design_spec.md`

**Step 1 开始前，一次性全文加载 `references/step_1_design_spec.md`**。获取写作流程、选模板策略、009 创作蓝图写法与字数/填充率校验规则。

🔴 **规划页面结构时必须先数照片**（Step 1b 前置操作）——拿到 `sources/` 照片清单，供 009 创作页自由引用。

**数据字典按需查阅**（🔴 `pipeline_config.schema.json` 和 `template_capacity.md` **按风格各存一份**，放在 `assets/templates/<风格名>/` 下 —— 写哪页查哪个模板，**先按 `_meta.template_set`（缺省 `default`）找到当前风格目录**；009 创作页**正文**的规范在**契约三册**，不查 schema——但它自己的 `PAGE_TITLE` 仍按 schema 校验）：

| 时机 | 读什么 | 怎么读 |
|------|--------|--------|
| **1a 开始前** | `references/design_spec_reference.md` | ✅ 全文 |
| **1a 开始前** | `assets/sources/文本.md` | ✅ 全文 |
| **1a 开始前** | **契约 ①** `references/creative_contract.md` + **契约 ③** `references/contract_layout.md` | ✅ **两册都读全**（009 创作法典）—— ①=§0 速览 / §1 红线 / §2 铁律（**498 行**）；③=§5 版式 / §6 照片 / §7 自检（**710 行**）。⚠️ **③ 偏大 ⇒ 读取可能被截断**，判据是**读到 §7 结尾**，没到底就从断点续读（② 册本步用不到，留给 Step 2） |
| **1b 数照片** | `scripts/validators/count_photos.py` | 一行命令 |
| **1d~1e 写某页（门面页）** | `assets/templates/<风格名>/pipeline_config.schema.json` → `_template_reference.<当前模板>.placeholder_schema` | 🔑 只查当前模板 |
| **1d~1e 写某页（门面页）** | `assets/templates/<风格名>/template_capacity.md` → 对应模板那节 | 🔑 只查当前模板 |
| **1e 写 009 创作蓝图** | 契约 **③** §5 版式家族（**19 种**）+ 契约 **①** §2 六条铁律（反重复 / 间距 / **左右等高** / 同色 / 填充 / 仲裁） | 🔑 先定版式骨架（**相邻页不重复 + 任一骨架 ≤1/2**），再查坐标；🔴🔴 **「布局框」只从 §5.0「标准布局框速查」抄**（已覆盖全部 19 骨架），**别自己算** |
| **1e 写 009 页面标题** | `assets/templates/<风格名>/template_capacity.md` → `009_content` 节 | 🔑 只有 `PAGE_TITLE` 一个占位符，**≤15字**；**用与门面页相同的占位符表格行写**（`| `PAGE_TITLE` | 内容 | `≤15字 ✓` |`），不要写 `**页面标题**：` 字段行 |
| **1c 写 §VI 图标** | `assets/icons/README.md` | 🔑 **默认启用，必须明确填**：选一个风格库（`tabler-outline` 最百搭）填库名+基调；只有纯数据/无区块结构时填 `None`。🚫 **不许留空**。**页面大标题不放图标** |
| **1e-2 动笔前自检（🔴 必过）** | `references/step_1_design_spec.md` → §1e-2（**10 条硬约束清单**） | 🔴 **写蓝图之前逐条扫一遍**：封面 `TITLE`≤18 / `PAGE_TITLE`≤15 / `CHAPTER_TITLE`≤6 / `TOC`≤8 ｜ 布局框顶边 `104` ｜ 最下框底边 ≥608 ｜ 内容量 ≥454（**素材量问题**）｜ 图框 AR ≤2.8 ｜ 骨架相邻不同族 + 任一 ≤1/2 ｜ §VI 图标必须填 |
| **1f 设计稿校验** | `scripts/validators/check_design_spec.py` | 一行命令（① 占位符字数 + ② **版式骨架反重复** + ③ **布局框 7 项**：底边≤648 / 同列间距≥24 / 左右等高 / 收口≥608 / 跨度≥504 / 密度≥50% / 内容量≥454） |
| **1g 全局审核** | `assets/templates/<风格名>/pipeline_config.schema.json` → `_template_reference.<模板>.placeholder_schema` | ✅ 一次读完 |

design_spec.md 写入 `assets/design_spec.md`。

> ⚠️ **出错时查** `references/step_1_design_spec.md` 的 **§1h 失败处理**：字数超限 / 009 漏占位符表 / 照片不够 / 版式骨架重复。

### Step 2: 生成 pipeline_config.json + 正文 SVG 片段

> 📥 输入：`assets/design_spec.md`  |  📤 输出：`assets/pipeline_config.json` + `assets/svg_body/pageXX.svg`（009 创作页正文片段）

**Step 2 开始前，一次性全文加载 `references/step_2_pipeline_config.md`**。获取 JSON 格式硬规则、字段结构、009 创作页写正文 SVG 的流程、自检命令。

**数据字典按需查阅**（schema.json 含全部模板定义，写哪页查哪个模板；009 正文规范在**契约三册**）：

| 时机 | 读什么 | 怎么读 |
|------|--------|--------|
| **2a 开始前** | `assets/design_spec.md` §VIII + §IX | ✅ 全文（核心内容源） |
| **2a 开始前** | 契约三册：① `creative_contract.md` + **② `contract_elements.md`** + ③ `contract_layout.md` | ✅ **② 是本步主用册**（§3 引擎 + §4 元素组件）。①③ 若 Step 1 已读且上下文未丢，**只需补读 ②**；三册都丢就三册都读。行数 ① **498** / ② **425** / ③ **710** —— ⚠️ **③ 偏大，读它时确认读到 §7 结尾** |
| **2c 填每页 text** | `assets/templates/<风格名>/pipeline_config.schema.json` → `_template_reference.<当前模板>.placeholder_schema` | 🔑 按模板查 |
| **2c-2 写 009 正文** | 契约 **②** §4.8 组件库 + 契约 **③** §5 版式家族 + 契约 **①** §2.2b 分栏等高 | 🔑 优先复用组件；禁止相邻页同骨架、单一骨架 >1/2；分栏页左右底边差 ≤24px。🔴 **落笔前先按「栏宽 → 单行可容字数」自查**（契约 ③ §5.0 ②：三栏 ≤19 字 / 四栏 ≤14 字）——**多栏越界是 Step 2 第一大返工源** |
| **2c-2 用图标** | 契约 **②** §4.9 + `assets/icons/README.md` | 🔑 **默认执行**（**只有** §VI 填了 `None` 才跳过）：**先** `python scripts/icon_sync.py . 库/名 …` 同步，**再**在卡片/条目小标题前写 `<use data-icon>`（页面大标题不放） |
| **2c-2 校验 009 正文** | `scripts/validators/check_creative_svg.py` | 一行命令。🔴 **主节奏 = 一口气把全部页面写完 → 跑一次全量校验 → 按报错分别返工**（🚫 别分批写、别写一页跑一次 —— 实测净亏 342s 且发现问题时机不变）。改错要**一次改到位、别挤牙膏**，且**改完必须回读确认**（用 `sed` 局部替换多字节中文会**写不进去且不报错**） |
| **2d 标点检查** | `references/step_2_pipeline_config.md` §2d | 只读表格 |
| **2e 配置全量校验** | `scripts/validators/check_step2_config.py` | **一行命令**（内部依次跑三项：① 字数 / ② JSON 完整性六项 / ③ 槽位；**全部执行、一次报全**） |
| **2f 来源标注注入** | `references/step_2_pipeline_config.md` §2f（+ 契约 §1.6 规格） | 一行命令 `python scripts/inject_image_credits.py`（**图片行 + 文字行**） |

> 🔑 **2e-③ 槽位校验的关键优势**：脚本内部会自己从 `schema.json` 的 `placeholder_schema` 取占位符清单（"模板需要哪些槽"的唯一定义处），你只需要运行命令看输出，不需要自己翻 schema.json 做人工比对。

> 🎨 **写 `_meta` 时别忘了 `palette`**：填入 **Step 0-pre 解析到的配色 id**（`default` / `orange`），缺省 `default`。
> 该字段由 Step 3 的 `build_svgs.py` 自动读取，决定配色替换。取值来源见 `step_2_pipeline_config.md` §2b。
> ⚠️ 写入**无效方案名**时，Step 3 会**回退 `default` 并打印醒目告警**（**不中断流程**）—— 但**别指望它兜底**：Step 0-pre 就该只记 `default` / `orange`。
>
> 🔴🔴 **配色只作用于创作页（009）**（2026-10-09 用户拍板）。
> **门面页（001/002/003/010）永远原样输出，不参与配色替换** —— Step 3 的 `build_svgs.py` 已在代码里限定。
> 🔑 **为什么**：门面页的风格本质是**整张版式设计**（底图 + 光效 + 构图），不是几个十六进制色值。
> 想给门面页换风格，正确做法是**换一整套门面页模板**（`assets/templates/<风格名>/`），而不是给旧模板刷漆。
> ⇒ 所以**不要**指望改 `_meta.palette` 能改变封面/目录/章节/致谢页的观感。

> 🗂️ **另有一个 `_meta.template_set`**（模板风格，**一般省略**）：选 `assets/templates/` 下哪个**子目录**当门面页源。
> 🔑 **省略时从 `_meta.palette` 派生同名目录**（存在才用）；**不存在则配色与门面页一起回退 `default`** + 一行提示，**不中断**（方案 C-1，**原子切换**）。
> 目前只有 `default`（= 现有这套门面页）；将来导入新风格时才有第二个值。
> ⚠️ **显式写**的风格名若指向不存在的目录，会**报错退出**（那是写错了）—— 但**省略时不存在不算错**。

> 🖼️ **2f 是「最后一道机械加固」**：Step 1 标了实拍/网图，但**"标了"≠"用了"** —— 真正决定加不加来源标注的，是
> Step 2 写出的片段里**实际引用了哪张图**。所以由脚本按「实际引用 ∩ 网图编号」自动注入（幂等，可重跑）。
> **写正文时不要手写来源标注，也不要在 `y>648` 放别的东西**。素材没有网图时此步无实质动作。

生成后执行全部自检通过（含 009 正文创作校验）才允许进入 Step 3 build。

> ⚠️ **出错时查** `references/step_2_pipeline_config.md` 的 **§2g 失败处理**：JSON 语法 / 字段完整 / 槽位缺失 / 009 正文校验不过 / 网图未被引用。

### Step 3: build_svgs.py — 生成 SVG 并填充文字

> 📥 输入：`assets/pipeline_config.json` + `assets/templates/` + `assets/svg_body/`（009 正文片段）  |  📤 输出：`assets/svg_output/`（N 页 SVG）

加载 `references/step_3_build.md` 获取完整说明。子步骤一览：

| 子步骤 | 做什么 | 读什么 |
|--------|--------|--------|
| **3a** | 确认前置条件（config / templates / images / svg_body 四样就位 + **模板字体自检**） | `step_3_build.md` §3a — 5 条检查；字体自检 `python scripts/validators/check_templates.py`（必须 exit 0） |
| **3b** | 清空旧中间产物（防上轮残留混入；中断重跑也要先清） | `step_3_build.md` §3b — `python scripts/cleaners/clear_downstream.py` |
| **3c** | 执行 build，生成 N 页 SVG | `step_3_build.md` §3c — `python scripts/build_svgs.py`（无参数） |
| **3d** | 验证输出（四项合检：页数 / 占位符 / 图片路径 / XML） | `step_3_build.md` §3d — `python scripts/validators/verify_build_output.py` |
| **3e** | 失败处理 | `step_3_build.md` §3e — 症状表 |
| **3f** | 完成收尾 + 引导下一步 | `step_3_build.md` §3f — 必须输出 ✅ + 引导 Step 4 |

> 009 创作页：build 会把「009 头部模板 + 正文片段（svg_body/）」拼接成完整一页；门面页照旧复制模板替换占位符。build 同时完成 XML 兼容性修复。

### Step 4: finalize_svg.py — 图片内联 + SVG 最终化

> 📥 输入：`assets/svg_output/`  |  📤 输出：`assets/svg_final/`（自包含 SVG，图片已 base64 内联）

加载 `references/step_4_finalize.md` 获取完整说明。子步骤一览：

| 子步骤 | 做什么 | 读什么 |
|--------|--------|--------|
| **4a** | 确认前置条件（Step 3 已跑、svg_output 就绪） | `step_4_finalize.md` §4a |
| **4b** | 理解三步内部处理（embed-icons / align-images / flatten-text） | `step_4_finalize.md` §4b — 全自动，无需选择 |
| **4c** | 执行图片内联 + 最终化 | `step_4_finalize.md` §4c — `python scripts/finalize_svg.py` |
| **4d** | 验证输出（三项合检：文件数 / 已内联 / 无外部引用） | `step_4_finalize.md` §4d — `python scripts/validators/verify_finalize_output.py` |
| **4e** | 失败处理 | `step_4_finalize.md` §4e — 症状表 |
| **4f** | 完成收尾 + 引导下一步 | `step_4_finalize.md` §4f — 必须输出 ✅ + 引导 Step 5 |

### Step 5: export_flat.py — 导出 PPTX 文件

> 📥 输入：`assets/svg_final/`  |  📤 输出：`exports/YYYYMMDD_<项目名>.pptx`（日期_标题；非法字符自动清洗、同名自动另存 `-02`）

加载 `references/step_5_export.md` 获取完整说明。子步骤一览：

| 子步骤 | 做什么 | 读什么 |
|--------|--------|--------|
| **5a** | 确认前置条件（Step 4 已跑、svg_final 就绪） | `step_5_export.md` §5a |
| **5b** | 理解五步内部处理 + 模板→patch 映射 | `step_5_export.md` §5b — 备份/收集/预处理/导出/恢复 |
| **5c** | 执行导出 | `step_5_export.md` §5c — `python scripts/export_flat.py` |
| **5d** | 验证输出（四项合检：PPTX 存在 / 大小 / svg_final 已恢复 / bak 已清） | `step_5_export.md` §5d — `python scripts/validators/verify_export_output.py` |
| **5e** | 失败处理 | `step_5_export.md` §5e — 症状表 |
| **5f** | 完成收尾（输出产物路径 + 清理中间产物） | `step_5_export.md` §5f |

> ⚠️ **export 报 exit 1 先别慌**：多半是收尾清 `exports/.pptx-build-*` 临时目录被本环境安全护栏拦截，PPTX 其实已导出成功。判断成败以 `verify_export_output.py`（5d）为准，🚫 千万别因 exit 1 重跑（同名会自动另存 `-02`，多出重复 pptx）。

导出完成后按 §5f 收尾：**输出产物路径**（`exports/YYYYMMDD_*.pptx`）+ 跑 `clear_downstream.py` 清中间产物，任务即结束。

> 💡 本 skill 以 **API 黑盒方式调用，全程无人工确认环节**。产物路径由脚本输出、调用方据此取件；
> 🔴 **导出后不要"渲染成图再看一眼"** —— COM / 截图 / 任何出图方式都**不做**。交付物就是 pptx，
> 想看成片效果**由调用方自己打开**；要核图注请 `Read` 直看 `assets/sources/image_NNN.*`（无需渲染）。
> 详见 `step_5_export.md` §5d 末尾的说明。
> 若调用方后续要求修改（小改 → 从 Step 3 重跑；大改 → 从 Step 1 重跑），处理方式见 `references/step_5_export.md` §5f。

> ⚠️ 即使之前跑过，也**必须逐条执行脚本**，每步检查输出确认无误后再跑下一步。禁止 `&&` 串联命令。

## 常用操作

加载 `references/common_operations.md` 获取：
- 改文字 / 换照片 / 新增页面 / 使用新模板 / 修改模板 / 项目重置
- **遇到问题时先查 `references/pitfalls.md`**——大部分常见问题都能在那里找到原因和解决方案
- 排查指南：先查 pitfalls → 再查 common_operations → 看脚本输出 → 验证命令排查

## 踩坑记录

**加载 `references/pitfalls.md` 获取已知问题及修复方案。**

遇到任何奇怪问题时，这是第一份要查的文件。涵盖：照片/图片问题、JSON/字数问题、SVG/模板问题、
Shell/Python 执行问题、自动化流程问题。如果表中没有，排查完后补充进去。

## 核心规则

1. **🎯 只做「资料 → PPT」，不编资料**：成品里的**每个字、每张图都必须可溯源** ——
   要么来自 `assets/sources/`（素材），要么来自**带来源标注的公开网络资料**（§0r 的 `_web_text_facts.json`）。
   联网取材**搜不到实质内容就终止**；🚫 **禁止**凭主题自行编造，也**禁止**拿无关内容凑数。
   素材 / 网文里没有的信息**不补充、不推断**——宁可少讲，也不编。（详见开头「能力边界」）
2. **配置驱动**：只改 `assets/pipeline_config.json` 和 `assets/svg_body/`（009 创作页正文片段）中的内容。绝不因内容变化而修改脚本。
3. **流程顺序不可变**：build → finalize → export。`build_svgs.py` 从 assets/templates/ 原版复制覆盖，并同时完成 XML 兼容性修复。
4. **先验字数/填充率校验**：assets/design_spec.md §IX 中门面页占位符内容必须在 build 之前 ≤ schema 的 max_chars；009 创作页的 `PAGE_TITLE` 同样 ≤15字（唯一占位符，**按占位符表格行书写，与门面页走同一套校验**），正文填充率 ≥25%。超限内容在写作阶段就压缩，不留给 build 阶段发现。
5. **009 正文从源头合规**：009 正文片段必须在 Step 2 通过 `check_creative_svg.py` 校验后才进 build，不允许带着违规 SVG（越界/配色/字体/字号/**区块重叠**/**图标**：尺寸、缺 `data-icon`、空圆圈）冲进 Step 4/5。
6. **🚫 禁止擅自删除受保护资源（铁律）**：**绝对禁止**以任何理由删除以下目录中的文件：
   - `assets/images/` — 模板底图
   - `assets/templates/` — SVG 模板
   - 🔴🔴 **`assets/icons/` — 全局图标库（8,352 个 SVG，4 个风格库）** ——
     **静态资产、永久保留**。⚠️ **别和 `<skill根>/icons/` 搞混**：那个是 `icon_sync.py`
     运行时生成的本项目图标池（可清）；`assets/icons/` 是**源库**（**绝不可删、不可打包时排除**）。
     🔑 **同名不同命**：判断一个 `icons/` 目录能不能清，**只看它在不在 `assets/` 下**。
   - `scripts/` — 自动化脚本
   - `references/` — 参考文档
   
   这些目录中的文件是人工放置/恢复的，删除后需要重新复制回来，代价极高。`sources/`、`svg_body/`、`svg_output/` 和 `svg_final/` 是中间/项目产物，可以清空；`exports/` 是最终产物，历史版本保留（`日期_标题` 命名、同名另存 `-02`，只清临时目录、不清 pptx）。其他一切目录都只读。
   > ⚠️ **打包/归档本 skill 时**：只排除 `__pycache__` / `*.pyc` / `<skill根>/exports` / `<skill根>/icons`（运行时池）/
   > `assets/sources|svg_body|svg_output|svg_final`。🔴 **`assets/icons/` 必须打进包** ——
   > 少了它 skill 会在 Step 1c/2c-2 用图标时直接失败。**判定口径：排除项一律写成绝对子路径，禁用裸目录名匹配。**
   >
   > 🔴🔴 **打包一律用现成脚本，别手写 `os.walk`**（2026-10-10 事故：手写时把排除项写成裸目录名
   > `"icons"` ⇒ `assets/icons/` 被误杀，包只有 176 文件 / 正常 8,557）：
   > ```bash
   > python scripts/cleaners/pack_skill.py            # 打包（自带完整性核验）
   > python scripts/cleaners/pack_skill.py --check    # 只核验既有 zip
   > python scripts/cleaners/pack_skill.py --list     # 只列清单不写文件
   > ```
   > 核验项 = **文件数 + 图标数下限（≥8000）+ 关键路径存在**。🔑 **别用"查几个 md 关键词"当完整性核验**。
