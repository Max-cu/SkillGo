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

SkillGo 使用 Alembic 管理 v0.1.0 之后的数据结构变更。空数据库会创建当前基线并写入 `alembic_version`；无版本表的现有 v0.1 数据库会先验证表和列，结构不完整时拒绝错误 Stamp。

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
SKILLGO_ORCHESTRATION_MODE=audit
```

`SKILLGO_CONTEXT_MAX_MESSAGES` / `SKILLGO_CONTEXT_MAX_CHARS` 只限制普通对话上下文。沙箱 Agent 另有一条投影。`SKILLGO_ORCHESTRATION_MODE=audit` 是默认值，行为与现在一致：旧交互整段保留，直到 token 预算装不下才整段丢弃；超大命令输出仍在写入历史时落到 `/workspace/work/tool-results/`。

`SKILLGO_ORCHESTRATION_MODE=fast` 是实验开关，不改变 `SKILLGO_DURABLE_CHECKPOINTS_ENABLED`，也不改变工作区快照频率。它只改变每次发给模型的投影：

- 最多保留最近 4 段交互（`SKILLGO_CONTEXT_MAX_EXCHANGES`，设为 `0` 表示不封顶）；
- 更早的工具正文超过 800 字符时，改成头 400 / 尾 200、sha256，以及指向持久历史或 `full_result_path` 的指针（`SKILLGO_COMPACT_OLDER_TOOLS`）；
- `references/deep/` 参考架只投影 path + sha256 + 短摘要（`SKILLGO_THIN_REFERENCE_SHELF`）。

已固定的 reference 正文如果还在保留的交互里，不会被压缩；最近一段交互原样发送。落库的消息、任务事件和工作区文件仍是全文。推理事件的 `context_budget` 会写下 `summarized_count`、`removed_tokens`、`preserved_exchange_count`、`exchange_cap_dropped_count`。在旁路实验量过延迟、每轮 token 和 PPT 通过率之前，不要在生产 anti-loop 路径打开 `fast`。

`SKILLGO_STRUCTURED_COMPACTION_SUMMARY` 默认关闭，并且不随 `fast` 打开。关闭时，发给模型的投影与未启用该摘要时相同。打开后，只有旧交互被整段丢弃、或旧工具正文被压缩时，才生成一份固定结构的摘要，附在投影**最后一条**状态消息里（`compaction_summary`）。栏目固定为：用户目标、约束、已做决定、创建或修改的文件路径、当前进度、待办步骤、已知错误。摘要不进入系统提示或已保留的交互，因此可缓存的稳定前缀不变。它只从被丢掉的历史和执行状态里提取这些事实，不依赖某个具体 Skill。装不下剩余预算时会截短或省略摘要，不会为了摘要再丢掉最近的交互。摘要复用已有任务计时（`model_wait_ms`、`tool_exec_ms`），不增加第二套计时。声明的状态文件优先使用预算，摘要只使用其后的剩余空间。
`SKILLGO_STATE_INJECT=1`（默认关闭）只在 `fast` 模式下生效，不改变 audit 投影。技能在 `SKILL.md` frontmatter 的 `metadata.state_files`（也接受顶层 `state_files`），或清单的 `metadata.state_files` / `spec.state_files`，声明要注入的工作区文件。每项是 `_state/<name>.json` 或 `/workspace/_state/<name>.json`，例如 `_state/photo_plan.json`。没有声明时不读取任何状态文件。开关关闭时同样不读取、不注入，投影与未开启时一致。

每一轮推理按声明顺序读取这些文件，把原文放在稳定历史前缀之后、最后一条执行状态消息之前。每个文件有 token 上限；超出时保留正文前缀，并注明需要回读原文件。文件缺失或读取失败只记 WARNING 并跳过该文件；同一个缺失文件每个任务只警告一次。文件 mtime 早于任务开始时间（`started_at`，否则 `created_at`；没有时区的时间按 UTC 解释）时，注入块和指标标出过期。推理事件的 `context_budget.state_inject` 按文件路径记录 `injected_tokens`、`file_mtime`、`validation` 和 `stale`。任务结束时的 JobEvent `timing` 记录累计模型等待、累计工具耗时和轮数；取消发生在等模型返回期间时，这段等待也计入 `model_wait_ms`。

`SKILLGO_AGENT_LOOP_RECOVERY_ENABLED` 默认关闭。关闭时，连续相同动作仍在第 4 次触发 `SANDBOX_AGENT_STALLED`，上下文预算不足或模型返回上下文长度错误时立即失败，不会重试。打开后：

- 同一个工具、同一组规范化参数（忽略 `reason` 和 `timeout_seconds`）连续出现 `SKILLGO_AGENT_REPEAT_TOOL_NOTE` 次（默认 3）时，向模型追加一句纠正说明；
- 连续次数达到 `SKILLGO_AGENT_REPEAT_TOOL_STOP`（默认比纠正阈值大 2）时，任务以 `SANDBOX_REPEATED_TOOL` 失败，错误信息写明工具名和次数；
- 本地投影超过上下文预算，或网关返回上下文长度错误时，用现有的工具正文压缩和交换上限把历史压缩一次再请求模型。最近一段交互（最后一条助手工具调用及其工具结果）保持原样，只压缩更早的正文。交换上限取当前保留上限的一半并向下取整，且至少再保留最近一段之外的一段；原来没有交换上限时，重试也不另加固定上限。持久消息不被改写。同一轮只压缩一次，第二次仍超限则按原来的错误失败。

如果模型不支持 `response_format: {"type":"json_object"}`，把 `SKILLGO_MODEL_JSON_MODE` 设为 `false`。内网 HTTPS 使用自签名证书时，优先把企业 CA 安装到运行环境；不要在生产环境关闭 TLS 校验。

平台设置中的每个模型连接可以声明“对话”“视觉”“OCR”一种或多种能力，并分别设置默认模型。视觉与 OCR 当前都使用 OpenAI-compatible `chat/completions` 多模态消息格式，图片以内联 `image_url` Data URL 发送给私有模型服务。视觉模型是图片附件的默认理解链路；工作台的“开启 OCR 识别”默认关闭，开启后平台先提取文字，再把 OCR 结果连同原图交给视觉模型，最后仍由用户选择的对话模型组织回答。OCR 失败时视觉理解继续执行并记录警告，视觉模型不可用且没有 OCR 结果时请求硬失败，不会假装已经理解图片。

网页运行 Skill 时使用 Agent 式自然语言会话，无需填写 JSON。会话固定到创建时选择的 Skill 版本；只有成功运行的输入与输出会进入后续上下文。清空或删除会话不会删除 Run 审计记录。Endpoint 调用仍需提交符合 Skill 输入 Schema 的 JSON。

### 会话文件工作区

在 Agent 输入框点击回形针即可给当前会话上传文件。文件只保存在平台管理的 `storage/workspaces/<用户>/<会话>/` 路径，所属用户之外的账号（包括管理员）不能列出、读取、下载或删除；平台不会读取用户电脑上的其他文件。

- 可提取文字并交给 Skill：TXT、Markdown、CSV、JSON、YAML、日志、HTML、XML、DOCX、XLSX；
- PNG、JPG/JPEG、WebP 会先校验文件签名，再由平台默认视觉模型理解；用户开启 OCR 时会追加 OCR 识别，解析结果及所用模型写入附件记录；
- PDF 等其他非脚本文件可安全保存和下载，但当前暂不自动交给模型；
- EXE、DLL、MSI、BAT、CMD、PowerShell、Shell、Python、JavaScript、JAR、快捷方式等可执行或脚本文件会被拒绝；
- 默认每个会话最多 30 个文件、单文件 10 MB；每次模型运行最多注入 40000 字符，防止文件把上下文无限撑大；
- 助手回复旁的“保存为文件”可把结果写入同一工作区，并供用户下载。

删除会话或 Skill 会同步清理对应的托管文件。对话附件、任务输入和生成产物统一保留 15 天；对话文字、任务状态、结果摘要与审计记录不会随文件到期而删除。保留期、扫描周期和孤儿文件缓冲期可通过 `SKILLGO_STORAGE_RETENTION_DAYS`、`SKILLGO_STORAGE_CLEANUP_INTERVAL_SECONDS`、`SKILLGO_STORAGE_ORPHAN_GRACE_HOURS` 调整。

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

运行摘要和用户消息不会被后台留存任务删除。详细运行事件默认在成功后保留 7 天、失败或取消后保留 30 天；附件、任务输入和生成产物统一保留 15 天。清理后仍保留文件名称、大小、哈希、任务状态和最终结果摘要，下载接口会明确返回文件已到期。清理周期和期限可通过对应的 `SKILLGO_AGENT_RUN_*` 与 `SKILLGO_STORAGE_*` 环境变量调整。

## 调用 Endpoint

部署已发布版本后，API Key 只完整显示一次。调用时把它放在专用 Header 中：

```powershell
$requestHeaders = @{ "X-SkillGo-Key" = "skg_replace_me" }
$requestBody = @{ input = @{ content = "需要总结的内容" } } | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/v1/invoke/summary-writer-1-0-0" -Headers $requestHeaders -ContentType "application/json" -Body $requestBody
```

Endpoint 密钥只保存 SHA-256 摘要，可在网页轮换；旧密钥轮换后立即失效。
