# 能力驱动环境：第一阶段与后续实施边界

## 定位

普通 Skill 以 SKILL.md、执行规则和静态资源为核心；capabilities 可选。保留现有脚本型和固定入口 Skill 的兼容性。能力声明不等于安装授权或联网授权。

依赖准备环境不接触用户任务数据；任务沙箱不自行获取安装依赖；业务联网权限独立授权。运行时安装的命令拦截与包管理器离线默认值是工具使用约束，不是任意 Python 代码的安全边界。授权联网的任意代码仍可能自行下载并运行内容，后续必须依靠真正的出口代理和环境隔离控制，而不能宣传仅靠提示词解决。

## 本次实现

1. 平台维护版本化能力目录：PDF 读取/渲染/写入/加注、Office 文档/表格/演示/转换、基础图像、表格数据、媒体转换、中文字体。
2. 支持 SKILL.md YAML frontmatter 的 capabilities 数组，以及扩展清单 spec.capabilities；声明格式在上传时检查。未知能力不执行任意安装操作，在任务预检时明确阻断。
3. PDF、docx、xlsx、pptx、中文等文本线索只产生建议，不能作为缺失能力的硬阻断依据，也不开放网络。
4. Worker 在 Agent 运行前执行平台内置的探针。Python 使用 -I，忽略用户目录、PYTHONPATH 和当前目录导入；探针不会接受 Skill 提交的可执行代码。记录实际包版本、能力、字体路径、运行镜像身份与清单摘要。
5. Python 探针包含小型功能检查；CLI 当前检查版本命令能否运行，字体检查 fontconfig 匹配。清单明确记录检查类型，不把路径或版本检查当作完整业务验证。
6. 环境清单写入任务 memory，注入 Agent 初始上下文；/workspace/work/environment.json 只是方便阅读的副本，不作为平台权威来源。inventory_digest 是探测清单摘要，不是未来完整环境构建的锁文件摘要。
7. 必需能力缺失时在首轮模型调用前报告 SANDBOX_DEPENDENCY_MISSING。未声明的旧 Skill 仍可运行，利用现有能力寻找替代方案。
8. 底座增加 PyMuPDF、Pillow、pandas，以及 LibreOffice Writer/Calc/Impress、Poppler、qpdf、ffmpeg、Noto CJK 字体和 fontconfig。
9. record_validation 通过且全部业务步骤已完成/跳过时，自动完成最终验证步骤；同步任务 memory、Agent checkpoint 与计划文件。不自动完成任何业务步骤，后续写入仍使验证失效。
10. Agent command 拦截常见包管理器安装命令；pip 禁用索引、npm 默认离线。此变更意味着依赖运行时 pip/npm 安装的旧 Skill 需要先准备平台环境。

示例：

```yaml
---
name: bilingual-pdf
description: PDF 原位双语加注
capabilities:
  - pdf.read
  - pdf.render
  - pdf.annotate
  - fonts.cjk
---
```

## 尚未实现，不能对外承诺

- 独立 Builder、网络出口代理与仓库凭据隔离。
- 发布阶段异步构建、完整的间接依赖锁定/哈希验证、不可变环境缓存。
- request_capability 工具与任务中途升级恢复。本阶段明确返回 environment_upgrade_supported=false，不向模型暴露一个不能兑现的工具。
- 多环境切换、暂停时进程终止和工作区独占交接。
- 任意包安装的全面安全阻断；目录推断只是兼容建议，探针通过不代表业务结果正确。

## 下一阶段协议

1. 能力目录解析成平台构建规格，仅能力 ID、目录版本、基础镜像摘要、目标架构及平台锁文件传给 Builder。用户、附件、Agent reason、业务文件路径不传给 Builder。
2. 下载器只接触受控仓库，安装/探针在无用户数据的断网环境运行。构建代码不能访问 Docker Socket、平台密钥和内部业务服务；可信控制器封装结果。
3. 环境状态：queued -> building -> probing -> ready/failed/revoked。Skill 版本保留独立的发布状态，构建失败不删除上传内容、不替换旧发布版本。
4. 首版升级只支持同一基础运行时、目录内预构建的兼容扩展。基础镜像/现有锁定包被替换视为冲突，不悄悄升级。
5. 升级事务：结束当前工具 -> 持久化会话和计划 -> 等待环境 -> 停止并隔离旧执行者 -> 新执行者独占挂载工作区 -> 探针通过 -> 注入新环境快照 -> 从下一次模型决策继续。不重放已经完成的工具。
6. 需要 lease/attempt fencing、取消传播、构建去重、构建超时/额度、工作区保留策略和回收器对 waiting_environment 状态的支持。升级失败保留原任务诊断，不能误报成功或无限重试。
7. 环境摘要按基础镜像、运行时/架构、完整锁文件、构建策略版本计算。任务记录 env-A -> env-B，不修改 Skill 默认绑定。旧环境支持禁用、审计和有界回收。

## 部署与验证

本次只准备候选镜像和代码，未切换线上 API/Worker 或默认沙箱镜像。需将新增模块随 API/Worker 一起发布。探针对旧镜像也可运行，并诚实返回缺失能力；若要提供完整底座能力，应先验证候选镜像，再部署 Worker。

基础镜像 Python 直接依赖已固定版本；apt 与间接依赖仍沿用现有构建方式，不宣称已经实现完整可复现锁定。候选镜像构建需联网，但构建上下文只包含平台 Dockerfile 与平台探针，无用户数据。

### 本轮验证记录

- 最终后端全量回归：271 项通过。
- 在服务器真实 runsc / network=none / 非 root / 只读根文件系统中运行平台探针，能区分旧底座已有和缺失的能力。
- 完整 Dockerfile 构建受 Docker Hub 访问超时阻碍；改用服务器已有底座的增量候选验证，但 Debian 包下载较慢（309 MB 总量，十多分钟仅约 79 MB），未完成安装。
- 已将下载缓存保存在服务器 .deploy/environment-base-candidate/download-cache，并停止本次专用构建容器，未修改线上 API、Worker、Web 或默认运行镜像。
- 新底座的 LibreOffice 转换、中文字体渲染、ffmpeg、PyMuPDF 组合功能验收尚未完成；不得将配置中的软件清单视为已上线能力。
- 发布前应重新构建标准 Dockerfile，并运行 .deploy/smoke-capability-base.py 或等效的断网功能验收。该临时脚本只用合成数据，不用用户文件。

### 2026-09-11 构建恢复与验收

用户的增量构建已完成 apt/pip/npm 安装，但因原有 sandbox 用户组重复创建失败（exit 9）。已确认并保留该构建容器 3f8460a7f701 的依赖成果，恢复为候选镜像；只补执行身份、目录所有者和 sleep 启动命令，没有重新下载依赖。标准 Dockerfile 已改为用户/组存在时复用，并校验 UID/GID 必须为 10001。

- 候选标签：skillgo/sandbox-runtime:capabilities-candidate
- 镜像 ID：sha256:f4a671517b471404e1c24f5cdd7d0808324095a8a56a901b4f9d10b4e0200f8d
- 身份：10001:10001；启动命令：sleep infinity。
- 在 gVisor、断网、只读根文件系统、无用户文件的临时容器内验证。
- 平台探针全部支持项可用：PyMuPDF 1.26.4、Pillow 12.3.0、pandas 2.3.2、现有 Office/PDF 库、Poppler、LibreOffice、ffmpeg、Noto Sans/Serif CJK。
- 六项合成功能测试全部通过：Office 创建、Office 转 PDF 和渲染、qpdf、中文文字与图像渲染、表格处理、媒体转换。
- 完整脚本保存在 sandbox-runtime/smoke-capabilities.py；它只使用合成数据。
- 本次验证的是恢复得到的候选镜像，不是从空缓存重新构建标准 Dockerfile 的验证。线上默认镜像和 API/Worker 未切换，后端能力代码仍待一起发布。
