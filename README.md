<p align="center">
  <img src="frontend/public/skillgo-logo.png" width="104" alt="SkillGo Logo" />
</p>

<h1 align="center">SkillGo</h1>

<p align="center">
  让 Skill 变成每个人的能力。<br />
  <sub>A self-hosted, multi-user Agent and Skill platform with isolated execution and verifiable artifacts.</sub>
</p>

<p align="center">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-3f36c9" />
  <img alt="Python" src="https://img.shields.io/badge/Python-3.12-3776ab" />
  <img alt="Node.js" src="https://img.shields.io/badge/Node.js-24-339933" />
  <img alt="Status" src="https://img.shields.io/badge/status-active%20development-6957ee" />
</p>

<p align="center">
  <a href="#主要能力">主要能力</a> ·
  <a href="#快速开始">快速开始</a> ·
  <a href="docs/README.md">项目文档</a> ·
  <a href="CONTRIBUTING.md">参与贡献</a> ·
  <a href="SECURITY.md">安全政策</a>
</p>

SkillGo 是一个可私有化部署的多用户 Agent 与 Skill 平台。用户通过自然语言和附件发起任务，平台负责执行已授权的 Skill、隔离运行环境、保存任务证据，并交付可下载的真实产物。

> 当前项目处于积极开发阶段。核心流程已有自动化测试覆盖，但在用于生产或处理敏感数据前，仍应完成组织级租户、数据库迁移、密钥托管、TLS、限流和独立执行控制面等加固。

## 为什么做 SkillGo

普通聊天工具擅长生成答案，但真正的工作往往还需要读取附件、调用工具、执行多步流程并交付文件。SkillGo 将这些步骤统一成可上传、可审核、可运行、可追踪的 Skill：

- **自然语言入口**：用户描述目标即可开始，不必先理解工作流参数。
- **真实任务执行**：工具调用、沙箱运行和文件产物都有实际证据，不以模型口述代替执行结果。
- **可复用与可治理**：Skill 有版本、权限、审核与发布状态，适合多人共同使用。
- **模型与执行解耦**：支持 OpenAI-compatible 模型服务，平台负责会话、工具、沙箱、存储和审计。

## 当前状态

| 模块 | 状态 |
| --- | --- |
| 对话工作台、上下文与附件 | 可用 |
| Skill 上传、版本、审核与社区 | 可用 |
| 多 Skill 任务与可验证产物 | 可用 |
| 同步/异步 API Endpoint | 可用 |
| Docker Compose 私有化部署 | 可用 |
| Linux + gVisor 任务隔离 | 可用，需宿主机安装 `runsc` |
| 组织级多租户、SSO、正式迁移体系 | 规划中 |

## 主要能力

- 对话式工作台：普通问答与 Skill 任务使用同一条会话主线。
- Skill 生命周期：上传、校验、版本化、审核、发布、收藏和下载。
- 开放包格式：兼容以顶层 `SKILL.md` 为入口的通用 Agent Skill，并支持可选的 `skillgo.yaml` 扩展。
- 多 Skill 编排：用户显式指定能力和顺序，执行器共享同一任务工作区。
- 任务级隔离：复杂任务在短生命周期容器中运行；Linux 部署可使用 gVisor `runsc`。
- 可验证交付：任务状态、工具事件、输入、产物、哈希和验证结果分别持久化。
- API 化：已发布的 Skill 可作为同步或异步 Endpoint 调用。
- 私有模型：通过 OpenAI-compatible Chat Completions 接口连接模型服务。

## 架构概览

```text
Browser / API client
        │
        ▼
React + TypeScript ──► FastAPI control plane ──► PostgreSQL
                              │                      │
                              │                      └─ users, skills, jobs,
                              │                         events, artifacts
                              ▼
                    lease-based sandbox worker
                              │
                              ▼
                    per-task container + gVisor
                              │
                              └─ isolated workspace / verified artifacts
```

控制面负责身份、权限、版本、任务租约、模型调用和产物索引；执行面只接收当前任务所需的包、文件和有限工具，不持有平台用户凭据。普通聊天无需创建沙箱，显式 Skill 任务按运行画像进入对应执行路径。

## 账号与角色

- **成员**：注册后立即启用，可使用工作台、创建和运行 Skill。
- **管理员**：注册申请进入待审核状态，由超级管理员批准后才能登录；启用后可处理平台审核与普通用户管理。
- **超级管理员**：每个 SkillGo 实例严格保留一个，由部署环境中的 Bootstrap 账号确定；角色不能通过公开接口转授。

旧版本产生重复超级管理员时，应用启动会保留 Bootstrap 主账号，并将其余账号安全调整为管理员，同时写入审计记录。

## 目录

| 路径 | 用途 |
| --- | --- |
| `backend/app` | FastAPI API、领域服务、Agent 状态、Worker 与沙箱协议 |
| `backend/tests` | 后端回归测试 |
| `frontend/src` | React 工作台、社区与管理界面 |
| `sandbox-runtime` | 任务沙箱镜像及受限工具入口 |
| `examples` | 示例 Skill 与 API 调用代码 |
| `docs` | 产品、架构、开发和接口文档；入口见 [docs/README.md](docs/README.md) |
| `deploy` | Linux 部署、自检与故障诊断脚本 |
| `poc` | 隔离执行方案的历史验证代码，不是生产入口 |

## 快速开始

> [!IMPORTANT]
> `.env.example` 只包含配置模板。首次启动前必须生成自己的数据库密码、JWT Secret、超级管理员密码和模型密钥；不要提交本机 `.env`。

### Docker Compose

需要 Docker Compose。要启用完整沙箱 Worker，还需要 Linux 主机和已安装的 gVisor `runsc`。

```powershell
Copy-Item .env.example .env
# 编辑 .env，至少替换数据库密码、JWT Secret 和超级管理员密码
docker compose up -d --build
```

默认网页地址为 `http://127.0.0.1:8080`。沙箱服务使用 Compose profile，启用方式和服务器准备步骤见 [开发说明](docs/development.md)。

服务器部署时请将 `deploy/ecs.env.example` 复制为 `deploy/ecs.env`，再按实际域名、Docker GID 和资源预算修改；该本机文件已被版本库忽略。完整步骤见 [deploy/README.md](deploy/README.md)。

### 本地开发

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements-dev.lock
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --reload
```

另开终端启动前端：

```powershell
Set-Location frontend
npm.cmd ci
npm.cmd run dev
```

前端为 `http://127.0.0.1:5173`，API 文档为 `http://127.0.0.1:8000/api/docs`。详细配置见 [docs/development.md](docs/development.md)，异步调用契约见 [docs/workflow-api.md](docs/workflow-api.md)。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
Set-Location frontend
npm.cmd run build
```

根目录的 `pyproject.toml` 已将测试发现范围固定在 `backend/tests`，本机缓存、数据库、历史包和部署备份不会参与测试或进入版本库。

## 安全边界

- Skill 包、模型响应、上传文件和工具结果一律视为不可信输入。
- 用户和会话的数据库查询、对象路径与沙箱工作区分别隔离。
- 没有真实工具证据和产物验证，任务不能标记为完成。
- 运行明细按保留策略清理；任务摘要和最终产物不会因明细清理而消失。
- 当前 Worker 管理 Docker 生命周期，生产部署应限制其宿主权限，并逐步迁移到独立执行控制面。

漏洞报告方式见 [SECURITY.md](SECURITY.md)。

## 参与和许可证

贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。SkillGo 采用 [MIT License](LICENSE)。第三方依赖继续遵循各自许可证，生产版本以 `backend/requirements.lock` 和 `frontend/package-lock.json` 为准；后端测试依赖单独锁定在 `backend/requirements-dev.lock`。

