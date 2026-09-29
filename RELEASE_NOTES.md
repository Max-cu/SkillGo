# SkillGo v0.4.9

本版本聚焦**大文件上传链路与 Agent 失败可恢复性**：附件上传上限提升 20 倍至 200 MB 并修通整条代理/解析链路；`update_plan` 参数容错再补两处；命令失败改为只报实测事实。

## 主要更新

**附件上传上限提升至 200 MB**（729c05d + 2576f93 + 20b8c2e）

- 上限：任务附件、会话附件与工作区文件从每文件 10 MB 提升至 200 MB（`SKILLGO_WORKSPACE_MAX_FILE_BYTES`），前端校验与 nginx `client_max_body_size` 同步调整；Skill 包维持 50 MB 不变。
- nginx 对 `/api/` 关闭请求/响应缓冲，改为流式转发：大上传不再落到 web 容器 96m tmpfs（此前 96-201 MB 的上传会以 "no space left" 失败），大产物下载同样受益，并消除了"先落盘再转发"的额外延迟。
- API 容器 `/tmp` tmpfs 64m→2g、mem_limit 768m→4g：Starlette 会把大于 1 MiB 的上传先写入 /tmp，此前超过约 64 MB 的 multipart 上传直接解析失败。实测 150 MB 直传 API 通过。

**Agent 计划参数容错：success_criteria / validation_step_id**（5bc20a0 + f1753b2）

- 与 v0.4.7 的 goal 修复同类：中途重规划时缺省/null 的 `success_criteria` 沿用上次计划、`validation_step_id` 沿用已记录的验证步骤，不再为可修复的参数形态烧掉一整轮纠错推理（私有算力下每轮约 40-80 秒）。
- 不可解读的形态（dict/int）仍快速拒绝；任务首个计划仍必须有目标/标准/验证步骤；完成时的验证门槛不变，省略字段只能保留已记录内容、不会丢失需求。

**命令失败只报实测事实**（ad2d15d）

- 未到期限即退出的命令此前只返回裸 `{exit_code, stdout, stderr}`：生产中两次 exit 137 且输出全空的失败让模型拿到不透明的错误信息。
- 现在返回 `ok=false` + `SANDBOX_COMMAND_NONZERO_EXIT`：退出码、实测耗时 vs 预算、有无输出、沙箱与 /workspace 完好——只报事实、不做原因归因（模型解读，平台报告）。
- 事件层记录 `exit_code`/`elapsed_seconds`，回退串携带同样事实，静默失败可直接从 job_events 分类；压缩观察保留事实字段，message/stderr 双空时以 stdout 尾部作为诊断。

## 升级

本版本**无数据库迁移**。默认配置变化：附件/工作区文件上限 200 MB、API 容器资源（`/tmp` 2g、内存 4g）、nginx 对 `/api/` 流式转发。请在无运行中任务时执行：

```bash
sudo SKILLGO_INSTALL_ROOT=/opt/skillgo \
  SKILLGO_DEPLOY_ENV=deploy/ecs.env \
  bash deploy/upgrade-skillgo.sh v0.4.9
```

## 验证

- 后端 513 项回归全部通过（本版本新增：`success_criteria`/`validation_step_id` 形态容错、非零退出事实 payload/事件记录/压缩观察等用例）。
- v0.4.9 全部修复已在生产环境逐项部署并运行时断言通过（6/6 容器）；9 月 29 日当天 3 个 PPT 长任务全部成功（31-36 分钟）。
