# SkillGo v0.2.2

这是一次稳定性修复版本，重点收紧普通对话上下文、完善多 Skill 删除清理，并让自动化部署可以可靠识别 Nginx/API 失联，而不是在首页仍可访问时误判发布成功。

## 主要更新

- 普通对话历史同时受消息数和字符数限制，超长的最近消息会保留末尾并标记截断。
- 删除多 Skill 任务中的任意参与 Skill 时，完整清理关联任务、运行记录、文件元数据和绑定。
- 部署流程先等待 API 健康，再强制重建 Web/Nginx，消除容器地址变化造成的代理 502。
- 发布成功必须同时通过首页与 `/health` 检查；失败时自动输出相关容器日志。
- 完整验证通过后才记录实际 Git 提交号，版本文件以 `0600` 权限原子更新。

## 升级

Git 工作区部署建议先执行备份，再升级到 `v0.2.2`：

```bash
sudo SKILLGO_INSTALL_ROOT=/opt/skillgo \
  SKILLGO_DEPLOY_ENV=deploy/ecs.env \
  bash deploy/backup-skillgo.sh

sudo SKILLGO_INSTALL_ROOT=/opt/skillgo \
  SKILLGO_DEPLOY_ENV=deploy/ecs.env \
  bash deploy/upgrade-skillgo.sh v0.2.2
```

完整的首次部署、升级、验证和回滚说明见 [`deploy/README.md`](deploy/README.md)。

## 部署提示

公网正式环境仍应配置 TLS 并妥善托管密钥。任务默认断网；需要联网的 Skill 按任务启用 Docker bridge 网络，不提供域名级出口白名单。

## 验证

- Backend: `142 passed`
- Frontend: TypeScript + Vite production build passed
- Operations: Compose sandbox profile、部署脚本语法与部署辅助函数测试通过

完整变更见 [`CHANGELOG.md`](CHANGELOG.md)。

