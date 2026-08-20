# Linux 部署工具

完整的生产式沙箱 Worker 需要 Linux、Docker Engine 和 gVisor `runsc`。建议流程：

1. 在目标机安装 Docker；以 root 运行 `bootstrap-ecs.sh` 安装并验证 gVisor。
2. 将根目录 `.env.example` 复制为 `.env`，写入强随机密钥和模型配置。
3. 将 `deploy/ecs.env.example` 复制为 `deploy/ecs.env`，设置公开域名、端口、Docker GID 和任务资源上限。
4. 在项目根目录运行：

```bash
docker compose --env-file .env --env-file deploy/ecs.env --profile build-only build sandbox-runtime
docker compose --env-file .env --env-file deploy/ecs.env --profile sandbox up -d --build
```

`install-skillgo.sh` 兼容历史上的压缩包迁移流程：只有 `.deploy/skillgo.dump` 和 `.deploy/storage.tar.gz` 同时存在时才恢复备份；普通全新部署不会尝试恢复数据。

其余脚本用于模型连通性、沙箱自检、Worker 端到端验证和任务诊断。安装与总体验证脚本默认项目位于 `/opt/skillgo`，可通过 `SKILLGO_INSTALL_ROOT` 修改。运行诊断脚本前不要把生产密钥或用户文档复制到仓库。
