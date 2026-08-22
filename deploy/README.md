# SkillGo 部署指南

本文档面向首次部署 SkillGo 的管理员。如果希望 Skill 真正在独立沙箱中运行，请使用 **Linux + Docker Engine + gVisor** 的完整部署方式。

## 1. 选择部署方式

| 方式 | 适合场景 | 启动服务 | 沙箱 Skill |
| --- | --- | --- | --- |
| 基础模式 | 本机试用、UI 预览、普通对话 | Web + API + PostgreSQL | 不可用 |
| 完整模式 | 私有化部署、文件处理、工具调用 | Web + API + PostgreSQL + Worker + gVisor | 可用 |

> 普通对话和 `instruction_only` 同步调用不创建任务容器。需要命令、脚本、附件或产物的 Skill 才会进入 Worker 和独立沙箱。

## 2. 完整部署前提

推荐使用独立的 Debian/Ubuntu Linux 主机。仓库内的 gVisor 安装脚本使用 `apt`；其他发行版请按 [gVisor 官方文档](https://gvisor.dev/docs/user_guide/install/) 安装 `runsc`。

- 建议至少 2 核 CPU、4 GB 内存、20 GB 可用磁盘。
- 需要 Docker Engine 和 Docker Compose v2。
- 服务器需能访问代码源、Docker 镜像源和 gVisor 软件源。
- 公网部署应在 SkillGo 前面配置 HTTPS 反向代理。
- 普通对话与 Agent 需要 OpenAI-compatible 模型服务。

## 3. 获取指定版本

生产或演示环境建议固定 Release Tag，不要直接跟随持续变化的 `main`。

```bash
git clone https://github.com/Max-cu/SkillGo.git
cd SkillGo
git checkout v0.1.0
```

也可从 [GitHub Releases](https://github.com/Max-cu/SkillGo/releases) 下载对应版本的源码包。

## 4. 安装 Docker 和 gVisor

1. 按 [Docker Engine 官方文档](https://docs.docker.com/engine/install/) 安装 Docker 和 Compose v2。
2. 在 Debian/Ubuntu 主机上执行：

```bash
sudo bash deploy/bootstrap-ecs.sh
```

该脚本会在系统没有 Swap 时创建 2 GB `/swapfile`，安装 `runsc`，向 Docker 注册 gVisor Runtime，重启 Docker，并用 `--runtime=runsc --network=none` 启动测试容器。

成功时最后会显示 `BOOTSTRAP_OK`。也可手动复查：

```bash
docker info --format '{{json .Runtimes}}'
docker run --rm --runtime=runsc --network=none hello-world
```

## 5. 创建本机配置

```bash
cp .env.example .env
cp deploy/ecs.env.example deploy/ecs.env
chmod 600 .env deploy/ecs.env
```

`.env` 和 `deploy/ecs.env` 已被 Git 忽略，不应提交到仓库。

### 5.1 必填密钥和初始管理员

用下面的命令生成三个不同的随机值：

```bash
openssl rand -hex 24
openssl rand -hex 32
openssl rand -hex 24
```

编辑 `.env`，至少替换：

```dotenv
POSTGRES_PASSWORD=<第一个随机值>
SKILLGO_JWT_SECRET=<第二个随机值>
SKILLGO_BOOTSTRAP_EMAIL=owner@example.com
SKILLGO_BOOTSTRAP_PASSWORD=<第三个随机值>
SKILLGO_BOOTSTRAP_NAME=SkillGo Owner
```

Bootstrap 账号是实例唯一的超级管理员。

### 5.2 模型配置

希望启动时就连接模型时，在 `.env` 中填写：

```dotenv
SKILLGO_MODEL_BASE_URL=https://your-model.example.com/v1
SKILLGO_MODEL_API_KEY=<your-private-key>
SKILLGO_MODEL_NAME=<model-name>
```

也可将这三项留空，启动后由超级管理员或管理员在“平台设置 → 模型”中配置。

### 5.3 地址、端口和 Docker GID

先查看 Docker Socket 的组 ID：

```bash
stat -c '%g' /var/run/docker.sock
```

将返回的数字写入 `deploy/ecs.env`：

```dotenv
SKILLGO_WEB_BIND=0.0.0.0
SKILLGO_WEB_PORT=80
SKILLGO_CORS_ORIGINS=https://skillgo.example.com
SKILLGO_DOCKER_GID=<上一步返回的数字>
```

- 直接用 IP 测试时，CORS 可写为 `http://<server-ip>`。
- 使用 HTTPS 时，必须写完整的 `https://...` Origin，不要带路径。
- GID 错误会导致 Worker 持续重启或报 `permission denied`。

## 6. 构建并启动

以拥有 Docker 权限的用户在项目根目录执行：

```bash
docker compose --env-file .env --env-file deploy/ecs.env --profile build-only build sandbox-runtime
docker compose --env-file .env --env-file deploy/ecs.env --profile sandbox up -d --build
```

第一条构建受控的 Skill 沙箱镜像；第二条启动数据库、API、Web 和 Worker。

```bash
docker compose --env-file .env --env-file deploy/ecs.env --profile sandbox ps
curl -fsS http://127.0.0.1/health
```

`curl` 应返回 `{"status":"healthy"}`。

## 7. 验证 gVisor 和 Worker

```bash
SKILLGO_INSTALL_ROOT="$PWD" bash deploy/verify-ecs.sh
```

自检会检查服务、Worker/API 日志、数据库，并真正用 `runsc` 启动非 root、只读、无网络的测试容器。关键输出：

```text
uid 10001
network_reachable False
VERIFY_ECS_OK
```

`network_reachable False` 是预期结果，表示自检沙箱的 `network=none` 已生效，不是部署失败。

## 8. 首次登录

1. 访问 `http://<server-ip>` 或配置好的 HTTPS 域名。
2. 使用 `.env` 中的 Bootstrap 邮箱和密码登录。
3. 在“平台设置 → 模型”中检查连接状态。
4. 上传一个需要脚本或附件的 Skill，执行并确认可下载通过验证的产物。

API 文档默认位于 `http://<server-ip>/api/docs`。

## 9. 基础模式（无沙箱）

只想查看界面、测试用户管理或普通对话时：

```bash
cp .env.example .env
# 编辑 .env，替换数据库密码、JWT Secret 和 Bootstrap 密码
docker compose up -d --build
```

默认访问 `http://127.0.0.1:8080`。此模式不启动 Worker，不能运行需要工具、脚本或文件产物的 Skill。

## 10. HTTPS 和公网入口

Compose 提供 HTTP 入口，不直接申请 TLS 证书。公网环境建议：

- 用 Caddy、Nginx 或云负载均衡器终止 HTTPS。
- 只对外开放 80/443；不要额外暴露 PostgreSQL。
- 将 CORS 配置为用户实际访问的 HTTPS Origin。
- 不要把 `.env`、数据库备份、用户文件或模型密钥放入仓库。

## 11. 更新、备份和回滚

更新前至少备份 PostgreSQL Volume `skillgo-postgres`、文件 Volume `skillgo-storage`、`.env` 和 `deploy/ecs.env`。

```bash
git fetch --tags
git checkout <new-version-tag>
docker compose --env-file .env --env-file deploy/ecs.env --profile build-only build sandbox-runtime
docker compose --env-file .env --env-file deploy/ecs.env --profile sandbox up -d --build
SKILLGO_INSTALL_ROOT="$PWD" bash deploy/verify-ecs.sh
```

回滚时检出上一个 Tag 并重建服务。如果 Release Notes 提到数结构不兼容，还必须恢复对应的数据库和文件备份。

`deploy/install-skillgo.sh` 保留给自动化升级和历史备份迁移。它默认项目位于 `/opt/skillgo`，可用 `SKILLGO_INSTALL_ROOT` 修改；只有 `.deploy/skillgo.dump` 和 `.deploy/storage.tar.gz` 同时存在时才会恢复备份。

## 12. 常见问题

| 现象 | 优先检查 |
| --- | --- |
| Worker 持续重启 | Docker GID 是否正确；Worker 是否有 Docker Socket 权限。 |
| 提示 `runsc` 不可用 | 执行 `docker info` 检查 Runtime，重新运行 `bootstrap-ecs.sh`。 |
| Skill 任务一直排队 | 是否用 `--profile sandbox` 启动 Worker，以及 Worker 开关是否为 `true`。 |
| 浏览器报 CORS | CORS Origin 是否与地址栏的协议、域名和端口完全一致。 |
| 页面正常但模型不回复 | 在管理界面测试模型连接，检查 API 到模型地址的网络和 TLS。 |
| 自检显示 `network_reachable False` | 这是 `network=none` 沙箱的正常结果。 |

查看日志：

```bash
docker compose --env-file .env --env-file deploy/ecs.env --profile sandbox logs --tail=200 api worker web db
```

仍无法定位时，请在 [GitHub Issues](https://github.com/Max-cu/SkillGo/issues) 提交脱敏后的环境信息、命令和日志。不要附上 `.env`、API Key 或用户文档。

