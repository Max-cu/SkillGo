# Changelog

所有重要变更都记录在此文件中。版本号遵循 [Semantic Versioning](https://semver.org/)。

## [Unreleased]

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

[0.1.0]: https://github.com/Max-cu/SkillGo/releases/tag/v0.1.0

