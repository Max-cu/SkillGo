# SkillGo v0.2.3

这是一次可信执行边界更新：完成沙箱执行内核模块化、固定入口与产物证据绑定，并将任务容器是否允许联网改为管理员对具体 Skill 版本的显式授权。

## 主要更新

- 管理员在审核沙箱 Skill 时决定是否开启“运行联网”，发布后仍可关闭或重新开启。
- 网络需求分析只提供审核提示，不会再自动为任务授予 Docker bridge 网络。
- 每个任务保存实际联网状态及授权来源，保证历史记录可追踪；多 Skill 任务由任一获授权版本开启联网。
- `sandbox_worker.py` 的 Agent 循环、工具注册、固定执行和产物验证已经拆分为独立模块。
- 固定入口 Skill 直接执行审核版本中的 argv；Agent 任务继续使用受限工具完成灵活流程。
- 验证操作绑定当时全部产物的 SHA-256，任何后续文件修改都会使旧验证失效。

## 升级

Git 工作区部署建议先执行备份，再升级到 `v0.2.3`：

```bash
sudo SKILLGO_INSTALL_ROOT=/opt/skillgo \
  SKILLGO_DEPLOY_ENV=deploy/ecs.env \
  bash deploy/backup-skillgo.sh

sudo SKILLGO_INSTALL_ROOT=/opt/skillgo \
  SKILLGO_DEPLOY_ENV=deploy/ecs.env \
  bash deploy/upgrade-skillgo.sh v0.2.3
```

完整的首次部署、升级、验证和回滚说明见 [`deploy/README.md`](deploy/README.md)。

## 部署提示

公网正式环境仍应配置 TLS 并妥善托管密钥。所有 Skill 版本默认断网；开启“运行联网”后任务使用 Docker bridge，当前不限制目标域名，也不提供出口代理或 SSRF 拦截，因此只应授权已经审核并信任的 Skill。

## 验证

- Backend: `155 passed`
- Frontend: TypeScript + Vite production build passed
- Operations: Compose sandbox profile configuration passed；Linux Shell checks由CI继续执行

完整变更见 [`CHANGELOG.md`](CHANGELOG.md)。

