# 工作交接：模型超时体系 QwenPaw 对齐与 v0.3.1 发布（2026-09-09）

> 面向 Codex 的进度对接文档。本轮工作由 Trae 完成，主线是：诊断长报告任务被超时误杀 → 调研 QwenPaw 编排设计 → 三轮改造对齐语义 → 生产验证 → 发布 v0.3.1。

## 一、问题背景

同一 Skill（IDA 3005 校对，模型 deepSeek-v4-pro）在 WorkBuddy/QwenPaw 能跑通，在 SkillGo 反复失败。多次失败形态：

| 任务 | 失败轮次 | 错误 |
|---|---|---|
| aff5586d | 第 30 轮 | 单次生成 599.997s 撞 600s 总预算 |
| 789e98c6 | 第 25 轮 | MODEL_RESPONSE_TIMEOUT（600s 预算耗尽） |
| 164a20fc | 第 10 轮 | 单轮生成 1.5MB / 4885 SSE 块仍未完成，600s 掐断 |

根因定性：模型直接在单轮内生成整份校审报告（巨量长输出，持续有数据流动），旧版"含重试总预算 600s"把合法长生成误判为超时。**平台设计问题，不是 Skill 问题。**

## 二、QwenPaw 调研结论（对齐依据）

- 模型调用层：`model_factory.py` 无任何推理超时，完全依赖 openai SDK 流式 per-chunk read timeout 600s 语义——**只要数据持续流动就永不超时**。
- 任务层：内置三种执行模式（default/goal/mission）均无 wall-clock 总时限；防失控靠 ReAct 最大迭代 100 + DoomLoopGate 连续重复检测（4 次重复干预）；取消仅手动。
- 唯一内置时限：心跳定时任务 3600s。
- 工具/沙箱：命令默认 30s，per-tool 注册默认 shell 60s / browser 300s，硬上限 24h。

## 三、三轮改造（提交链）

```
b2d0d34  失败轮次事件 + worker 日志持久化 transport 诊断（model_transport: attempts/chunks/bytes/first_chunk_ms/done）
7a083e9  单轮总预算可选：timeout_seconds=0 = 不限时；空闲检测默认 120/60 → 600/600（连接 15 不变）；schemas/前端允许 0=不限
33d0336  任务级上限放开：SKILLGO_SANDBOX_JOB_TIMEOUT_SECONDS 0=禁用（默认 0）；轮数 100→300、工具操作 160→480（均支持 0=禁用）
e69e5f7  chore: release SkillGo v0.3.1（tag v0.3.1，GitHub Release success）
```

### 最终超时语义（全对齐 QwenPaw）

| 层 | 配置 | 值 | 说明 |
|---|---|---|---|
| 连接 | `SKILLGO_MODEL_CONNECT_TIMEOUT_SECONDS` | 15s | 不变 |
| 首响应 | `SKILLGO_MODEL_FIRST_CHUNK_TIMEOUT_SECONDS` | 600s | 无任何数据才触发 |
| 流停滞 | `SKILLGO_MODEL_STREAM_STALL_TIMEOUT_SECONDS` | 600s | 中途无新数据触发 |
| 单轮总预算 | 模型连接 `timeout_seconds`（DB） | **0 = 不限** | deepSeek-v4-pro 已设 0；qwen3.5 / MinerU 保持 120 |
| 任务 wall-clock | `SKILLGO_SANDBOX_JOB_TIMEOUT_SECONDS` | **0 = 禁用** | 服务器 ecs.env 已设 0 |
| 推理轮数 | `SKILLGO_SANDBOX_MAX_AGENT_TURNS` | 300（0=禁用） | QwenPaw 为 100，放宽 3 倍 |
| 工具操作 | `SKILLGO_SANDBOX_MAX_AGENT_TOOL_CALLS` | 480（0=禁用） | 同比例放宽 |

防失控体系（去时间化）：轮数 300 封顶 + 重复循环干预（连续 2 轮单例工具即介入）+ 每轮 600s 空闲检测 + 工具命令 120s。核心原则：**只要 Agent 在动就不掐，真卡死 600 秒内被空闲检测发现。**

### 关键实现位置（backend/）

- `app/model_adapter.py`：`_consume_sse`（分层预算消费）、`post_json`（deadline=inf when timeout=0）、`httpx.Timeout` read/write 传 None
- `app/model_gateway.py`：`ModelConnection.http_timeout` 属性（0→None）、`_transport_error` 各错误码文案适配无限语义
- `app/sandbox_agent_loop.py`：轮数用 `itertools.count(1)`（0=无限）；工具调用上限 `> 0 and` 短路
- `app/sandbox_worker.py`：`job_timeout > 0` 才包 `asyncio.wait_for`
- 版本号：`app/main.py` 两处 0.3.1

## 四、生产验证

重跑同一校对任务 `cf85c18e`（设计校对2.txt → Word 报告）：

- **SUCCEEDED**：44 轮推理，46.5 分钟，无任何 `MODEL_*_TIMEOUT`
- 关键证据：第 11 轮单轮持续约 12 分钟（正是历史死点形态）顺利通过；第 10/25/30 轮历史失败点全部安全跨过

## 五、服务器状态（10.2.14.138）

- 运行代码 = `33d0336`（与 v0.3.1 tag 运行内容一致，`e69e5f7` 仅版本号/文档差异）；`.deploy/revision` 记 `33d0336`
- 镜像：`skillgo-api` / `skillgo-worker` = 72fefde51ff4（增量构建，备份 tag `skillgo-api:before-no-wall-clock-20260909`）
- `deploy/ecs.env`：`JOB_TIMEOUT=0 / MAX_AGENT_TURNS=300 / MAX_AGENT_TOOL_CALLS=480`
- DB（model_connection_configs 表）：deepSeek-v4-pro `timeout_seconds=0`（qwen3.5 / MinerU=120 不变）；分层超时未按模型覆盖，走 env 默认 600/600/15
- 备份/回滚：`.deploy/no-wall-clock-20260909/`（manifest.json + after.md5 + rollback.md）；此前还有 `.deploy/streaming-timeouts-20260908/`、`.deploy/unlimited-round-budget-20260908/`
- git：main 已推送 GitHub（`e69e5f7`），tag `v0.3.1` 已触发 Release workflow 并 success；CI main 进行中

## 六、给 Codex 的注意事项

1. **工作区一致性**：`7a083e9` 部署后发现本地 `backend/app/model_gateway.py` 被回退（丢失已部署的 `http_timeout` 属性，4 个测试失败），已 `git restore` 恢复。若你在此工作区改代码，动前先 `git status` + `git diff` 核对，避免覆盖/回退他人改动。
2. **服务器对比**：本地是 CRLF、服务器是 LF，对比内容一致性用 `git show HEAD:<path>` 的规范化哈希，不要直接比文件。
3. **离线部署唯一可行模式**：`.deploy/Dockerfile.<tag>` 以 `FROM skillgo-api:before-<tag>` 为基的增量构建；`DOCKER_BUILDKIT=0 docker build --pull=false -f .deploy/Dockerfile.<tag> -t skillgo-api -t skillgo-worker backend`；重建容器必须带双 env（`--env-file .env --env-file deploy/ecs.env`）。
4. **改 env 默认值**要同步四处：`backend/app/config.py`、`compose.yaml`、`.env.example`、`deploy/ecs.env.example`，以及服务器实际 `deploy/ecs.env`。
5. 本轮全量测试 **238 项通过**（`backend/tests/`，`pytest tests/ -q`）；新增用例在 `tests/test_model_resilience.py`（含 timeout=0 无限预算、stall 仍生效等 25 项）。

## 七、可能的后续方向（未做）

- Skill 提示词层面：要求分段生成报告 + python-docx 逐段落盘，减少单轮巨量生成（治本方向之一，需用户决定）
- 服务器 `.deploy/revision` 与 tag 版本的对应关系可再规范化（当前记运行代码 commit）
- 空闲检测的极端边界：模型服务器静默挂起 TCP 且零字节发送，由流停滞检测 600s 兜底，目前无实测案例
