# 执行环境、隔离与运维

[返回项目首页](../README.md) · [部署指南](../deploy/README.md)

本文整理 Agent 执行、环境准备、权限边界与运维策略，具体行为以所用版本代码和配置为准。

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

Agent 可以读取 Skill 和文件、更新计划、执行命令或 Python、检查生成图片、请求环境能力，以及按验收策略完成任务。当前任务无人值守运行，缺少信息时采用合理假设，无法继续时明确阻断。平台检查工具参数、工作区路径和执行状态，并把可恢复错误交回 Agent 处理。

管理员可为沙箱 Skill 版本开启“沿用 Skill 既定流程”。单 Skill 任务按完整 Skill 执行，不强制模型另列计划；需要协调或恢复时模型仍可规划。多个 Skill 的任务保留普通规划要求。网页、API 和重试创建任务时继承对应版本设置，已有任务不受版本开关后续修改影响。详见 [既定流程开关](skill-flow-policy.md)。

同轮允许执行具有安全顺序的多个工具调用；计划状态更新等已知操作可以合并，减少单独的记账轮次。较大的工具结果保存到工作区，模型获得摘要和读取路径；上下文按预算整理，并保留选定参考资料、计划和执行证据。

### 验证与交付

默认采用完整验证策略：

1. Agent 优先复用 Skill 自带检查，或执行覆盖任务成功条件的只读验证程序。
2. `run_verifier` 检查退出状态、结构化检查结果，以及验证前后产物是否一致。
3. **验证成功后，平台自动登记证据**，并在其他步骤已完成时完成验证步骤，无需模型再调用一次 `record_validation`。
4. 交付前重新核对产物与已验证文件的哈希；Worker 收集文件时再检查路径、大小、结构和持久化结果。

计划检查会在单次快照内复用重叠引用的文件读取结果，下一次检查仍读取当前文件。优化减少了重复调用和 I/O，但不把“命令退出成功”直接当成“用户目标已完成”。结构与哈希检查也不能保证文档内容或模型判断在业务上完全正确。

部署可通过 `SKILLGO_ARTIFACT_DELIVERY_ONLY=true` 为全部 Skill 启用文件接收模式，或以
`SKILLGO_DELIVERY_ONLY_SKILL_IDS` 指定 Skill ID 白名单。该模式保留 Skill 自带检查，
省去额外的平台验证程序与验证报告要求；仍检查文件路径、可读取性、大小、非空及实际保存结果，
不判定格式结构、业务内容或版式质量。`verified` 在该模式下仅表示文件成功接收。

另可为审核过的版本设置 `SKILLGO_WORKFLOW_AUTO_DELIVERY_VERSION_IDS`。目前仅支持匹配的
已适配的演示文稿最终构建、整理、导出流程：必须证明本次导出产生新文件、余下校验和清理全部成功，
且文件字节未变化，才能省去最后一轮模型结束调用；条件不满足时回到普通 Agent 路径。
详见 [自动交付条件](workflow-auto-delivery.md)。

文件接收模式下，任意账号和 Skill 的最终操作也可使用通用 `delivery` 声明，
将生成/导出与所有剩余检查、清理安排在同一工具调用中。框架比较操作前后的精确文件指纹，
只在全部成功、文件非空且由本组生成或更新，并通过原有完成门槛时直接交付。
格式与版本不设白名单；不满足条件时回到普通结束路径。详见 [通用最终操作交付](final-operation-delivery.md)。

### 中断与恢复

Worker 通过数据库租约领取任务并持续心跳，每次尝试都有独立执行身份。正常命令超时会终止命令进程组并保留沙箱供后续处理；Docker exec 通信超时等故障会触发相应的失败或恢复路径。

启用 `SKILLGO_DURABLE_CHECKPOINTS_ENABLED=true` 后，平台会在轮次边界保存工作文件、计划、消息、验证状态和执行位置。已完成工具轮次的快照可被新 Worker 接管；工具执行中断、结果不确定时明确报错，避免自动重放可能已有副作用的操作。

恢复不包含进程内存、后台进程或 `/tmp`。当前仍是每轮全量可变工作区快照，采用流式归档并排除平台配置的不可变 Skill 文件；大工作区会增加冻结和 I/O 成本。等待用户回答后的继续执行采用重新发起尝试的语义，并非恢复原进程。详见 [持久化任务快照](durable-task-checkpoints.md)。


## 环境准备与依赖

SkillGo 将环境准备交给独立的 `environment-worker`，任务容器内常见的临时 pip/npm 安装命令会被拦截。

- **能力目录**：覆盖 PDF、Office、图像、表格、中文字体、Markdown、二维码、条形码、XML 等能力。任务运行前通过探针确认实际可用性。
- **Python 依赖**：支持声明、`requirements.txt` 和部分导入/安装示例分析。解析器从公共 PyPI 解析兼容 wheel，保留底座版本约束，校验哈希后离线安装并验证候选环境。
- **明确失败**：不支持任意 URL、本地路径、源码包构建或直接执行 Skill 的安装脚本；无法解析或与底座冲突的依赖会报错，不静默替换已有环境。
- **环境复用**：环境规格与准备结果绑定到版本，兼容任务可复用准备镜像；任务按不可变镜像 ID 执行。
- **运行中升级**：Agent 可请求补充平台能力或 Python 依赖，每个任务最多两次实际升级尝试。成功后在新环境恢复工作文件，继续任务并重新验证结果；不会恢复进程或 `/tmp`。

环境准备**默认关闭**，需要配置基础镜像并启动 `environments` profile。能力目录和 Python 依赖解析的实现分别见 [`environment_capabilities.py`](../backend/app/environment_capabilities.py)、[`python_dependencies.py`](../backend/app/python_dependencies.py) 与 [`environment_builder.py`](../backend/app/environment_builder.py)。当前环境缓存面向单 Docker 主机，不提供跨主机镜像分发。


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

Worker 和环境准备服务持有 Docker 管理权限，属于可信控制面。gVisor 不替代宿主机维护、访问控制和密钥管理。公网部署应配置 HTTPS；更多边界见 [安全政策](../SECURITY.md)。

## 启用环境准备与持久化恢复

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
docker compose --env-file .env --env-file deploy/ecs.env --profile sandbox --profile environments up -d --build --scale worker=12 api worker environment-worker
# API 重建后同步重建 Web，刷新 Nginx 的后端地址
docker compose --env-file .env --env-file deploy/ecs.env up -d --no-deps --force-recreate web
```

已有业务的实例应在任务空闲时变更配置，先备份再更新。环境构建需要访问依赖源；任务沙箱是否联网仍按版本授权控制。


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

安装、指定版本升级、自检、备份和恢复流程见 [部署指南](../deploy/README.md)。生产环境应选择经过验证的提交或 Release；GitHub `main` 上的新功能可能尚未进入最近的 Release。

旧工具 `scripts/migrate_sqlite_to_postgres.py` 仅支持旧版表合并。源库含有未支持的非空表时会在复制和写入前停止，**不能用它完成当前完整数据库的跨引擎迁移**。


## 当前边界

- 多 Skill 是同一 Agent、同一任务工作区内的协调执行；步骤依赖检查不等于通用并行 DAG、条件分支或多 Agent 调度。
- 模型兼容性取决于服务端对 Chat Completions、流式输出和工具调用等协议的实现。对话、视觉使用 OpenAI-compatible 接口；OCR 还支持 MinerU `/file_parse`，需要单独配置相应能力。
- 图片可以由视觉模型理解，OCR 按需开启；平台不保证所有文件格式都能完整解析。
- 依赖准备当前侧重受约束的 Python wheel 与平台能力目录，不提供通用 npm、apt 或源码编译服务。
- 快照是工作文件和 Agent 状态的恢复，不是进程快照；等待环境构建仍占用当前任务 Worker。
- 当前模型耗时、工具执行、文件规模与检查点 I/O 都会影响完成速度；减少流程调用不代表所有任务都获得固定比例提速。

