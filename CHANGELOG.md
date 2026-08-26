# Changelog

所有重要变更都记录在此文件中。版本号遵循 [Semantic Versioning](https://semver.org/)。

## [Unreleased]

## [0.2.3] - 2026-08-26

### Added

- 管理员可在版本审核时为沙箱 Skill 开启“运行联网”，并可在发布后随时关闭或重新开启；所有版本默认断网。
- 任务创建时保存实际联网状态及授权来源 Skill 版本，多 Skill 任务按任一授权版本开启联网，历史记录不受后续开关变化影响。
- 固定入口执行模式、产物 SHA-256 验证快照与验证失效机制，确定性 Skill 不再由模型临时选择脚本。
- 审核页、Skill 版本列表、任务运行页和工作台任务卡展示联网需求、当前授权及任务快照。

### Changed

- 将沙箱 Agent 循环、工具注册、固定执行和产物验证从 Worker 主流程拆分为独立模块。
- Skill 内容中的网络声明、URL和依赖文件仅作为审核提示，不再自动授予任务容器网络。
- pip/npm 依赖继续限定在一次性工作区，apt/apk 等系统包安装保持禁止。

### Security

- 只有管理员审核过的具体 Skill 版本能够获得运行联网权限，权限变更写入审计日志，模型和普通用户不能自行开启。
- 任务容器继续不接收数据库连接、JWT Secret、模型 API Key、Endpoint Key或 Docker Socket。
- 当前联网仍为不限制目标域名的 Docker bridge；管理员只应为可信 Skill 开启，域名白名单、出口代理与 SSRF 防护不在本版本范围内。

## [0.2.2] - 2026-08-24

### Fixed

- 普通对话上下文同时遵守消息数量与字符上限，最近单条超长消息也会安全截断，避免请求体失控。
- 删除参与多 Skill 编排的任意 Skill 时完整清理关联任务、运行记录和绑定关系，不再遗留失效引用。
- 部署时等待 API 健康后强制重建 Web/Nginx，避免后端容器地址变化后 `/health` 等代理路由返回 502。

### Changed

- 自动化部署同时验证首页与 API 健康路由，失败时输出 Web/API 日志并停止发布。
- 只有完整自检成功后才以原子方式记录实际部署的 Git 提交号，便于迁移、审计和故障定位。
- Tag 升级流程采用相同的数据库、API、Web 启动顺序与健康检查。

## [0.2.1] - 2026-08-22

### Changed

- README 聚焦当前已实现能力与实际运行边界，不再陈列未排期的功能路线图。
- GitHub Release 工作流默认发布正式版本，不再自动标记为 Pre-release。

## [0.2.0] - 2026-08-22

### Added

- Alembic 数据库迁移基线，兼容安全接管已有 v0.1 数据库。
- 部署前配置、Docker GID、gVisor Runtime 和沙箱镜像预检。
- PostgreSQL、托管文件和本机配置的版本化备份与校验恢复脚本。
- 指定 Release Tag 的备份优先升级脚本。
- 统一的 15 天托管文件生命周期：对话附件、任务输入与生成产物到期后自动释放，任务记录和结果摘要继续保留。
- 文件过期下载状态，以及带服务器磁盘可视化的管理员存储概览。
- 孤儿文件安全缓冲清理、系统审计事件和 v0.1 数据库的无损升级迁移。

## [0.1.0] - 2026-08-22

### Added

- 多用户账号、三级角色、管理员审核和唯一超级管理员。
- Skill 上传、结构校验、不可变版本、发布审核与社区可见性。
- 任务级 Docker Volume 与容器隔离，gVisor `runsc` 支持。
- 基于租约的 Worker、心跳、重试、取消和失效恢复。
- 多 Skill 编排、工具事件、上下文续话和附件处理。
- 可验证产物、完整性校验和用户隔离的下载。
- 同步与异步 Skill API Endpoint，独立 API Key 和幂等请求。
- React 工作台、Skill 社区、运行记录、用户管理和模型配置界面。
- Docker Compose 私有化部署、gVisor 安装与环境自检脚本。

### Security

- 密码使用 Argon2 哈希，管理操作和资源访问写入审计记录。
- 任务容器默认非 root、只读根文件系统、去除 Linux Capabilities 并限制 CPU、内存和 PID。
- 本机密钥、运行数据、用户文件和备份默认被版本库忽略。

[Unreleased]: https://github.com/Max-cu/SkillGo/compare/v0.2.3...HEAD
[0.2.3]: https://github.com/Max-cu/SkillGo/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/Max-cu/SkillGo/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/Max-cu/SkillGo/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/Max-cu/SkillGo/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Max-cu/SkillGo/releases/tag/v0.1.0

