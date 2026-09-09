# SkillGo v0.3.1

本次发布集中升级模型调用的传输韧性与超时语义，对齐 QwenPaw 的编排设计：只要 Agent 持续产出即不掐断任务，真正的卡死在空闲检测窗口内被发现。解决长报告类任务（单轮巨量生成）在旧版本被总预算误杀的问题。

## 主要更新

- Agent 推理改为 SSE 流式接收，聚合增量内容、推理文本与工具调用分片；网关忽略流式请求时自动回退整体 JSON。
- 分层超时独立可配：连接 15 秒、首响应与流停滞默认 600 秒，可按模型在 agent_options 覆盖；模型单轮总预算支持 `timeout_seconds=0` 不限时。
- 传输进展诊断：推理轮次事件记录首响应耗时、数据块数、字节数与重试次数；失败区分 `MODEL_FIRST_RESPONSE_TIMEOUT` 与 `MODEL_STREAM_STALLED` 错误码并附带统计。
- 沙箱任务取消 wall-clock 总时限（`SKILLGO_SANDBOX_JOB_TIMEOUT_SECONDS=0` 可重新启用），推理轮数与工具操作上限放宽到 300/480 次且支持 `0=禁用`；防失控依赖轮数上限、重复循环干预与分层空闲检测。
- 上下文分配优先保留最近一次完整工具交互，大观察结果落盘保存有界摘录；模型预算快照写入任务运行时记录。

## 升级

本版本无数据库迁移。更新 API、Worker 与前端后重启即可；按需调整模型连接的 `timeout_seconds`（建议报告类模型设 0）与沙箱超时环境变量。

Git 工作区部署可使用以下命令；源码压缩包部署需按现有安装方式更新源文件，不能直接运行依赖 Git checkout 的升级脚本。

```bash
sudo SKILLGO_INSTALL_ROOT=/opt/skillgo \
  SKILLGO_DEPLOY_ENV=deploy/ecs.env \
  bash deploy/backup-skillgo.sh

sudo SKILLGO_INSTALL_ROOT=/opt/skillgo \
  SKILLGO_DEPLOY_ENV=deploy/ecs.env \
  bash deploy/upgrade-skillgo.sh v0.3.1
```

## 验证与能力边界

- 后端 238 项回归通过；前端 TypeScript 检查与 Vite 正式构建通过。
- 生产环境实测：同一校对任务从旧版多轮超时失败（第 10/25/30 轮）变为 44 轮完整成功，含单轮约 12 分钟的长生成。
- 空闲检测依赖响应流有数据到达；模型服务器静默挂起 TCP 连接且不发送任何字节的极端场景由流停滞检测在 600 秒内兜底。

完整变更见 [CHANGELOG.md](CHANGELOG.md)，超时问题排查过程见 [模型超时调查记录](docs/model-timeout-investigation-2026-09-08.md)。
