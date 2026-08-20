# SkillGo 产品需求规格 V1

> 版本：v0.1
> 日期：2026-08-03
> 产品定位：可私有化部署的 Skill 工作流平台与 Skill 社区

## 1. 产品定义

SkillGo 允许用户上传、版本化、运行、分享、下载和安装 Skill，并把一个已发布 Skill 的完整执行流程部署成稳定 API。

平台的核心不是沙箱，而是以下四个能力：

1. **Skill Registry**：管理 Skill 包、版本、依赖、文档和发布状态；
2. **Skill Community**：发现、搜索、分享、下载、收藏、评分和审核 Skill；
3. **Workflow Runtime**：按 `SKILL.md`、Manifest 和平台工具执行完整流程；
4. **Skill-as-an-API**：将已发布版本部署为 Endpoint，提供同步、异步、SSE 和 Webhook 调用。

代码型步骤需要隔离执行，但沙箱作为 Runtime 插件存在，不主导产品结构。

## 2. 用户与角色

V1 采用三级平台角色，后端权限以 permission 校验实现，不只依赖前端隐藏按钮。

### 2.1 超级管理员 `super_admin`

- 管理平台级配置、注册开关、存储、模型和全局限额；
- 创建、停用和授权管理员；
- 查看和管理所有用户、Skill、版本、Endpoint 和 Run；
- 处理高风险审核、举报、封禁、下架和强制撤回；
- 查看全局审计、运行指标和安全事件；
- 不默认读取用户永久密钥明文；敏感操作必须二次确认并记录审计。

### 2.2 管理员 `admin`

- 管理普通用户，但不能创建或撤销超级管理员；
- 审核公开 Skill 及新版本；
- 管理分类、标签、推荐位、举报和社区内容；
- 查看平台运行状态和授权范围内的 Run 元数据；
- 可以暂停、下架或禁用违规 Skill；
- 不能修改平台主密钥、系统级存储和超级管理员权限。

### 2.3 普通用户 `user`

- 管理个人资料、访问令牌和凭据绑定；
- 上传、编辑、测试和删除自己的草稿 Skill；
- 提交 Skill 或版本审核；
- 发布私有、链接分享或公开 Skill；
- 浏览、搜索、收藏、下载、安装和派生允许使用的 Skill；
- 运行有权限的 Skill，创建 Endpoint 并调用自己的 API；
- 查看自己的 Run、步骤、日志、产物和用量。

### 2.4 权限矩阵

| 能力 | 超级管理员 | 管理员 | 普通用户 |
|---|---:|---:|---:|
| 管理超级管理员 | 是 | 否 | 否 |
| 管理管理员 | 是 | 否 | 否 |
| 管理普通用户 | 是 | 是 | 否 |
| 修改平台安全配置 | 是 | 否 | 否 |
| 审核公开 Skill | 是 | 是 | 否 |
| 管理社区分类/推荐 | 是 | 是 | 否 |
| 上传 Skill | 是 | 是 | 是 |
| 发布个人私有 Skill | 是 | 是 | 是 |
| 提交公开审核 | 是 | 是 | 是 |
| 运行授权 Skill | 是 | 是 | 是 |
| 下载公开 Skill | 是 | 是 | 是 |
| 查看任意用户敏感数据 | 按需授权并审计 | 否 | 否 |

超级管理员也必须经过 Service 层权限检查。不得在代码中把 `super_admin` 等同于绕过租户边界、对象归属和审计。

## 3. Skill 包规范

```text
my-skill/
├── SKILL.md
├── skillgo.yaml             # SkillGo 扩展，可选
├── scripts/                 # 可选
├── references/              # 可选
├── assets/                  # 可选
├── tests/
│   └── cases.yaml
└── CHANGELOG.md             # 推荐
```

标准 `SKILL.md` 必须提供 `name` 和 `description` Frontmatter。SkillGo 扩展 Manifest 在需要对应能力时描述：

- 唯一名称、展示名称、说明、图标、分类和标签；
- 输入与输出 JSON Schema；
- Skill 类型：`instruction` 或 `code`；
- 需要的模型、工具、网络和凭据权限；
- 超时、最大步骤数和资源请求；
- 许可证、仓库地址、作者和兼容平台版本；
- 可否下载、可否派生和默认可见性。

`SkillVersion` 发布后不可修改。修复内容必须创建新版本；已被 Endpoint 锁定的历史版本保持可追溯。

## 4. Skill 社区

### 4.1 可见性

每个 Skill 支持四种可见性：

- `private`：仅作者和被明确授权者可见；
- `unlisted`：不出现在搜索和榜单中，持链接且有权限者可访问；
- `internal`：当前私有化实例内的登录用户可见；
- `public`：社区公开展示，可被搜索和下载。

私有化部署方可以全局关闭 `public`，仅保留内部社区。

### 4.2 社区能力

- 首页推荐、最新发布、趋势榜、下载榜和分类入口；
- 关键词、作者、分类、标签、运行时、许可证和更新时间筛选；
- Skill 详情、README、版本、更新记录、权限声明和安全状态；
- 收藏、点赞、评分、评论和举报；
- 下载原始包、安装到个人空间、复制/派生为新 Skill；
- 作者主页、已发布 Skill 和可信作者标识；
- 管理员推荐、置顶、下架和撤回版本；
- 下载量、安装量和成功运行次数统计，防重复刷量。

V1 评论可以延后，但收藏、下载、安装、举报和审核不可省略。

### 4.3 发布状态机

```text
draft
  -> validating
  -> scan_failed | ready
  -> submitted
  -> reviewing
  -> rejected | published
  -> deprecated
  -> yanked
```

- `draft` 可以被作者修改；
- `published` 版本不可变；
- `deprecated` 仍可被已锁定 Endpoint 使用，但不建议新安装；
- `yanked` 禁止新安装和新 Endpoint，是否终止现有 Endpoint 由安全级别决定；
- 公开发布和扩大权限的新版本必须重新审核；
- 仅修改社区介绍、不改变包哈希的元数据可以走轻量审核。

### 4.4 上传与审核

```text
上传 ZIP
 -> 安全解压与结构检查
 -> Manifest/Schema 校验
 -> 哈希、许可证和敏感信息扫描
 -> 脚本/依赖/SBOM 扫描
 -> 自动测试
 -> 管理员审核权限与说明
 -> 固化版本包和摘要
 -> 发布到社区
```

用户可以上传，但不等于立即获得执行权限：

- 指令型 Skill 通过基础扫描后可在作者空间测试；
- 包含脚本、依赖安装或网络访问的 Skill 使用更严格策略；
- 公开版本必须展示其申请的模型、工具、网络和凭据权限；
- 安装时用户再次确认权限；权限扩大时必须重新授权。

## 5. 工作流运行

### 5.1 两类 Runtime

#### 指令型 Skill

- Agent Runner 读取 `SKILL.md`；
- 使用平台 Model Gateway 与 Tool Gateway；
- 每个 Run 使用独立上下文和工作目录；
- 不允许直接执行 Shell/Python/Node；
- 作为 V1 首要支持类型。

#### 代码型 Skill

- Agent Runner 仅负责编排；
- 脚本步骤交给一次性隔离 Executor；
- 非 root、默认断网、资源限制、超时和运行后销毁；
- V1 可只允许管理员审核版本，gVisor 在公开不可信代码前作为上线门槛。

### 5.2 Run 状态机

```text
created -> queued -> preparing -> running
                         |          |
                         v          v
                       failed   waiting_input
                                    |
                                    v
                                  running

running -> succeeded | failed | cancelled | timed_out
```

每个 Run 包含不可变的 SkillVersion、输入快照、权限快照、模型/工具配置快照和发起者信息。重试生成新的 Attempt，不覆盖旧执行证据。

### 5.3 运行记录

- Run 总状态和耗时；
- 每个步骤的输入摘要、输出摘要、开始/结束时间和状态；
- 模型、Token、工具、外部 API 和资源用量；
- SSE 实时事件；
- 可下载产物；
- 错误码、可安全展示的错误信息和内部诊断 ID；
- 完整审计，但日志不得泄露密码、Token 和凭据正文。

## 6. Skill-as-an-API

已发布 SkillVersion 可以部署为 `Endpoint`。Endpoint 锁定版本、输入输出 Schema、凭据绑定、并发、超时、费用和访问策略。

### 6.1 统一调用 API

```http
POST /api/v1/endpoints/{endpoint_slug}/runs
Authorization: Bearer sk_live_xxx
Idempotency-Key: 3a8...
Content-Type: application/json
```

```json
{
  "input": {},
  "response_mode": "async",
  "callback_url": "https://client.example.com/webhooks/skillgo"
}
```

### 6.2 必要接口

- `POST /api/v1/endpoints/{slug}/runs`：创建执行；
- `GET /api/v1/runs/{run_id}`：查询状态和结果；
- `GET /api/v1/runs/{run_id}/events`：SSE 事件；
- `POST /api/v1/runs/{run_id}/cancel`：取消；
- `GET /api/v1/runs/{run_id}/artifacts`：列出产物；
- Webhook：完成、失败、超时和等待输入事件；
- 每个 Endpoint 根据 Schema 生成 OpenAPI 文档和调用示例。

支持：

- `sync`：仅用于短任务，超过网关限制自动转异步或返回 Run ID；
- `async`：默认模式；
- `stream`：SSE 返回步骤和模型输出事件；
- Webhook 使用签名、时间戳、事件 ID 和重放保护。

## 7. 网页信息架构

### 7.1 公共/社区页面

- 首页；
- Skill 市场；
- 分类与搜索结果；
- Skill 详情和版本详情；
- 作者主页；
- 登录、注册、找回密码；
- 平台文档和 API 文档。

### 7.2 用户工作台

- 概览；
- 我的 Skill；
- 上传/创建 Skill；
- 版本、测试、提交审核和发布；
- 已安装、收藏和下载记录；
- Run 列表、Run 详情、步骤、日志和产物；
- Endpoint 管理、在线调试和调用示例；
- API Key；
- 模型、工具和凭据连接；
- 个人资料和通知。

### 7.3 管理后台

- 用户和状态管理；
- Skill/版本审核队列；
- 分类、标签、推荐位；
- 举报、评论和内容治理；
- Run 与 Worker 运行监控；
- 配额和限流；
- 审计日志；
- 安全扫描结果。

### 7.4 超级管理后台

- 管理员与角色；
- 注册、社区和发布策略；
- 模型供应商与平台工具；
- 对象存储、队列、执行器和系统配置；
- 全局 API/运行限额；
- 系统健康、备份、升级和安全事件。

管理员和超级管理员可以共用一个后台应用，菜单与操作按 permission 动态显示，API 再次强制校验。

## 8. 核心数据模型

```text
User
Role / Permission / UserRole

Skill
SkillVersion
SkillCollaborator
SkillGrant
SkillCategory / SkillTag
SkillStatDaily
Favorite / Installation / DownloadEvent
Review / ReviewDecision / Report

Endpoint
EndpointApiKey
Run
RunAttempt
StepRun
RunEvent
Artifact

Credential
CredentialBinding
AuditEvent
Notification
```

关键关系：

```text
User 1 ── N Skill
Skill 1 ── N Immutable SkillVersion
SkillVersion 1 ── N Review
SkillVersion 1 ── N Installation
SkillVersion 1 ── N Endpoint
Endpoint 1 ── N Run
Run 1 ── N RunAttempt
RunAttempt 1 ── N StepRun / RunEvent / Artifact
```

所有下载、安装和执行都在服务端检查可见性、版本状态、对象归属和授权；不能只依赖社区页面是否展示。

## 9. 安全与治理原则

- 上传包按不可信文件处理，防 ZIP Slip、压缩炸弹、链接和特殊文件；
- 包和版本以 SHA-256 标识，发布版本不可替换；
- 下载响应强制 attachment，并设置安全 MIME；
- 公开 Skill 必须显示权限声明与安全扫描状态；
- Tool Gateway 才能获得真实凭据，Skill 只拿临时能力；
- Prompt 中的指令不能扩大 Manifest 权限；
- 用户不能指定底层容器运行参数、宿主挂载或控制面地址；
- API Key 只存哈希，支持 scope、过期、轮换和撤销；
- 管理操作、审核决定、强制下架和角色变化全部写审计日志；
- 社区下载包不代表平台背书，风险提示和举报入口必须明确。

## 10. V1 范围

### 必须完成

- 本地账号登录与三级角色；
- 完整网页框架和权限菜单；
- Skill ZIP 上传、校验、版本化和私有保存；
- 社区搜索、详情、收藏、下载和安装；
- 公开发布申请与管理员审核；
- 指令型 Skill Runner；
- Run 状态机、步骤、日志、产物；
- Endpoint、API Key、异步调用、查询和 SSE；
- 基础 Webhook；
- 审计和基础配额。

### 可以延后

- 评论和复杂评分防作弊；
- 组织/团队空间和组织管理员；
- 收费市场、分成和账单；
- 跨实例 Skill 联邦；
- 在线可视化拖拽编排；
- 用户任意依赖构建和未审核代码执行；
- Kubernetes、多地域和自动扩缩容。

## 11. 实施顺序

### Phase 1：产品骨架

- 前后端工程、数据库迁移和 Compose；
- 登录、三级 RBAC、导航和后台框架；
- Skill、SkillVersion、分类、标签和对象存储；
- 上传、Manifest 校验和私有 Skill 页面。

### Phase 2：社区与审核

- 市场、搜索、详情、作者、收藏、下载和安装；
- 发布状态机、审核后台、举报与下架；
- 版本哈希、扫描结果和审计。

### Phase 3：工作流与 API

- Agent Runner、Model Gateway、Tool Gateway；
- Run/Attempt/Step 状态机、日志、SSE 和产物；
- Endpoint、API Key、OpenAPI、Webhook 和在线调试。

### Phase 4：代码型 Skill

- Builder、依赖锁定、SBOM 和签名；
- 一次性 Sandbox Executor；
- gVisor、安全网络、只读根和资源验收；
- 仅在安全门槛通过后开放给普通用户。

## 12. V1 验收场景

1. 普通用户上传一个指令型 Skill，校验失败时能看到明确错误；
2. 用户创建私有版本并成功测试，其他用户无法枚举或下载；
3. 用户提交公开审核，管理员批准后出现在社区搜索中；
4. 另一用户查看权限声明、收藏、下载并安装该版本；
5. 用户把安装版本部署成 Endpoint，通过 API 创建 Run；
6. 网页和 API 均可查看步骤、日志、产物和最终结构化结果；
7. 重复 `Idempotency-Key` 不创建第二个 Run；
8. 无权限用户不能查看 Run、产物、凭据或私有 Skill；
9. 管理员可以下架违规版本，但不能修改超级管理员和平台主密钥；
10. 超级管理员的角色修改和强制操作都有不可抵赖审计；
11. 被撤销 API Key 立即无法创建新 Run；
12. 代码型 Skill 未通过平台安全策略时不能发布或执行。

## 13. 当前决策

- 产品名称暂用 `SkillGo`；
- 默认支持私有部署，社区既可配置为实例内部，也可开放公开内容；
- 普通用户可以上传 Skill，但发布、下载和执行权限彼此独立；
- V1 优先做指令型 Skill，代码型 Skill 使用现有 OpenSandbox PoC 作为后续插件；
- 对外采用统一 Run API，并为每个 Endpoint 自动生成文档，而不是为每个 Skill 手写后端接口；
- V1 先采用平台级三级角色，组织/团队角色后续增加。
