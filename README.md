<p align="center">
  <img src="frontend/public/skillgo-logo.png" width="104" alt="SkillGo Logo" />
</p>

<h1 align="center">SkillGo</h1>

<p align="center">
  把 Skill 变成团队可以使用、管理和集成的任务能力。<br />
  <sub>A self-hosted, multi-user platform for running agent skills in isolated sandboxes and delivering verifiable artifacts.</sub>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-3f36c9" /></a>
  <img alt="Python 3.12" src="https://img.shields.io/badge/Python-3.12-3776ab" />
  <img alt="Node.js 24" src="https://img.shields.io/badge/Node.js-24-339933" />
  <a href="https://github.com/Max-cu/SkillGo/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/Max-cu/SkillGo/actions/workflows/ci.yml/badge.svg" /></a>
  <a href="https://github.com/Max-cu/SkillGo/releases"><img alt="Release" src="https://img.shields.io/github/v/release/Max-cu/SkillGo?color=6957ee" /></a>
</p>

<p align="center">
  <a href="#能用-skillgo-做什么">产品能力</a> ·
  <a href="#从-skill-到交付物">使用流程</a> ·
  <a href="#agent-如何执行任务">Agent 执行</a> ·
  <a href="#快速开始">快速开始</a> ·
  <a href="#通过-api-接入业务">API 接入</a> ·
  <a href="#开发与文档">开发与文档</a>
</p>

SkillGo 是一个可私有化部署的多用户 Skill 平台。你可以上传现有的 `SKILL.md` 技能包，管理版本与审核，在网页中通过自然语言和附件发起任务，再下载生成的文档、表格、演示稿、图片或其他文件。已发布的 Skill 也可以绑定独立 API Endpoint，接入已有业务系统。

普通消息直接进入模型对话；需要脚本和文件处理的任务，由 Agent 在独立的 Linux 沙箱中执行。平台负责环境准备、任务调度、工具约束、状态记录、产物验证和存储，模型负责理解目标、组织步骤与使用 Skill。

本文描述当前 `main` 的实现。项目仍在持续演进；部署已发布版本时，请结合对应 Tag 的文档与 [Release Notes](https://github.com/Max-cu/SkillGo/releases)。

<p align="center">
  <img src="docs/assets/skillgo-workbench.png" width="100%" alt="SkillGo 任务工作台：对话、执行进度与文件交付" />
</p>

## 能用 SkillGo 做什么

| 能力 | 当前支持 |
| --- | --- |
| Skill 管理 | ZIP 上传、元数据分析、版本管理、审核发布、社区展示、收藏和下载 |
| 对话工作台 | 流式回复、历史会话、附件、历史文件复用，以及在对话中发起 Skill 任务 |
| 文件任务 | 一个任务选择多个 Skill，查看执行计划、关键进度、技术日志和生成文件 |
| 模型接入 | 配置多个模型连接，分别选择对话、视觉与 OCR 能力及默认模型 |
| 任务执行 | Agent 工具调用、Python 与命令执行、固定入口 Skill、取消、重试和等待用户补充信息 |
| 运行环境 | 基础能力探测、独立环境准备服务、Python 依赖解析、环境复用与运行中升级 |
| 故障恢复 | 租约与心跳、失效 Worker 回收；可选的工作区与 Agent 状态持久化恢复 |
| 团队治理 | 成员、管理员、唯一超级管理员；资源归属校验、版本联网授权与审计记录 |
| 业务集成 | 固定版本 Endpoint、独立密钥、同步调用、异步任务、幂等提交和产物下载 |
| 存储管理 | 文件占用统计、保留期限、过期清理、备份与恢复脚本 |

文档处理、表格分析、演示稿生成等具体业务能力由上传的 Skill 和所配置模型决定；平台提供通用执行与交付机制。

<table>
  <tr>
    <td width="50%"><img src="docs/assets/skillgo-skills.png" alt="Skill 管理与版本状态" /><br /><sub>Skill 管理与发布</sub></td>
    <td width="50%"><img src="docs/assets/skillgo-storage.png" alt="文件占用与存储保留期限" /><br /><sub>存储与生命周期管理</sub></td>
  </tr>
</table>

## 从 Skill 到交付物

```text
上传 Skill → 准备运行环境 → 固定版本并审核发布
                                  ↓
                   自然语言 + 附件 + 选定的 Skill
                                  ↓
                   Agent 编排 → 独立沙箱执行
                                  ↓
                   验证真实文件 → 网页 / API 交付
```

1. **导入 Skill**：上传 ZIP，平台解析结构、名称、说明、执行画像与依赖信息。元数据智能分析不可用时会回退到本地解析。
2. **准备和发布版本**：需要环境准备的版本先完成构建与探测；管理员审核版本，并按需授权运行联网。版本内容固定，修改需要上传新版本。
3. **发起任务**：选择 Skill，输入目标并添加附件。普通对话与 Skill 任务可以出现在同一会话里；纯指令 Skill 也有独立对话调试入口。
4. **查看过程**：工作台展示计划、关键执行进度和产物；完整工具记录可展开查看。缺少关键输入时，任务可等待用户回答。
5. **获取结果**：下载验证通过的真实文件。失败、取消或受阻会保留明确状态，用户可以查看原因并重试。

### Skill 包格式

普通技能包以一个 `SKILL.md` 为入口，可包含脚本、参考资料和素材；需要平台扩展配置时，可添加 `skillgo.yaml`。

```text
my-skill/
├── SKILL.md
├── scripts/          # 可选：执行脚本
├── references/       # 可选：参考文档
├── assets/           # 可选：模板与素材
├── requirements.txt  # 可选：Python 依赖
└── skillgo.yaml      # 可选：SkillGo 扩展声明
```

- 支持常见仓库下载包的外层包装目录，安全文件树中必须且只能有一个 `SKILL.md`。
- 名称、说明缺失时可从目录或正文推断；兼容 UTF-8 BOM，并对标识做规范化处理。
- 默认 ZIP 上限 **50 MiB**、解压后上限 **250 MiB**、文件数上限 **500**。素材密集型 Skill 可由管理员调整文件数配置，大小限制仍然生效。
- 路径穿越、符号链接、特殊文件、重复路径和异常压缩等内容会被拒绝。
- 可声明 `capabilities`、`python_dependencies`；依赖分析不等于任意安装命令的执行授权。

示例见 [`examples/`](examples/)，格式与校验实现见 [`skill_package.py`](backend/app/skill_package.py)。

## Agent 如何执行任务

当前任务编排由**一个 Agent 在一个任务沙箱内协调所选 Skill**。计划可以声明步骤依赖、输入输出引用和成功条件；工具调用按返回顺序执行。不同任务由多个 Worker 领取，任务内没有通用并行 DAG 调度或子 Agent 系统。

| 执行路径 | 适用情况 |
| --- | --- |
| 普通对话 | 不运行 Skill 脚本，直接调用模型回复 |
| `instruction_only` | 纯指令 Skill，通过模型执行路径处理 |
| `sandbox_required` | 脚本、文件或工具任务，进入持久任务队列和独立沙箱 |
| 固定入口 | 声明 `spec.execution.mode: fixed` 的 Skill 使用固定入口与验证契约；单 Skill 任务可直接执行，无需模型选择脚本 |
| `platform_tools` | 所需平台能力未安全接入时明确阻断，不静默降级 |

### 计划、工具与上下文

Agent 可以读取 Skill 和文件、更新计划、执行命令或 Python、检查生成图片、请求环境能力、进行最终验证，以及在缺少关键信息时提问。平台检查工具参数、工作区路径和执行状态，并把可恢复错误交回 Agent 处理。

同轮允许执行具有安全顺序的多个工具调用；计划状态更新等已知操作可以合并，减少单独的记账轮次。较大的工具结果保存到工作区，模型获得摘要和读取路径；上下文按预算整理，并保留选定参考资料、计划和执行证据。

### 验证与交付

平台将业务验证和文件交付连起来：

1. Agent 优先复用 Skill 自带检查，或执行覆盖任务成功条件的只读验证程序。
2. `run_verifier` 检查退出状态、结构化检查结果，以及验证前后产物是否一致。
3. **验证成功后，平台自动登记证据**，并在其他步骤已完成时完成验证步骤，无需模型再调用一次 `record_validation`。
4. 交付前重新核对产物与已验证文件的哈希；Worker 收集文件时再检查路径、大小、结构和持久化结果。

计划检查会在单次快照内复用重叠引用的文件读取结果，下一次检查仍读取当前文件。优化减少了重复调用和 I/O，但不把“命令退出成功”直接当成“用户目标已完成”。结构与哈希检查也不能保证文档内容或模型判断在业务上完全正确。

### 中断与恢复

Worker 通过数据库租约领取任务并持续心跳，每次尝试都有独立执行身份。正常命令超时会终止命令进程组并保留沙箱供后续处理；Docker exec 通信超时等故障会触发相应的失败或恢复路径。

启用 `SKILLGO_DURABLE_CHECKPOINTS_ENABLED=true` 后，平台会在轮次边界保存工作文件、计划、消息、验证状态和执行位置。已完成工具轮次的快照可被新 Worker 接管；工具执行中断、结果不确定时明确报错，避免自动重放可能已有副作用的操作。

恢复不包含进程内存、后台进程或 `/tmp`。当前仍是每轮全量可变工作区快照，采用流式归档并排除平台配置的不可变 Skill 文件；大工作区会增加冻结和 I/O 成本。等待用户回答后的继续执行采用重新发起尝试的语义，并非恢复原进程。详见 [持久化任务快照](docs/durable-task-checkpoints.md)。

## 环境准备与依赖

SkillGo 将环境准备交给独立的 `environment-worker`，任务容器内常见的临时 pip/npm 安装命令会被拦截。

- **能力目录**：覆盖 PDF、Office、图像、表格、中文字体、Markdown、二维码、条形码、XML 等能力。任务运行前通过探针确认实际可用性。
- **Python 依赖**：支持声明、`requirements.txt` 和部分导入/安装示例分析。解析器从公共 PyPI 解析兼容 wheel，保留底座版本约束，校验哈希后离线安装并验证候选环境。
- **明确失败**：不支持任意 URL、本地路径、源码包构建或直接执行 Skill 的安装脚本；无法解析或与底座冲突的依赖会报错，不静默替换已有环境。
- **环境复用**：环境规格与准备结果绑定到版本，兼容任务可复用准备镜像；任务按不可变镜像 ID 执行。
- **运行中升级**：Agent 可请求补充平台能力或 Python 依赖，每个任务最多两次实际升级尝试。成功后在新环境恢复工作文件，继续任务并重新验证结果；不会恢复进程或 `/tmp`。

环境准备**默认关闭**，需要配置基础镜像并启动 `environments` profile。能力目录和 Python 依赖解析的实现分别见 [`environment_capabilities.py`](backend/app/environment_capabilities.py)、[`python_dependencies.py`](backend/app/python_dependencies.py) 与 [`environment_builder.py`](backend/app/environment_builder.py)。当前环境缓存面向单 Docker 主机，不提供跨主机镜像分发。

## 执行隔离与权限

```text
浏览器 / 业务系统
       │
       ▼
Web + FastAPI ────────── PostgreSQL / 文件存储
       │                       ▲
       │ 任务队列              │ 状态、证据与产物
       ▼                       │
可信 Worker ── 模型编排 ── 任务容器（gVisor runsc）
       │                       └─ 本次尝试独立 /workspace Volume
       └─ 环境准备服务提供已验证镜像
```

隔离粒度是**一次任务尝试一套容器和工作卷**。多个 Skill 在同一任务内共享该任务工作区，不同任务不会复用同一工作卷。

| 边界 | 实现 |
| --- | --- |
| 任务运行时 | Linux Docker + gVisor `runsc`；运行环境不可用时拒绝执行，不静默回退普通容器 |
| 身份与文件 | 非 root `10001:10001`、只读根文件系统、独立 `/workspace`、临时 `/tmp` |
| Linux 权限 | `cap_drop=ALL`、`no-new-privileges`；任务容器不挂载 Docker Socket 或宿主目录 |
| 资源 | 内存、CPU、进程数、命令时间、模型轮次和工具调用次数可配置 |
| 访问归属 | API 与存储路径检查用户、会话、任务或 Endpoint 的归属 |
| 密钥 | 任务容器不注入平台数据库、JWT、模型或 Endpoint 密钥 |
| 产物 | 只交付 `/workspace/output` 内声明的常规文件，默认单文件上限 50 MiB |

运行联网默认关闭，由管理员对具体已审核版本授权。**多 Skill 任务中，只要一个选中版本获准联网，整个任务沙箱就具有网络访问能力。** 任务保存授权来源；权限调整作用于新任务。当前联网沙箱不限制目标域名，依赖构建服务的下载策略与任务运行联网是两套机制。

Worker 和环境准备服务持有 Docker 管理权限，属于可信控制面。gVisor 不替代宿主机维护、访问控制和密钥管理。公网部署应配置 HTTPS；更多边界见 [安全政策](SECURITY.md)。

## 快速开始

### 1. 基础模式：先体验界面与对话

需要 Git、Docker Engine / Docker Desktop 和 Docker Compose v2。在终端执行：

```bash
git clone https://github.com/Max-cu/SkillGo.git
cd SkillGo
cp .env.example .env
```

编辑 `.env`，至少替换 `POSTGRES_PASSWORD`、`SKILLGO_JWT_SECRET`、`SKILLGO_BOOTSTRAP_EMAIL` 和 `SKILLGO_BOOTSTRAP_PASSWORD`，再启动：

```bash
docker compose up -d --build
```

访问 **http://127.0.0.1:8080**，使用配置的 Bootstrap 账号登录。这是实例唯一的超级管理员；模型可在登录后的平台设置中配置，也可提前填写 `.env` 中的模型地址、密钥和名称。

基础模式启动 Web、API 和 PostgreSQL，适合管理 Skill 和普通模型对话；**不会启动执行脚本任务的沙箱 Worker**。Windows PowerShell 可用 `Copy-Item .env.example .env` 替代 `cp`。

### 2. 完整模式：运行沙箱任务

需要 **Linux + Docker Engine + 已注册到 Docker 的 gVisor `runsc`**。gVisor 安装、Docker Socket GID 和服务器配置按 [部署指南](deploy/README.md) 完成。在项目根目录准备配置：

```bash
cp deploy/ecs.env.example deploy/ecs.env
# 编辑 deploy/ecs.env：填写 Docker GID、访问地址和端口
docker compose --env-file .env --env-file deploy/ecs.env --profile build-only build sandbox-runtime
bash deploy/preflight.sh
docker compose --env-file .env --env-file deploy/ecs.env --profile sandbox up -d --build --scale worker=1
SKILLGO_INSTALL_ROOT="$PWD" bash deploy/verify-ecs.sh
```

上例以 **1 个 Worker** 起步；仓库 Compose 配置为 5 个副本，可按宿主资源使用 `--scale worker=N` 调整。Worker 本身与每个任务沙箱都会占用资源，应根据任务文件大小和并发量规划容量。

`deploy/ecs.env.example` 默认对外监听 80 端口，需填写实际访问地址；它覆盖根目录 `.env` 的同名配置。部署示例给任务沙箱配置 2 GiB 内存、1 CPU、128 PIDs 和 900 秒单命令超时，任务总时限默认关闭；这些是可调整的预算，不是性能承诺。

### 3. 可选：启用环境准备与持久化恢复

先取得基础镜像 ID：

```bash
docker image inspect skillgo/sandbox-runtime:local --format '{{.Id}}'
```

将以下配置写入 `deploy/ecs.env`，把基础镜像占位值替换成上一步完整输出：

```dotenv
SKILLGO_ENVIRONMENT_PREPARATION_ENABLED=true
SKILLGO_ENVIRONMENT_BASE_IMAGE=sha256:<完整镜像ID>
SKILLGO_DURABLE_CHECKPOINTS_ENABLED=true
```

两个功能可以独立启用。启用环境准备时启动对应服务，并保持 API、Worker 与环境服务配置一致：

```bash
docker compose --env-file .env --env-file deploy/ecs.env --profile sandbox --profile environments up -d --build --scale worker=1 api worker environment-worker
# API 重建后同步重建 Web，刷新 Nginx 的后端地址
docker compose --env-file .env --env-file deploy/ecs.env up -d --no-deps --force-recreate web
```

已有业务的实例应在任务空闲时变更配置，先备份再更新。环境构建需要访问依赖源；任务沙箱是否联网仍按版本授权控制。

## 通过 API 接入业务

Endpoint 固定绑定拥有者和已审核、可执行的 Skill 版本。密钥只在创建或轮换时完整展示，服务端保存前缀与摘要。请求通过 `X-SkillGo-Key` 认证。

| 方式 | 接口 | 返回 |
| --- | --- | --- |
| 纯指令同步调用 | `POST /api/v1/invoke/{slug}` | 结构化结果与 `run_id` |
| 沙箱异步任务 | `POST /api/v1/workflow-endpoints/{slug}/jobs` | `202 Accepted`、任务 ID 与查询地址 |

异步接口当前接收一个必填输入文件和可选指令，支持 `Idempotency-Key`：

```bash
export SKILLGO_BASE_URL="https://skillgo.example.com"
export SKILLGO_API_KEY="<你的 Endpoint 密钥>"

curl "$SKILLGO_BASE_URL/api/v1/workflow-endpoints/your-slug/jobs" \
  -H "X-SkillGo-Key: $SKILLGO_API_KEY" \
  -H "Idempotency-Key: request-001" \
  -F "file=@./input.docx" \
  -F "instruction=检查日期、金额和前后矛盾，生成检查报告"
```

提交后查询任务状态，再下载产物；外部接口仍执行 Endpoint 与任务归属校验。同步调用的请求/响应契约、Endpoint 的创建与密钥轮换（平台 JWT）、错误码与 `waiting_user` 行为见 [任务 API](docs/workflow-api.md)，可运行示例见 [Python 客户端](examples/workflow_api_client.py)。开发环境 API 文档位于 `http://127.0.0.1:8000/api/docs`。

## 数据与运维

| 数据 | 默认策略 |
| --- | --- |
| 对话附件、任务输入、生成产物 | 保留 15 天，可配置；到期文件不再可下载 |
| 成功运行的详细事件 | 保留 7 天 |
| 失败或取消运行的详细事件 | 保留 30 天 |
| 对话文字、任务状态、结果摘要、审计 | 不随上述文件保留期限自动删除 |
| 数据库 | PostgreSQL 用于生产；SQLite 用于本地开发；Alembic 管理结构迁移 |

管理员可查看平台文件分类和服务器磁盘占用。更新前应备份数据库、托管文件及配置：

```bash
bash deploy/backup-skillgo.sh
```

安装、指定版本升级、自检、备份和恢复流程见 [部署指南](deploy/README.md)。生产环境应选择经过验证的提交或 Release；GitHub `main` 上的新功能可能尚未进入最近的 Release。

旧工具 `scripts/migrate_sqlite_to_postgres.py` 仅支持旧版表合并。源库含有未支持的非空表时会在复制和写入前停止，**不能用它完成当前完整数据库的跨引擎迁移**。

## 当前边界

- 多 Skill 是同一 Agent、同一任务工作区内的协调执行；步骤依赖检查不等于通用并行 DAG、条件分支或多 Agent 调度。
- 模型兼容性取决于服务端对 Chat Completions、流式输出和工具调用等协议的实现。对话、视觉使用 OpenAI-compatible 接口；OCR 还支持 MinerU `/file_parse`，需要单独配置相应能力。
- 图片可以由视觉模型理解，OCR 按需开启；平台不保证所有文件格式都能完整解析。
- 依赖准备当前侧重受约束的 Python wheel 与平台能力目录，不提供通用 npm、apt 或源码编译服务。
- 快照是工作文件和 Agent 状态的恢复，不是进程快照；等待环境构建仍占用当前任务 Worker。
- 当前模型耗时、工具执行、文件规模与检查点 I/O 都会影响完成速度；减少流程调用不代表所有任务都获得固定比例提速。

## 开发与文档

本地开发使用 Python 3.12、Node.js 24。以下为 PowerShell 示例：

```powershell
Copy-Item backend\.env.example backend\.env
# 编辑 backend/.env：替换密钥、Bootstrap 信息和模型配置
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements-dev.lock
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --env-file backend/.env --reload
```

另开终端启动前端：

```powershell
Set-Location frontend
npm.cmd ci
npm.cmd run dev
```

前端地址为 `http://127.0.0.1:5173`，通过 Vite 代理访问本机 API。开发服务器不会自动提供 Linux/gVisor 沙箱能力。

从项目根目录运行验证：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
npm.cmd --prefix frontend run build
```

GitHub Actions 检查后端测试、前端构建、部署脚本和沙箱 Compose 配置；真实 Docker/gVisor 自检由部署脚本执行。

| 目录 | 内容 |
| --- | --- |
| `backend/app` | FastAPI、模型网关、Agent、任务 Worker、环境准备与存储 |
| `backend/tests` | 后端回归测试 |
| `frontend/src` | React / TypeScript 工作台、Skill 社区与管理界面 |
| `sandbox-runtime` | Linux 任务基础镜像 |
| `examples` | 示例 Skill 与 API 客户端 |
| `deploy` | 安装、预检、自检、升级、备份与恢复 |

| 文档 | 用途 |
| --- | --- |
| [部署指南](deploy/README.md) | Linux、gVisor、配置、备份与运维 |
| [开发指南](docs/development.md) | 本地环境与开发流程 |
| [任务 API](docs/workflow-api.md) | 异步调用、状态查询与产物下载 |
| [产品与技术架构](docs/PRODUCT_ARCHITECTURE.md) | 产品边界与系统分层 |
| [Agent 内核](docs/agent-kernel.md) | 编排、工具、上下文与验证设计 |
| [持久化任务快照](docs/durable-task-checkpoints.md) | 恢复语义、边界与验收记录 |
| [文档索引](docs/README.md) | 其他设计资料与历史阶段记录 |

部分设计文档保留了阶段性状态与当时的验证记录，不能作为当前功能开关或部署状态的证明；具体行为以所用版本的代码、配置和测试为准。

## 参与项目

·欢迎提交问题、改进建议与 Pull Request。如果你认可SkillGo，不妨点个star⭐，这将鼓励我们。

·开发约定见 [CONTRIBUTING.md](CONTRIBUTING.md)，安全问题请按 [SECURITY.md](SECURITY.md) 私下报告。

·SkillGo 使用 [MIT License](LICENSE)。
