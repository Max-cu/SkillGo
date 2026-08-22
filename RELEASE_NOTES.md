# SkillGo v0.2.0

本次预览版重点补齐私有化部署后的数据库升级、备份恢复和文件生命周期管理，让 SkillGo 从“可以运行”进一步走向“可以持续运维”。

## 主要更新

- 引入 Alembic 版本化数据库迁移，可安全接管已有 v0.1 数据库并无损升级。
- 新增部署预检，启动前检查配置、Docker GID、gVisor Runtime 和沙箱镜像。
- 新增 PostgreSQL、托管文件与本机配置的版本化备份、校验恢复和备份优先升级脚本。
- 对话附件、任务输入和生成产物统一保留 15 天，到期后由后台计划任务自动删除。
- 文件到期后仍保留任务状态、文件元数据、结果摘要和审计记录，下载接口会明确返回已过期状态。
- 新增管理员存储中心，动态展示服务器磁盘总量、已用、可用空间、SkillGo 文件与用户占用。
- 清理数据库无引用的孤儿文件，并保留安全缓冲时间，减少误删风险。

## 升级

生产环境建议先执行备份，再切换到 `v0.2.0`：

```bash
sudo SKILLGO_INSTALL_ROOT=/opt/skillgo \
  SKILLGO_DEPLOY_ENV=deploy/ecs.env \
  bash deploy/backup-skillgo.sh

sudo SKILLGO_INSTALL_ROOT=/opt/skillgo \
  SKILLGO_RELEASE_TAG=v0.2.0 \
  SKILLGO_DEPLOY_ENV=deploy/ecs.env \
  bash deploy/update-skillgo.sh
```

完整的首次部署、升级、验证和回滚说明见 [`deploy/README.md`](deploy/README.md)。

## 已知边界

这是 Pre-release。组织级多租户、SSO、域名级精细出站策略和集中式对象存储仍在规划中。处理敏感数据前，请完成组织自身的 TLS、密钥托管、限流、备份计划和定期恢复演练。

## 验证

- Backend: `139 passed`
- Frontend: TypeScript + Vite production build passed
- Database: v0.1 baseline adoption and Alembic upgrades passed
- Operations: Compose sandbox profile and deployment scripts validated

完整变更见 [`CHANGELOG.md`](CHANGELOG.md)。

