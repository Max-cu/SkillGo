# SkillGo v0.4.0

本次发布的主线是**让长任务真正跑得完、断了能续**：新增 Skill 能力环境自动准备与任务持久化快照恢复，命令超时不再摧毁沙箱，并系统治理 Agent 上下文浪费（同一文件重复读取、工具输出被过度截断）。执行过程界面同步升级为 Codex 风格的扁平行动流，模型思考摘要与工具调用理由直接可见。

## 主要更新

**任务韧性与恢复**

- 持久化任务快照与围栏恢复：Worker 崩溃或重启后，任务从检查点继续而不是从头再来；工作区快照/恢复改为流式处理，大工作区不再 OOM。
- 命令超时只杀子进程、不毁沙箱：超时由容器内 GNU `timeout(1)` 执行（TERM 后 5 秒 KILL），沙箱与 `/workspace` 完整保留，Agent 按脚本自身的断点状态续跑；默认命令预算 900 秒。
- 沙箱在安全的 Agent 轮次边界按同镜像恢复；Worker 恢复扫描不再误伤健康任务。

**Skill 能力环境**

- 能力预检与经验证的 golden runtime；任务开始前自动准备并绑定隔离的能力环境。
- 通用 PyPI 依赖准备：任意 wheel 依赖自动解析间接依赖并锁定版本/哈希，仅使用预编译 wheel；任务执行中最多可发起 2 次依赖升级，环境重建后继续任务。

**Agent 上下文治理**

- 不可变引用架：Skill 静态资料与用户输入首次读取后钉住，后续分页读由内存直接服务，消除同一文件被重复读取数十次的浪费。
- `read_file` 请求的窗口始终完整返回；`command`/`run_python`/`run_verifier` 最近结果 52 KiB 以内全量保留，超限全文落盘并同时给出头部与尾部片段（报错栈通常在末尾）。
- 计划支持目录引用（聚合哈希绑定）；中途更新计划采用"中途信任、边界验证"，最终交付仍严格校验真实产物。

**界面与附件**

- 执行过程改为 Codex 风格扁平行动流：模型思考摘要、工具调用理由内联展示；执行计划与运行日志分区。
- 多附件 OCR/视觉分析并行（并发上限 3）；PDF 附件强制 OCR，未配置时在发送前明确阻断；附件上传代理超时放宽到 905 秒。
- 平台默认模型单轮预算不限时；Worker 扩展到 5 副本，Worker/环境 Worker/任务沙箱内存上调到 2GB/1GB/2GB。

## 升级

本版本包含 1 个数据库迁移（`20260911_0008_prepared_environments`，能力/预备环境表），服务启动时自动执行；建议升级前按惯例备份数据库。

新增可配置项：`SKILLGO_SANDBOX_TOOL_INLINE_BYTES`（最近命令类结果内联阈值，默认 53248 = 52 KiB）。沙箱运行时镜像需要重建以支持 `timeout(1)` 包装与流式快照；API、Worker、前端镜像均需更新。

Git 工作区部署可使用以下命令；源码压缩包部署需按现有安装方式更新源文件，不能直接运行依赖 Git checkout 的升级脚本。

```bash
sudo SKILLGO_INSTALL_ROOT=/opt/skillgo \
  SKILLGO_DEPLOY_ENV=deploy/ecs.env \
  bash deploy/backup-skillgo.sh

sudo SKILLGO_INSTALL_ROOT=/opt/skillgo \
  SKILLGO_DEPLOY_ENV=deploy/ecs.env \
  bash deploy/upgrade-skillgo.sh v0.4.0
```

## 验证与能力边界

- 后端 444 项回归通过；前端 TypeScript 检查与 Vite 正式构建通过。
- 生产环境实测：PPT 生成、图纸批量校验等长任务在快照恢复、超时续跑与引用架机制下成功完成，单任务工具操作 50+ 轮无重复读取、无超时摧毁沙箱。
- 引用架只钉住不可变静态内容（≤20KB 的 Skill 资料、用户输入）；更大的文件与任务中生成的可变文件仍走正常分页读取，不会占用模型上下文预算。

完整变更见 [CHANGELOG.md](CHANGELOG.md)。
