# 环境准备第二阶段（候选实现，尚未部署）

## 已实现的执行路径

上传版本时解析可选 capabilities 声明、SKILL.md 中的目录线索，以及有限大小 Python 源文件的静态 import。平台仅将这些结果映射到受控能力目录，不执行 Skill 的脚本，也不将 requirements.txt 当作安装授权。此版本的分析器是确定性规则与静态分析，不是完整的 LLM 语义分析；不保证发现运行时动态生成代码的一切依赖。

每个版本独立绑定环境摘要。环境状态为 queued → building → probing → ready/failed，管理员可 revoke。发布必须 ready；失败不会删除上传版本或改变旧发布版本。相同规格通过数据库主键去重；基础镜像/架构、探针摘要、平台扩展目录摘要、锁文件、构建策略和能力集合共同确定缓存键。

基础能力命中固定镜像时只验证并记录，不重新下载。扩展只支持平台登记的 wheel；第一个扩展是 image.qr（qrcode 8.2），用于验证通用构建链路，不代表平台已支持任意 PyPI 包。锁定下载地址和 SHA256 来自 https://pypi.org/pypi/qrcode/8.2/json 。新增能力须由平台维护者更新依赖闭包、功能探针和锁定文件，并完成验收。

## 数据和执行边界

- 可信环境控制器单独运行，持有数据库和 Docker 管理权限，不挂载任务存储卷，不读取 Skill ZIP 或用户附件。
- 下载容器只收到平台登记的 URL/版本/哈希，使用固定 Python 下载程序，仅接受 files.pythonhosted.org 的 HTTPS URL，禁止重定向与代理继承。下载器不执行下载内容、不挂任何宿主目录或 Docker Socket。当前不是基于网络代理的强制全网出口白名单，不宣传为通用联网任意代码容器。
- 安装容器使用 gVisor、断网、丢弃 capabilities；其自身可写容器层用于启动前注入平台材料，无任何宿主/用户卷。pip 只用本地 wheel，--no-deps、--require-hashes、--only-binary，目标为临时扩展目录。pip check 检查依赖闭包，拒绝覆盖底座文件及符号链接。
- 安装结果经过有界归档检查，镜像构建只有 FROM/COPY/USER/WORKDIR/CMD，无 RUN、无联网安装。随后新建非 root、断网、只读 gVisor 容器运行平台功能探针。仅探针通过才保存镜像 ID 并标记 ready。
- Builder 从不接收用户身份、Agent reason、文档路径或业务数据。任务仍执行原有 Artifact 验证。

## 调度与恢复

任务将组合后的环境摘要写入自己的 memory。环境未完成时留在队列，Worker 不申请执行租约、不创建用户沙箱、不消耗模型调用；等待环境的任务不阻塞后续其他就绪任务。默认最多等待 3600 秒，超时进入明确失败路径，用户可以取消队列中的任务。

多 Skill 必须使用相同底座、构建策略及兼容锁文件，不能静默升级既有依赖。任务实际使用绑定的不可变镜像 ID；镜像丢失时显式失败并允许重新准备，不回退到可变标签。关闭新环境准备功能也不会绕过既有绑定。

构建固定总预算默认 900 秒；独立 attempt 与 lease 限制过期进程更新结果。崩溃的构建标记失败，重试由作者主动发起，过期构建容器由环境 Worker 回收。管理员可通过 POST /api/v1/admin/environments/{digest}/revoke 停用环境；已在执行中的任务不会因此被强行终止。

运行中 request_capability、任务中途切换环境仍属于第三阶段，本次不实现。缓存镜像当前不自动删除，避免删掉仍被版本引用的镜像；长期缓存配额与回收需在后续运维策略中补齐。

可重复验收脚本：`backend/scripts/smoke_environment.py --base-image sha256:<ID>`（从 backend 工作目录运行，设置 `PYTHONPATH=.`）。脚本只用合成内容，成功后删除临时扩展镜像；应在独立测试控制器运行，避免与正式环境回收器并行。当前缓存针对单一 Docker 主机，不提供跨主机镜像分发。

## 配置与上线步骤

本次默认关闭功能，不修改线上配置。上线需同时部署后端、前端及迁移 20260911_0008，然后启用独立环境控制器。

```dotenv
SKILLGO_ENVIRONMENT_PREPARATION_ENABLED=true
# 必须填写目标 Docker 主机已有的不可变镜像 ID，不接受可变标签。
SKILLGO_ENVIRONMENT_BASE_IMAGE=sha256:<64位镜像ID>
SKILLGO_ENVIRONMENT_BUILD_SECONDS=900
SKILLGO_ENVIRONMENT_QUEUE_WAIT_SECONDS=3600
```

api、worker 和 environment-worker 的功能开关与基础镜像配置必须一致。environment-worker 的 compose profile 为 environments；它只需要数据库和 Docker Socket，不配置模型密钥、不挂载 skillgo-storage。

```bash
docker compose --env-file .env --env-file deploy/ecs.env --profile sandbox --profile environments up -d --build api worker environment-worker web
```

上线前先确认任务空闲、备份数据库并保留旧镜像；先迁移和启动环境控制器，再开放 API 的新上传。回滚时可保留新增表和已有绑定，勿删除缓存数据。旧代码不认识环境绑定，若回滚到旧 Worker，应停止接收依赖扩展环境的任务。

## 验收记录

2026-09-14：独立服务器合成验收通过：固定 wheel 下载 → gVisor 断网安装 → 扩展文件组装 → 全新非 root gVisor 功能探针。image.qr 的 qrcode 8.2 可用，PDF、Office、中文字体等基础能力保留。临时验收镜像验证后删除，线上镜像/服务未切换。

实现中发现并修正两处平台差异：运行中 gVisor 临时目录不能依赖 Docker 归档接口导出；直接 commit 安装容器未保留该运行配置下的包写入。因此实际实现通过受控日志字节导出扩展，并组装只有 COPY 的镜像，再验证新实例。

最终补充验收：平台 DockerSandbox 实际使用准备镜像，完成合成输入挂载及 preflight_environment，镜像身份一致，image.qr 与全部底座能力可用；验收后删除临时任务沙箱、工作卷和扩展镜像。

本地验证：后端全量 288 项通过；随后环境/沙箱/任务相关 40 项通过（包含新增边界测试，与全量有重叠，不相加）；前端生产构建通过。功能仍默认关闭，尚未提交或部署。此前图片缓存优化属于独立待办，不是本阶段环境准备的依赖。
