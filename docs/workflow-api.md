# Skill 工作流 API

SkillGo 对外提供两类 Endpoint，由所发布 Skill 版本的执行模式决定：

| Endpoint 类型 | 调用方式 | 适用场景 |
| --- | --- | --- |
| `instruction_only` | 同步 JSON：`POST /api/v1/invoke/{slug}` | 纯指令处理，秒级~分钟级返回结构化结果 |
| `sandbox_required` | 异步任务：`POST /api/v1/workflow-endpoints/{slug}/jobs` | 需要文件与独立 Linux gVisor 沙箱的工作流 |

两种调用都使用 Endpoint 密钥：

```http
X-SkillGo-Key: skg_xxx
```

密钥只在创建或轮换 Endpoint 时完整显示一次。Endpoint 的创建与管理使用平台登录令牌（`Authorization: Bearer <JWT>`），见第 1 节。

接口始终随代码自动生成最新的 OpenAPI：`/api/openapi.json`、交互文档 `/api/docs`。本页是人工契约说明，如与 OpenAPI 不一致以 OpenAPI 为准。

## 1. 管理 Endpoint（平台令牌）

以下接口需要平台账号的 JWT（网页登录后获取），不是 `X-SkillGo-Key`。

### 创建 Endpoint

```http
POST /api/v1/endpoints
Authorization: Bearer <JWT>
Content-Type: application/json
```

```json
{
  "version_id": "skill-version-id",
  "slug": "drawing-check",
  "name": "图纸一致性校验"
}
```

- 只有**已发布（PUBLISHED）且可运行**的 Skill 版本能创建 Endpoint；`slug` 全局唯一，规则 `^[a-z0-9][a-z0-9-]*[a-z0-9]$`，3-100 字符。
- 响应 `201`，在 Endpoint 字段之外额外返回一次 **`api_key`（明文密钥，仅此一次）**，请立即保存。
- 常见错误：`409` slug 已占用；`422` 版本不是可外放类型；`409` + `RUNTIME_UNAVAILABLE` 运行环境未就绪。

### 列出 / 停用启用 / 轮换密钥

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/endpoints` | 列出自己拥有的 Endpoint（管理员可见全部） |
| `PATCH` | `/api/v1/endpoints/{id}` | 请求体 `{"is_active": false}` 停用，`true` 重新启用；停用后调用返回 `404` |
| `POST` | `/api/v1/endpoints/{id}/rotate-key` | 立即作废旧密钥并返回新明文密钥 |

## 2. 创建异步任务

```http
POST /api/v1/workflow-endpoints/{slug}/jobs
Content-Type: multipart/form-data
X-SkillGo-Key: skg_xxx
Idempotency-Key: your-request-id
```

表单字段：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `file` | 是 | Skill 的输入文件，单文件；大小上限由 `SKILLGO_WORKSPACE_MAX_FILE_BYTES` 控制，默认 **10 MB** |
| `instruction` | 否 | 自然语言补充要求，最长 20,000 字符 |

`Idempotency-Key` 为可选请求头，最长 **200 字符**，强烈建议设置：同一 Endpoint 下重复提交相同键时，平台返回第一次创建的任务，并在响应头加 `X-Idempotent-Replay: true`，不会重复执行、重复计费。

成功响应：`202 Accepted`，响应体为任务对象（见第 4 节），响应头 `Location` 指向任务查询地址。

```bash
curl -X POST "$SKILLGO_BASE_URL/api/v1/workflow-endpoints/your-slug/jobs" \
  -H "X-SkillGo-Key: $SKILLGO_API_KEY" \
  -H "Idempotency-Key: request-001" \
  -F "file=@./input.docx" \
  -F "instruction=重点检查日期、金额和前后矛盾"
```

## 3. 查询、追问与取消

```http
GET  /api/v1/workflow-endpoints/{slug}/jobs/{job_id}
POST /api/v1/workflow-endpoints/{slug}/jobs/{job_id}/cancel
```

任务状态：`queued`、`running`、`waiting_user`、`producing_artifacts`、`verifying`、`succeeded`、`failed`、`cancelled`、`blocked`。终态为 `succeeded`、`failed`、`cancelled`、`blocked`，建议外部系统每 2-5 秒轮询一次。

**关于 `waiting_user`**：Agent 在沙箱中可以向用户追问。外部 API 任务进入该状态后：

- 任务对象的 `pending_question` 字段会携带 `id` 与问题内容；
- 当前版本**不提供用 Endpoint 密钥回答追问的接口**：需要由 Endpoint 拥有者在网页端回答（任务挂在拥有者账号下），外部调用方也可以调用上面的 `cancel` 放弃任务；
- 因此无人值守的外部集成应把 `waiting_user` 视为需要人工介入的停滞状态处理（示例客户端会在该状态退出而非无限轮询）。为外部系统设计 Skill 时，应尽量让指令与输入一次齐备，减少追问。

取消返回 `{"message": ...}`，对终态任务取消返回 `409`。

## 4. 任务对象与产物

`GET` 任务接口与创建响应返回同一对象，关键字段：

```json
{
  "id": "uuid",
  "status": "running",
  "trigger": "api",
  "instruction": "...",
  "pending_question": null,
  "error_code": null,
  "error_message": null,
  "created_at": "2026-09-24T06:00:00Z",
  "started_at": null,
  "finished_at": null,
  "input_files": [{"id": "...", "filename": "input.docx", "size_bytes": 12345, "sha256": "..."}],
  "artifacts": [],
  "steps": [],
  "events": []
}
```

产物接口：

```http
GET /api/v1/workflow-endpoints/{slug}/jobs/{job_id}/artifacts
GET /api/v1/workflow-endpoints/{slug}/jobs/{job_id}/artifacts/{artifact_id}/download
```

产物对象含 `id`、`filename`、`content_type`、`size_bytes`、`sha256`、`kind`、`verified`、`created_at`。只有创建该任务的 Endpoint 密钥能读取任务与产物；即使另一个 Endpoint 属于同一用户、绑定同一 Skill 版本，也不能越过 Endpoint 与任务的绑定关系。`download` 在产物被存储保留策略清理后返回 `410 Gone`。

## 5. 同步调用

```http
POST /api/v1/invoke/{slug}
X-SkillGo-Key: skg_xxx
Content-Type: application/json
```

请求体固定为：

```json
{ "input": { "任意结构化字段": "由 Skill 的输入契约决定" }
```

成功 `200`：

```json
{
  "run_id": "uuid",
  "status": "succeeded",
  "output": { "Skill 输出契约中的结构化结果": "..." },
  "model_name": "qwen...",
  "latency_ms": 8421
}
```

执行失败返回 `502 Bad Gateway`，响应体带 `run_id` 便于排查：

```json
{ "detail": { "run_id": "uuid", "code": "ERROR_CODE", "message": "..." } }
```

在沙箱型 Endpoint 上调用同步接口返回 `409`（`ASYNC_WORKFLOW_REQUIRED`）；在纯指令 Endpoint 上创建文件任务返回 `409`（`SYNC_ENDPOINT_REQUIRED`）。请求体不符合 Skill 声明的输入契约返回 `422`。

## 6. 错误码速查

| HTTP | 场景 |
| --- | --- |
| `401` | 缺少或错误的 `X-SkillGo-Key` |
| `404` | Endpoint 不存在或已停用（故意不区分，避免探测 slug） |
| `409` | Endpoint 类型与调用方式不匹配；slug 冲突；终态任务再取消 |
| `422` | 缺少文件、文件为空/超限、`instruction` 超长、Idempotency-Key 超 200 字符、同步输入不符合契约 |
| `502` | 同步调用执行失败（携带 run_id 与错误码） |
| `503` | 沙箱运行环境暂不可用（`RUNTIME_UNAVAILABLE`） |
| `410` | 产物或输入已被保留策略清理 |

## 7. Python 完整示例

仓库提供可直接运行的 [workflow_api_client.py](../examples/workflow_api_client.py)：创建任务、轮询状态（正确处理 `waiting_user` 与失败终态），并下载全部已验证产物。

```powershell
pip install requests
$env:SKILLGO_BASE_URL="https://skillgo.example.com"
$env:SKILLGO_API_KEY="skg_xxx"
$env:SKILLGO_ENDPOINT_SLUG="your-slug"
$env:SKILLGO_INPUT_FILE="C:\path\to\input.docx"
python examples\workflow_api_client.py
```

## 隔离边界

外部 API 并不是绕过用户系统直接运行代码。Endpoint 固定绑定一个拥有者和一个已发布版本，创建的任务记到该拥有者名下；输入与产物使用 `用户 ID / 任务 ID` 存储路径；Worker 每次只领取一个确定任务，并创建只挂载该任务工作区的一次性非 root gVisor 容器（无网络权限除非管理员显式授予）。数据库查询、对象存储路径和运行容器三层同时限制访问范围。
