# Changelog

所有重要变更都记录在此文件中。版本号遵循 [Semantic Versioning](https://semver.org/)。

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

