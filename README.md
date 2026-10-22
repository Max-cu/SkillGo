<p align="center">
  <img src="frontend/public/skillgo-logo.png" width="100" alt="SkillGo Logo" />
</p>

<h1 align="center">SkillGo</h1>

<p align="center">
  <strong>把好用的 Skill，变成每个人的能力。</strong><br />
  沙箱隔离 · 成果交付 · 外部调用
</p>

<p align="center">
  <sub>A self-hosted platform for running agent skills in isolated sandboxes and delivering artifacts through the web or API.</sub>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-6558d9" /></a>
  <a href="https://github.com/Max-cu/SkillGo/releases"><img alt="Release" src="https://img.shields.io/github/v/release/Max-cu/SkillGo?color=6558d9" /></a>
  <a href="https://github.com/Max-cu/SkillGo/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/Max-cu/SkillGo/actions/workflows/ci.yml/badge.svg" /></a>
  <img alt="Python 3.12" src="https://img.shields.io/badge/Python-3.12-3776ab" />
  <img alt="Node.js 24" src="https://img.shields.io/badge/Node.js-24-339933" />
</p>

<p align="center">
  <a href="#产品演示">产品演示</a> ·
  <a href="#核心能力">核心能力</a> ·
  <a href="#使用流程">使用流程</a> ·
  <a href="#快速开始">快速开始</a> ·
  <a href="#api-接入">API 接入</a> ·
  <a href="#开发与文档">开发与文档</a>
</p>

SkillGo 是一个可私有化部署的多用户 Skill 执行平台，专注于**接收一次任务，按选定 Skill 完成工作，交付真实成果**。上传现有的 `SKILL.md` 技能包，准备环境并审核发布，再通过自然语言和附件发起任务。Agent 在独立沙箱中执行，处理可恢复错误，平台按配置的验收策略接收并保存生成文件；同一项 Skill 能力也可以发布为 API，接入已有业务系统。

## 产品演示

**第一次了解 SkillGo？不妨先观看这段 1 分 41 秒的产品演示。**

以 PPT 制作为例，直观了解 SkillGo 如何将 Skill 转化为可执行、可交付、可供业务调用的能力。视频配有中文配音与中英双语字幕。

https://github.com/user-attachments/assets/eb3e6cdf-cda6-434b-a0a9-4cc0d37262f4

<p align="center">
  <a href="https://github.com/user-attachments/assets/eb3e6cdf-cda6-434b-a0a9-4cc0d37262f4">下载演示视频</a> · 1080p · 60 fps
</p>

> **说明：** 视频展示 SkillGo 的产品流程。具体文档、表格、演示稿及图像处理能力取决于所上传的 Skill 和配置的模型。
## 核心能力

<p align="center">
  <img
    src="docs/assets/skillgo-core-capabilities.svg"
    width="100%"
    alt="SkillGo 核心能力：沙箱隔离、成果交付、外部调用"
  />
</p> 

### 平台功能

<p>
  <img src="docs/assets/feature-icons/layers.svg" width="18" height="18" alt="" />&nbsp;&nbsp;
  <strong>Skill 管理</strong> — 技能导入、版本管理、审核发布、社区共享
</p>

<p>
  <img src="docs/assets/feature-icons/messages-square.svg" width="18" height="18" alt="" />&nbsp;&nbsp;
  <strong>对话工作台</strong> — 自然语言交互、附件处理、历史文件复用
</p>

<p>
  <img src="docs/assets/feature-icons/workflow.svg" width="18" height="18" alt="" />&nbsp;&nbsp;
  <strong>Agent 执行</strong> — Skill 流程直达、多 Skill 协调、Python / 命令执行、进度追踪与成果交付
</p>

<p>
  <img src="docs/assets/feature-icons/cpu.svg" width="18" height="18" alt="" />&nbsp;&nbsp;
  <strong>模型与环境</strong> — 多模型连接、视觉与 OCR、环境准备及依赖解析
</p>

<p>
  <img src="docs/assets/feature-icons/users-round.svg" width="18" height="18" alt="" />&nbsp;&nbsp;
  <strong>团队与运维</strong> — 权限管理、操作审计、文件保留、备份与恢复
</p>

### 界面预览



<details>
<summary>Skill 管理与存储界面</summary>

<table>
  <tr>
    <td width="50%">
      <img src="docs/assets/skillgo-skills.png" alt="Skill 管理界面" /><br />
      <sub>Skill 管理与发布</sub>
    </td>
    <td width="50%">
      <img src="docs/assets/skillgo-storage.png" alt="存储管理界面" /><br />
      <sub>存储与生命周期管理</sub>
    </td>
  </tr>
</table>

</details>

## 使用流程

<p align="center">
  <img src="docs/assets/skillgo-workflow.png" width="100%" alt="SkillGo 使用流程：准备并发布 Skill，提交任务，独立沙箱执行，验证并交付成果" />
</p>

1. **准备 Skill**：导入 ZIP，按需准备运行环境并完成审核发布。
2. **发起任务**：选择一个或多个 Skill，输入目标并添加附件。
3. **跟踪执行**：查看进度与工具日志；任务无人值守运行，信息不足时采用合理假设，无法继续时明确报告原因。
4. **获取成果**：下载已保存的文件，查看所用验收策略；失败或取消时可查看原因并重试。

同一任务由一个 Agent 协调所选 Skill，共享该任务工作区；不同任务使用独立工作卷。详细格式和示例参见 [Skill 包说明](docs/skill-packages.md) 与 [`examples/`](examples/)。

### 执行策略由管理员按需开启

| 设置 | 默认行为 | 适用方式 |
| --- | --- | --- |
| 沿用 Skill 既定流程 | 关闭 | 在 Skill 版本管理中开启；仅单 Skill 任务免除强制规划，模型仍可在必要时规划 |
| 运行联网 | 关闭 | 按版本授权；与既定流程开关相互独立 |
| 文件接收模式 | 默认开启 | 保留 Skill 自带校验，平台安全接收并保存真实文件；额外的平台完整验证可主动选择 |
| 最终操作自动交付 | 默认可用，无需单独开启 | 框架引导模型提前声明最后一组操作及产物；满足条件后直接交付，适用于所有账号、Skill 和文件格式 |
| 详细上下文观测 | 关闭 | `SKILLGO_CONTEXT_OBSERVABILITY=true`，排查时记录统计，不增加摘要模型调用 |

这些是独立设置。既定流程开关不会自动开启联网、跳过 Skill 校验或开启自动交付；联网与既定流程设置在创建任务时复制，修改版本按钮不改变已有任务。文件接收成功只表示真实文件已安全保存，不等于业务内容或版式已通过审核。

通用自动交付需要模型在最后一组工具调用中声明确切文件路径，并包含所有剩余的 Skill 校验和清理。若文件名需运行后才能得知、还需要模型看版，或文件没有在本组中生成/更新，仍使用普通结束路径。无需修改 Skill 包，详见 [通用最终操作交付](docs/final-operation-delivery.md)。

新部署无需配置上述两项。需要额外平台验证时可显式设置 `SKILLGO_ARTIFACT_DELIVERY_ONLY=false`；已有实例的显式配置继续有效，升级不会自动覆盖。

## 快速开始

### 体验界面与对话

准备 **Git、Docker Engine / Docker Desktop 和 Docker Compose v2**，克隆项目并创建环境文件：

```bash
git clone https://github.com/Max-cu/SkillGo.git
cd SkillGo
git checkout v1.0.0
cp .env.example .env
```

在 `.env` 中设置 `POSTGRES_PASSWORD`、`SKILLGO_JWT_SECRET`、`SKILLGO_BOOTSTRAP_EMAIL`、`SKILLGO_BOOTSTRAP_PASSWORD`，然后启动：

```bash
docker compose up -d --build
```

打开 **http://127.0.0.1:8080**，使用 Bootstrap 管理员账号登录，并在平台设置中配置模型。Windows PowerShell 请用 `Copy-Item .env.example .env` 代替 `cp`。

> **基础模式**仅启动 Web、API 和数据库，适合体验界面、Skill 管理与普通对话。要执行脚本、处理附件或生成文件，请继续启用沙箱模式。

### 启用沙箱执行（Linux）

需要 **Docker Engine + 已注册到 Docker 的 gVisor `runsc`**。先按 [部署指南](deploy/README.md) 完成安装与 Docker Socket GID 配置，再执行：

```bash
cp deploy/ecs.env.example deploy/ecs.env
# 编辑 deploy/ecs.env：配置 Docker GID、访问地址与端口

# 构建沙箱镜像并完成预检
docker compose --env-file .env --env-file deploy/ecs.env \
  --profile build-only build sandbox-runtime
bash deploy/preflight.sh

# 启动沙箱模式（示例：2 个 Worker）
docker compose --env-file .env --env-file deploy/ecs.env \
  --profile sandbox up -d --build --scale worker=2

# 验证服务、Worker 和 gVisor 隔离
SKILLGO_INSTALL_ROOT="$PWD" bash deploy/verify-ecs.sh
```

Worker 数量可按宿主资源调整：`--scale worker=N`。`deploy/ecs.env.example` 默认监听 **80 端口**，并覆盖根目录 `.env` 的同名配置；环境准备默认关闭，启用方法见 [执行环境与隔离](docs/execution-and-isolation.md)。

## API 接入

**同一项已发布的 Skill 能力，也可以通过独立 Endpoint 接入业务系统。** Endpoint 必须绑定已审核、可运行的 Skill 版本；密钥仅在创建或轮换时完整展示。

- **同步指令调用**：`POST /api/v1/invoke/{slug}` — 适用于纯指令处理，返回结构化结果与 `run_id`。
- **异步沙箱任务**：`POST /api/v1/workflow-endpoints/{slug}/jobs` — 适用于文件与工具任务，返回 `202 Accepted`、任务 ID 和查询地址。

异步接口接收可选输入文件和文本指令，至少提供一项；纯文本调用省略 `file`，不会生成占位附件。

### 示例：提交文件处理任务

```bash
export SKILLGO_BASE_URL="https://skillgo.example.com"
export SKILLGO_API_KEY="<你的 Endpoint 密钥>"

curl "$SKILLGO_BASE_URL/api/v1/workflow-endpoints/your-slug/jobs" \
  -H "X-SkillGo-Key: $SKILLGO_API_KEY" \
  -H "Idempotency-Key: request-001" \
  -F "file=@./input.docx" \
  -F "instruction=根据资料制作项目汇报 PPT，突出关键进展与下一步计划"
```

请求成功后，查询任务状态并下载产物。`Idempotency-Key` 可避免同一 Endpoint 下重复提交相同请求键时重复创建任务。外部任务无人值守执行，不会中途追问；信息不足时，Agent 会采用合理假设并在结果中说明。

Endpoint 绑定具体 Skill 版本。每个新 API 任务都会继承该版本的联网授权和“沿用 Skill 既定流程”设置；调用方无需额外传入这两个开关。

[完整 API 文档](docs/workflow-api.md) · [Python 客户端示例](examples/workflow_api_client.py) · 开发环境交互文档：`http://127.0.0.1:8000/api/docs`

## 架构与边界

<p align="center">
  <img src="docs/assets/skillgo-flow.png" width="100%" alt="SkillGo 运行架构与执行闭环：控制平面、任务队列、Worker、gVisor 沙箱与成果交付" />
</p>

SkillGo 由 **Web / FastAPI 控制面、PostgreSQL 与文件存储、可信 Worker、独立 gVisor 任务沙箱**构成。控制面负责授权、任务管理与交付，Worker 协调模型和沙箱中的执行过程。

**关键边界：**

- **执行隔离**：每次任务尝试使用独立容器与工作卷；沙箱以非 root 身份运行，使用只读根文件系统和可配置资源预算，不注入平台密钥或挂载 Docker Socket。
- **网络授权**：运行联网默认关闭。多 Skill 任务中，只要一个所选版本获得联网授权，整个任务沙箱就可联网；当前不限制目标域名。
- **任务编排**：一个 Agent 在任务沙箱内协调所选 Skill；多 Skill 协调不等于通用并行 DAG 或多 Agent 调度。
- **完成判定**：普通任务由 Agent 声明产物；自动交付仅支持明确审核的版本与命令序列，不以“目录里存在一个 PPT”作为完成依据。
- **速度与质量**：平台减少重复规划和确定流程中的往返；实际耗时及内容质量仍取决于模型、Skill、输入和重试，不承诺固定耗时或全面快于其他框架。
- **信任边界**：Worker 与环境准备服务属于可信控制面；gVisor 不能代替宿主机维护、权限治理与 HTTPS。

更多细节见 [执行环境与隔离](docs/execution-and-isolation.md)；漏洞报告方式见 [SECURITY.md](SECURITY.md)。

## 开发与文档

本地开发使用 **Python 3.12 + Node.js 24**；后端为 FastAPI，前端为 React / TypeScript。

**部署与接入**

[部署指南](deploy/README.md) · [开发指南](docs/development.md) · [Skill 包说明](docs/skill-packages.md) · [任务 API](docs/workflow-api.md)

**架构与进阶**

[执行环境与隔离](docs/execution-and-isolation.md) · [产品与技术架构](docs/PRODUCT_ARCHITECTURE.md) · [Agent 内核](docs/agent-kernel.md) · [文档索引](docs/README.md)

[既定流程开关](docs/skill-flow-policy.md) · [通用最终操作交付](docs/final-operation-delivery.md) · [工作流兼容适配](docs/workflow-auto-delivery.md) · [上下文观测](docs/context-observability.md)

本文描述当前 `main`。生产部署请选择经过验证的提交或 [Release](https://github.com/Max-cu/SkillGo/releases)，并参考对应版本文档；历史设计记录不代表当前功能开关或部署状态。

## v1.0.0：让 Skill 顺畅执行到交付

- **沿用既定流程**：管理员可按 Skill 版本开启，单 Skill 任务不再强制重列执行计划；完整 Skill、前置条件和自带校验仍交给模型遵循。
- **减少执行往返**：支持批量读取、批量命令和工作流连续执行，可恢复错误返回模型处理；正常完成后安全收集真实产物。
- **默认直接接收**：默认使用文件接收模式，复用 Skill 自带校验；需要额外平台验证时仍可主动开启。
- **通用自动交付**：默认对全部账号、Skill 和文件格式可用；模型在最后操作前声明产物，操作及自带校验成功、文件确为本组新建或更新且收集一致后，可省去独立结束轮次。
- **网页与 API 一致**：新任务继承所绑定版本的联网和既定流程设置，不要求外部调用方重复配置。

[查看 v1.0.0 发布说明](RELEASE_NOTES.md) · [完整变更记录](CHANGELOG.md)

## 参与项目

欢迎提交问题、改进建议与 Pull Request。如果你也认可 SkillGo ，欢迎点一个 Star⭐，这对我们很重要 ^_^

开发约定见 [CONTRIBUTING.md](CONTRIBUTING.md)，安全问题请按 [SECURITY.md](SECURITY.md) 私下报告。SkillGo 使用 [MIT License](LICENSE)。
