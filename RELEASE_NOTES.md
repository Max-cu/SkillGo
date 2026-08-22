# SkillGo v0.1.0

SkillGo 的首个公开预览版，建立了从 Skill 治理、独立沙箱执行到结果交付和 API 接入的完整主链路。

## 核心能力

- 成员、管理员与唯一超级管理员，并对用户资源做所有权校验。
- 上传、校验、版本化、审核和发布 Skill。
- 每次沙箱执行创建独立 Docker Volume 与容器，Linux 可使用 gVisor `runsc`。
- 任务租约、心跳、重试、取消、执行事件与审计记录。
- 产物大小、SHA-256 和文件结构验证，只交付真实生成的文件。
- 将已发布的固定 Skill 版本封装为同步或异步 API Endpoint。
- React 工作台、Skill 社区、运行记录、API 接入和管理页面。

## 部署

- 基础模式：Docker Compose 启动 Web、API 和 PostgreSQL。
- 完整模式：Linux + Docker Engine + gVisor + Sandbox Worker。
- 完整的从零部署、验证、HTTPS、更新和回滚说明见 [`deploy/README.md`](deploy/README.md)。

## 已知边界

这是 Pre-release。组织级多租户、SSO、正式数据库迁移体系和域名级精细出站策略仍在规划中。处理敏感数据前，请完成组织自身的 TLS、密钥托管、备份、限流和宿主权限加固。

## 验证

- Backend: `133 passed`
- Frontend: TypeScript + Vite production build passed

完整变更见 [`CHANGELOG.md`](CHANGELOG.md)。

