# Skill 工作流 API

SkillGo 的 Endpoint 分为两类：

- `instruction_only`：同步 JSON 调用，使用 `POST /api/v1/invoke/{slug}`。
- `sandbox_required`：异步文件工作流，使用本页描述的任务 API。每个任务在独立的 Linux gVisor 沙箱中执行。

API Key 只在创建或轮换 Endpoint 时完整显示一次。所有请求都使用：

```http
X-SkillGo-Key: skg_xxx
```

## 1. 创建任务

```http
POST /api/v1/workflow-endpoints/{slug}/jobs
Content-Type: multipart/form-data
X-SkillGo-Key: skg_xxx
Idempotency-Key: your-request-id
```

表单字段：

- `file`：必填，Skill 的输入文件，当前上限由 `SKILLGO_WORKSPACE_MAX_FILE_BYTES` 控制，默认 10 MB。
- `instruction`：可选，自然语言补充要求，最长 20,000 字符。

`Idempotency-Key` 可选但强烈建议设置。相同 Endpoint 下重复提交同一个键时，平台返回第一次创建的任务，并通过 `X-Idempotent-Replay: true` 标记重放。成功响应为 `202 Accepted`，`Location` 响应头指向任务查询地址。

```bash
curl -X POST "$SKILLGO_BASE_URL/api/v1/workflow-endpoints/your-slug/jobs" \
  -H "X-SkillGo-Key: $SKILLGO_API_KEY" \
  -H "Idempotency-Key: request-001" \
  -F "file=@./input.docx" \
  -F "instruction=重点检查日期、金额和前后矛盾"
```

## 2. 查询与取消

```http
GET  /api/v1/workflow-endpoints/{slug}/jobs/{job_id}
POST /api/v1/workflow-endpoints/{slug}/jobs/{job_id}/cancel
```

任务状态包括：`queued`、`running`、`waiting_user`、`producing_artifacts`、`verifying`、`succeeded`、`failed`、`cancelled` 和 `blocked`。

`succeeded`、`failed`、`cancelled`、`blocked` 是终态。外部系统通常每 2 秒查询一次；后续可以再增加 Webhook，避免持续轮询。

## 3. 获取产物

```http
GET /api/v1/workflow-endpoints/{slug}/jobs/{job_id}/artifacts
GET /api/v1/workflow-endpoints/{slug}/jobs/{job_id}/artifacts/{artifact_id}/download
```

只有创建该任务的 Endpoint Key 可以读取任务与产物。即使另一个 Endpoint 属于同一用户、绑定同一 Skill 版本，也不能越过 Endpoint 与任务的绑定关系。

## 4. Python 完整示例

仓库提供了可直接运行的 [workflow_api_client.py](../examples/workflow_api_client.py)：创建任务、轮询状态，并下载全部已验证产物。

```powershell
pip install requests
$env:SKILLGO_BASE_URL="https://skillgo.example.com"
$env:SKILLGO_API_KEY="skg_xxx"
$env:SKILLGO_ENDPOINT_SLUG="your-slug"
$env:SKILLGO_INPUT_FILE="C:\path\to\input.docx"
python examples\workflow_api_client.py
```

## 隔离边界

外部 API 并不是绕过用户系统直接运行代码。Endpoint 固定绑定一个拥有者和一个已审核版本，创建的任务记到该拥有者名下；输入与产物使用 `用户 ID / 任务 ID` 存储路径；Worker 每次只领取一个确定任务，并创建只挂载该任务工作区的一次性非 root gVisor 容器。数据库查询、对象存储路径和运行容器三层同时限制访问范围。
