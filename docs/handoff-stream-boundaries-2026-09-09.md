# 流式等待边界修复与上线记录（2026-09-09）

本次在 v0.3.1 优化基础上补齐等待与取消边界，已部署至 SkillGo 服务器。没有重新运行完整 IDA 校对任务；本次验证覆盖传输、取消、服务健康及沙箱回收。

## 最终行为

- 首响应预算从每次 HTTP 请求开始计算，包括发送请求、等待响应头和等待首个有效生成事件。响应头返回不会重新获得一整段等待时间。
- SSE 注释、空行、空数据、只有 role 的 delta 不延长等待。正文、reasoning_content、工具名称或参数的非空增量计为生成进展。
- 收到有效生成后，按最后一次进展计算停滞预算。`timeout_seconds=0` 仍关闭单轮总截止；持续生成可超过 600 秒。
- 网关忽略 SSE、返回普通 JSON 时，完整响应必须在首响应预算内到达，避免无限等待读取正文。普通 JSON 不具备 SSE 的逐事件进展语义。
- 传输层只在没有有效生成前重试，最多三次；每次首响应预算独立，总预算若非零仍覆盖全部尝试。已有半截生成的断流不自动重放。只有完整响应才交给原有工具参数校验，半截参数不会执行。
- 模型等待期间每秒检查取消/任务租约状态。取消先结束并清理模型请求，再由现有 Worker 沙箱上下文执行回收。
- 诊断保留 `first_chunk_ms`，新增 `first_progress_ms` 和 `progress_events`，区分收到传输行与实际生成。

## 发现并修复的配置遗漏

原交接文档记录 600/600，但生产实测为首响应 120、停滞 60 秒。原因是 `compose.yaml` 的 API 和 Worker 环境变量仍默认 120/60，覆盖了 Python 配置里的新默认值；DeepSeek 没有数据库分层覆盖项。

已将 Compose 四处默认值改为 600，重新创建容器后实测生效：

| 项目 | 最终值 |
|---|---|
| 首响应等待 | 600 秒/尝试 |
| 有效生成停滞等待 | 600 秒 |
| DeepSeek 单轮总预算 | 0（数据库核对） |
| qwen3.5 / MinerU 总预算 | 120 秒（数据库核对，未修改） |
| 任务 wall-clock | 0 |

## 验证

- 完整后端回归：249 passed，117.60 秒；只有已有 Starlette 依赖弃用警告。
- 新增 11 项本地 TCP 模拟服务测试；使用真实 HTTPX 客户端，覆盖响应头不返回、心跳空转、role-only、普通 JSON 正文持续空白、生成后心跳停滞、持续正文/推理/工具参数、半截工具参数、用户取消及外层任务取消。持续生成用例通过 `post_json` 验证总预算关闭语义。
- 本地 Windows 测试使用 SelectorEventLoop，与生产 Linux 事件循环类型一致，避免 Python 3.12 Proactor 在对端重置连接时的测试服务器回收异常。
- 部署前两次切换均确认活跃任务为零；Compose 配置校验通过。
- API / Worker 三个代码文件哈希与本地部署清单一致；API 健康、Web `/` 和 `/health` 通过。
- 真实 gVisor 沙箱：runsc、无网络、文件产物验证和自动回收通过。
- 真实沙箱中使用模拟挂起的模型调用：取消传播、待完成调用清理和沙箱回收通过，不消耗私有模型推理。

## 部署与回滚

- 新镜像：`9e04da45c9ed02c9c72cd47f876567f167e456b0a2c64f6475e7dcda3a98f1e2`。
- 镜像标签：`org.skillgo.hotfix=stream-boundaries-20260909`。
- 基础镜像：`72fefde51ff44e5fd06e632b4db5e1330ebd1ecd8ffd42f9b98d4c985c020e0d`。
- 服务器备份目录：`/opt/skillgo/.deploy/stream-boundaries-20260909/`。
- 目录包含源文件备份 `source-before.tar.gz`、`compose-before.yaml` 和前后哈希/基础镜像清单 `manifest.json`。
- 旧镜像标签：`skillgo-api:before-stream-boundaries-20260909`、`skillgo-worker:before-stream-boundaries-20260909`。
- 服务器没有 Git 工作树；`.deploy/revision` 保留基础记录 `33d0336`，本次补丁以镜像标签和 manifest 标识。尚未创建 Git 提交或推送。

回滚时先确认无活跃任务，恢复三个源文件及 Compose 备份，将旧镜像重标记为 `skillgo-api` / `skillgo-worker`，用 `.env`、`deploy/ecs.env` 和 `--profile sandbox` 的 Compose 命令重建 API/Worker；API 健康后强制重建 Web 并验证路由。恢复旧 Compose 也会恢复 120/60 秒默认值，需明确是否需要这一行为。

下一步可用相同输入重跑一次 IDA，关注实际进展时间、任务总耗时和最终校对内容质量。模拟测试通过不等于完整校对业务已经再次通过。
