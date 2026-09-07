# SkillGo v0.3.0

本次发布集中升级任务拆解、Skill 编排与执行验证，并包含 v0.2.6 之后的 Skill 包导入兼容性和工作台交互修复。

## 主要更新

- 任务计划保存步骤依赖和输入输出文件哈希；文件变化后，相关步骤与下游阶段失效，支持重新规划。
- 专用验证器绑定实际产物和最新验证结果，失败写入会使旧验证失效，模型声明不能覆盖失败证据。
- 工作台提供可见的“自动选择 Skill”开关，默认关闭；开启后检索相关候选，零匹配时明确提示。
- 缺少必要信息时可向用户提问；回答持久化，任务携带已确认答案重新排队执行。
- 支持固定入口与 Agent 阶段混合运行；生成图片可调用已配置的视觉模型检查。
- 模型连接支持协议适配、推理强度、上下文与输出预算；执行上下文保留活动 Skill、原始要求及阶段证据。
- Skill 包导入兼容包装目录、唯一嵌套 SKILL.md、BOM 和部分缺失元数据，模型分析超时会回退到本地解析。

## 升级

本版本新增数据库迁移 `20260907_0007`，保存编排记忆与模型参数。升级前备份数据库、文件存储和配置，并同步更新 API、Worker 和前端。

Git 工作区部署可使用以下命令；源码压缩包部署需按现有安装方式更新源文件，不能直接运行依赖 Git checkout 的升级脚本。

```bash
sudo SKILLGO_INSTALL_ROOT=/opt/skillgo \
  SKILLGO_DEPLOY_ENV=deploy/ecs.env \
  bash deploy/backup-skillgo.sh

sudo SKILLGO_INSTALL_ROOT=/opt/skillgo \
  SKILLGO_DEPLOY_ENV=deploy/ecs.env \
  bash deploy/upgrade-skillgo.sh v0.3.0
```

数据库包含新字段与状态，回滚前应停止任务并按备份恢复与旧代码匹配的数据。完整操作见 [部署说明](deploy/README.md)。

## 验证与能力边界

- 后端 206 项回归通过；前端 TypeScript 检查与 Vite 正式构建通过。
- 服务器已验证 PostgreSQL 迁移、服务健康及真实 gVisor 沙箱自检。
- 真实业务通过率和模型效果尚待独立测评。验证器真实执行不等于其已覆盖全部业务要求。
- 自动召回目前使用词项相关性排序；回答澄清问题后使用新沙箱重跑，尚不恢复半成品工作区。

完整变更见 [CHANGELOG.md](CHANGELOG.md)，编排实现与后续测评范围见 [三批优化记录](docs/orchestration-upgrade-2026-09-07.md)。
