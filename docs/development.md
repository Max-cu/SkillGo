# SkillGo 本地开发

## 当前纵向切片

- 本地账号注册和登录；
- `super_admin`、`admin`、`user` 三级角色；
- Skill 社区、详情、用户工作台和角色后台；
- Skill 创建、ZIP 上传、包安全检查和不可变版本；
- 提交审核、批准/驳回、社区发布；
- 收藏和授权下载 API；
- 按用户、Skill 和版本隔离的多轮会话上下文；
- 按用户和会话隔离的托管文件工作区；
- 私有模型 Instruction Runner、运行记录与 Endpoint 调用；
- SQLite 本地开发与 PostgreSQL Compose 配置。

纯指令 Skill 可调用私有模型；包含脚本、文件工具或文档生成的 Skill 由 Linux Worker 在任务级 gVisor 沙箱中执行。本机 Windows 开发环境可以运行控制面和全部单元测试，但不能提供 `runsc` 隔离。

## 后端开发

复制 `backend/.env.example` 为 `backend/.env`，替换 JWT Secret 和首个超级管理员密码，然后：

```powershell
.\.venv\Scripts\python.exe -m pip install -r backend\requirements-dev.lock
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --env-file backend/.env --reload
```

API 文档：`http://127.0.0.1:8000/api/docs`

### 数据库迁移

SkillGo 使用 Alembic 管理 v0.1.0 之后的数结构变更。空数据库会创建当前基线并写入 `alembic_version`；无版本表的现有 v0.1 数据库会先验证表和列，结构不完整时拒绝错误 Stamp。

修改 SQLAlchemy Model 后，在 `backend` 目录生成新迁移：

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m alembic revision --autogenerate -m "describe schema change"
..\.venv\Scripts\python.exe -m alembic upgrade head
```

生成文件必须人工检查 `upgrade()` 和 `downgrade()`，并在空库、v0.1 基线库和包含业务数据的备份上测试。不得用 `create_all` 替代新版本的显式迁移。

## 私有模型

Instruction Runner 使用 OpenAI-compatible `chat/completions` 接口。模型地址应包含兼容 API 的版本前缀，例如：

```dotenv
SKILLGO_MODEL_BASE_URL=http://127.0.0.1:8001/v1
SKILLGO_MODEL_API_KEY=your-private-key
SKILLGO_MODEL_NAME=your-model-name
SKILLGO_MODEL_JSON_MODE=true
SKILLGO_MODEL_TLS_VERIFY=true
SKILLGO_CONTEXT_MAX_MESSAGES=20
SKILLGO_CONTEXT_MAX_CHARS=30000
SKILLGO_CONVERSATION_LOCK_SECONDS=180
```

如果模型不支持 `response_format: {"type":"json_object"}`，把 `SKILLGO_MODEL_JSON_MODE` 设为 `false`。内网 HTTPS 使用自签名证书时，优先把企业 CA 安装到运行环境；不要在生产环境关闭 TLS 校验。

网页运行 Skill 时使用 Agent 式自然语言会话，无需填写 JSON。会话固定到创建时选择的 Skill 版本；只有成功运行的输入与输出会进入后续上下文。清空或删除会话不会删除 Run 审计记录。Endpoint 调用仍需提交符合 Skill 输入 Schema 的 JSON。

### 会话文件工作区

在 Agent 输入框点击回形针即可给当前会话上传文件。文件只保存在平台管理的 `storage/workspaces/<用户>/<会话>/` 路径，所属用户之外的账号（包括管理员）不能列出、读取、下载或删除；平台不会读取用户电脑上的其他文件。

- 可提取文字并交给 Skill：TXT、Markdown、CSV、JSON、YAML、日志、HTML、XML、DOCX、XLSX；
- 可安全保存和下载，但暂不自动交给模型：PDF、PNG、JPG 等其他非脚本文件；
- EXE、DLL、MSI、BAT、CMD、PowerShell、Shell、Python、JavaScript、JAR、快捷方式等可执行或脚本文件会被拒绝；
- 默认每个会话最多 30 个文件、单文件 10 MB；每次模型运行最多注入 40000 字符，防止文件把上下文无限撑大；
- 助手回复旁的“保存为文件”可把结果写入同一工作区，并供用户下载。

删除会话或 Skill 会同步清理对应的托管文件。对话附件、任务输入和生成产物默认保留 15 天；对话文字、任务状态、结果摘要与审计记录不会随文件到期而删除。重要任务可以标记为长期保留。保留期、扫描周期和孤儿文件缓冲期可通过 `SKILLGO_STORAGE_RETENTION_DAYS`、`SKILLGO_STORAGE_CLEANUP_INTERVAL_SECONDS`、`SKILLGO_STORAGE_ORPHAN_GRACE_HOURS` 调整。

## 前端开发

```powershell
cd frontend
npm.cmd run dev
```

网页：`http://127.0.0.1:5173`

## 测试和构建

```powershell
.\.venv\Scripts\python.exe -m pytest backend\tests -q
cd frontend
npm.cmd run build
npm.cmd audit
```

## 私有化 Compose

1. 把根目录 `.env.example` 复制为 `.env`；
2. 替换数据库密码、JWT Secret 和首个超级管理员密码；
3. 启动：

```powershell
docker compose up -d --build
```

默认只绑定本机：`http://127.0.0.1:8080`。

如果镜像构建阶段无法连接 Docker Hub 或 PyPI，先确认 Docker Desktop 的代理/DNS 配置；这不影响使用上面的本机开发方式运行。

## 制作示例 ZIP

标准 Agent Skill ZIP 只需包含带 `name`、`description` YAML Frontmatter 的 `SKILL.md`；`scripts/`、`references/` 和 `assets/` 均为可选。需要结构化 API Schema 或权限声明时，可额外提供 `skillgo.yaml`。旧版 `manifest.yaml` 继续兼容。

在网页选择 ZIP 后，平台会先做安全校验，再使用已配置的私有模型生成名称、唯一标识、简介、详细说明和分类。模型未配置或调用失败时，平台会使用包内元数据兜底，用户始终可以在创建前修改结果。

## 工作流任务

平台会依据包内脚本、`SKILL.md` 命令、运行时和网络声明，把版本识别为：

- `instruction_only`：纯文本说明型 Skill，由当前可信执行器运行并生成经过哈希校验的文本产物；
- `sandbox_required`：包含脚本、命令、兼容工具调用，或需要生成 DOCX/XLSX/PDF/PPTX 的 Skill，由 Linux 沙箱 Worker 执行；
- `platform_tools`：仅用于声明了沙箱兼容层尚无法替代的外部平台工具的 Skill。

沙箱兼容层会把常见的第三方 Agent 工具名称按能力映射到 SkillGo 原语，例如目录浏览映射为 `list_files`，Word/Excel/PDF/PPT 生成映射为 `run_python` 与对应文档库。Skill 不需要为了 SkillGo 改写成私有格式；无法安全等价替代的外部服务工具仍会保持阻断并明确列出。

工作流状态、步骤、输入文件和产物分别保存在独立数据表中。网页与 API 均使用 `/api/v1/jobs`，产物通过 `/api/v1/jobs/{job_id}/artifacts/{artifact_id}/download` 下载。旧的 `/api/v1/runs` 只用于纯指令对话和同步 Endpoint 调用。

普通工作台对话和 Skill 工作流都会创建统一的 `agent_runs` 运行记录。Sandbox Worker 使用带心跳的任务租约；租约过期后，原尝试会被 fencing token 阻止继续落库，任务最多在新的独立 gVisor 沙箱中自动尝试 3 次。默认租约为 90 秒、每 15 秒续租，可通过 `SKILLGO_SANDBOX_WORKER_LEASE_SECONDS`、`SKILLGO_SANDBOX_WORKER_HEARTBEAT_SECONDS` 和 `SKILLGO_SANDBOX_WORKER_MAX_ATTEMPTS` 调整。

运行摘要和用户消息不会被后台留存任务删除。详细运行事件默认在成功后保留 7 天、失败或取消后保留 30 天；附件、任务输入和生成产物默认保留 15 天，长期保留任务除外。清理后仍保留文件名称、大小、哈希、任务状态和最终结果摘要，下载接口会明确返回文件已到期。清理周期和期限可通过对应的 `SKILLGO_AGENT_RUN_*` 与 `SKILLGO_STORAGE_*` 环境变量调整。

## 调用 Endpoint

部署已发布版本后，API Key 只完整显示一次。调用时把它放在专用 Header 中：

```powershell
$requestHeaders = @{ "X-SkillGo-Key" = "skg_replace_me" }
$requestBody = @{ input = @{ content = "需要总结的内容" } } | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/v1/invoke/summary-writer-1-0-0" -Headers $requestHeaders -ContentType "application/json" -Body $requestBody
```

Endpoint 密钥只保存 SHA-256 摘要，可在网页轮换；旧密钥轮换后立即失效。
