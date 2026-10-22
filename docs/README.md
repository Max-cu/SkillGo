# SkillGo 文档索引

## 当前文档

- [Skill 包说明](skill-packages.md)：包结构、扩展声明、上传限制与示例。
- [执行环境与隔离](execution-and-isolation.md)：Agent、环境准备、验证、权限、恢复与数据保留。

- [开发与部署](development.md)：本机开发、模型配置、测试、Compose 和沙箱 Worker。
- [产品与技术架构](PRODUCT_ARCHITECTURE.md)：产品边界、执行分层、隔离模型和演进方向。
- [Agent 内核](agent-kernel.md)：计划、工具、上下文、验证和产物门禁。
- [工作流 API](workflow-api.md)：异步 Endpoint、幂等、状态查询和产物下载。
- [沿用 Skill 既定流程](skill-flow-policy.md)：版本开关、单 Skill 免强制规划和 API 继承规则。
- [最终工作流自动交付](workflow-auto-delivery.md)：版本白名单、导出证据、文件一致性与回退条件。
- [上下文观测](context-observability.md)：可选统计、记录范围和估算值边界。
- [持久化快照](durable-task-checkpoints.md)：可选中断恢复及其限制。
- [技术设计](technical-design.md)：数据模型、控制面、执行面和安全约束的详细设计。

## 历史规划材料

`phase0-status.md`、`phase1-status.md`、`phase2-status.md`、`product-requirements-v1.md` 和 `business-optimization-roadmap.md` 是项目演进过程中的阶段快照。它们用于解释设计背景，不代表当前运行状态；出现冲突时，以代码、自动化测试和上述当前文档为准。
