# SkillGo v0.4.3

本次发布把**单条沙箱命令的执行预算从 900 秒提高到 3000 秒**，并清理代码里散落的 900 秒硬编码。同时包含 v0.4.2 的全部快照健壮性改进（如从 v0.4.1 升级请一并阅读）。

## 主要更新

**命令超时 900s → 3000s**

- `SKILLGO_SANDBOX_COMMAND_TIMEOUT_SECONDS` 默认值与 ECS 配置统一改为 **3000**：图纸一致性校验等批量任务单批实际需要 30-45 分钟，此前每条命令在第 900 秒被精确终止（exit 124），模型只能反复续跑。
- 工具层此前有 4 处独立硬编码 900（模型工具 schema 的 maximum/文案、动作参数校验、系统提示、超时恢复提示），现全部改为从该配置动态取值——以后调整预算只需改一处环境变量，不会再出现"env 改了但校验仍按 900 拒绝"的不一致。
- `run_python` 工具的 600 秒上限保持不变（一次性 Python 片段不适合长驻）；任务级总超时仍为关闭（0）。
- 超时语义不变：到期由作业容器内 GNU timeout 发 TERM（5 秒后 KILL），沙箱与 /workspace 保留，模型从脚本保存的批次状态恢复；只有 docker-exec 传输彻底卡死才回收容器。

## v0.4.2 一并包含的快照改进

- 快照超容量/Docker 传输抖动不再杀死任务：跳过后续快照、记录中性事件，任务继续；完整性错误仍致命。
- 快照上限 256 MiB → 512 MiB。
- `/workspace/input` 不可变输入不再进入快照，恢复/换箱/依赖升级时从对象存储重新投放。
- 快照辅助容器删除超时 60s → 180s 并重试一次，清理失败不掩盖主错误。

## 升级

本版本**无数据库迁移**。需要更新配置 `SKILLGO_SANDBOX_COMMAND_TIMEOUT_SECONDS=3000` 并重建 api、worker 镜像（请在无运行中任务时执行）。

```bash
sudo SKILLGO_INSTALL_ROOT=/opt/skillgo \
  SKILLGO_DEPLOY_ENV=deploy/ecs.env \
  bash deploy/upgrade-skillgo.sh v0.4.3
```

## 验证

- 后端全部回归通过；超时预算相关断言改为从配置派生（schema maximum、校验上限 3000/3001 边界、系统提示动态渲染）。
