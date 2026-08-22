# SkillGo v0.2.1

SkillGo 的首个正式 Release，建立了从 Skill 治理、任务级独立沙箱执行、结果交付到外部 API 接入的完整闭环，并补齐私有化部署所需的数据库升级、备份恢复和文件生命周期管理。

## 主要更新

- 引入 Alembic 版本化数据库迁移，可安全接管已有 v0.1 数据库并无损升级。
- 新增部署预检，启动前检查配置、Docker GID、gVisor Runtime 和沙箱镜像。
- 新增 PostgreSQL、托管文件与本机配置的版本化备份、校验恢复和备份优先升级脚本。
- 对话附件、任务输入和生成产物统一保留 15 天，到期后由后台计划任务自动删除。
- 文件到期后仍保留任务状态、文件元数据、结果摘要和审计记录，下载接口会明确返回已过期状态。
- 新增管理员存储中心，动态展示服务器磁盘总量、已用、可用空间、SkillGo 文件与用户占用。
- 清理数据库无引用的孤儿文件，并保留安全缓冲时间，减少误删风险。

## 升级

生产环境建议先执行备份，再切换到 `v0.2.1`：

```bash
sudo SKILLGO_INSTALL_ROOT=/opt/skillgo \
  SKILLGO_DEPLOY_ENV=deploy/ecs.env \
  bash deploy/backup-skillgo.sh

sudo SKILLGO_INSTALL_ROOT=/opt/skillgo \
  SKILLGO_RELEASE_TAG=v0.2.1 \
  SKILLGO_DEPLOY_ENV=deploy/ecs.env \
  bash deploy/update-skillgo.sh
```

完整的首次部署、升级、验证和回滚说明见 [`deploy/README.md`](deploy/README.md)。

## 部署提示

生产环境请配置 TLS、妥善托管密钥、设置访问限流，并建立经过恢复验证的备份计划。任务默认断网；需要联网的 Skill 按任务启用 Docker bridge 网络，不提供域名级出口白名单。

## 验证

- Backend: `139 passed`
- Frontend: TypeScript + Vite production build passed
- Database: v0.1 baseline adoption and Alembic upgrades passed
- Operations: Compose sandbox profile and deployment scripts validated

完整变更见 [`CHANGELOG.md`](CHANGELOG.md)。

