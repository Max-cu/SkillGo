# Skill 工作流 API

SkillGo 对外提供两类 Endpoint，由所发布 Skill 版本的执行模式决定：

| Endpoint 类型 | 调用方式 | 适用场景 |
| --- | --- | --- |
| `instruction_only` | 同步 JSON：`POST /api/v1/invoke/{slug}` | 纯指令处理，秒级~分钟级返回结构化结果 |
| `sandbox_required` | 异步任务：`POST /api/v1/workflow-endpoints/{slug}/jobs` | 接收文本或文件，在独立 Linux gVisor 沙箱中执行的工作流 |

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
| `file` | 否 | Skill 的输入文件，单文件；大小上限由 `SKILLGO_WORKSPACE_MAX_FILE_BYTES` 控制，默认 **10 MB** |
| `instruction` | 否 | 自然语言制作要求，最长 20,000 字符；未提供文件时必须为非空文字 |

纯文本请求省略 `file`，平台只保留任务指令，不创建 ZIP 或占位文本附件；文件与非空指令不能同时缺失。纯文本任务支持取消和重试。

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

任务状态：`queued`、`running`、`waiting_user`（仅历史任务，见下）、`producing_artifacts`、`verifying`、`succeeded`、`failed`、`cancelled`、`blocked`。终态为 `succeeded`、`failed`、`cancelled`、`blocked`，建议外部系统每 2-5 秒轮询一次。

**无人值守，不会追问**：当前版本的任务一口气执行到底，Agent 无法向用户提问；信息不足时它必须自行采用最合理的假设继续，并在最终摘要中说明该假设。因此新任务不会再进入 `waiting_user`，外部集成也不需要实现问答回调。该状态值仅为 v0.4.3 之前的历史任务保留。

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
| `422` | 文件与非空指令均缺失、文件为空/超限、`instruction` 超长、Idempotency-Key 超 200 字符、同步输入不符合契约 |
| `502` | 同步调用执行失败（携带 run_id 与错误码） |
| `503` | 沙箱运行环境暂不可用（`RUNTIME_UNAVAILABLE`） |
| `410` | 产物或输入已被保留策略清理 |

## 7. Python 完整示例

仓库提供可直接运行的 [workflow_api_client.py](../examples/workflow_api_client.py)：创建任务、轮询状态（识别失败终态与历史 `waiting_user` 任务），并下载全部已验证产物。

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


## 交付文件收集与接收策略

Skill 可以保留自己的导出目录，例如 `/workspace/skills/.../exports/report.pptx`。
Agent 在 `finish.artifacts` 中声明确切的普通文件路径，框架会将这些文件安全地复制到交付区。
无需修改 Skill 以适应平台的 `/workspace/output` 路径。框架不会遍历并自动交付整个 Skill 包；
路径越界、文件缺失、目录、符号链接及空文件/超限文件会返回可修正的工具错误。

自 v1.0.0 起默认使用文件接收模式，不需要调用方或部署者额外开启。部署者可显式设置
`SKILLGO_ARTIFACT_DELIVERY_ONLY=false` 选择额外平台完整验证；如果此前配置了
`SKILLGO_DELIVERY_ONLY_SKILL_IDS` 白名单，其中的 Skill 仍沿用文件接收策略。
文件接收模式不运行额外的模型验证程序，不要求验证报告，
仅确认声明文件可安全读取、复制和保存，不判定 PPT 的内容、版式或文件格式质量。
文件自身的 Skill 验证流程保持原样。按白名单选择接收模式时，混合其他 Skill 的任务仍使用完整验证。
接收模式中的 `verified` 仅代表文件已成功保存和接收，不代表通过了业务或版式审查。

API 创建任务时会复制 Endpoint 所绑定版本的联网授权和 `follow_skill_flow` 设置。
调用方不必传入这两个开关；后续修改版本设置不改变已创建任务。
文件接收模式下，模型可在最后一组操作中使用通用 `delivery` 声明；整组成功、声明文件由本组生成或更新，
且完成门槛满足时，框架直接收集文件，无需下一轮 `finish`。该能力不按账号、版本或格式设限，
详见 [通用最终操作交付](final-operation-delivery.md)。原有白名单工作流适配仍保留；
证据不足、还有模型审阅步骤或文件名未知时，继续普通结束路径。
