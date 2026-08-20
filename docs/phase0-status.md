# Phase 0 PoC 状态

> 更新：2026-07-24

## 结论

本机 OpenSandbox 功能 PoC 已完成：

- 功能回归：**6/6 通过**；
- 宿主机容器参数：收紧后验收 **14/15 通过**；
- 唯一失败项：`ReadonlyRootfs=false`；
- 当前机器只有 `runc`，因此这不是 gVisor 安全验收结论。

结论是：**OpenSandbox 可以作为控制面与生命周期底座继续推进，但进入多用户生产实现前，必须补只读根文件系统，并在原生 Linux 上完成 gVisor 复验。**

## 已完成

- 固定 OpenSandbox Server、SDK、execd、egress 和 Python 基础镜像版本；
- 创建 Docker Desktop 本机联调配置；
- 创建原生 Linux + gVisor 验收配置；
- 创建非 root、固定入口、JSON Contract 的示例 Skill；
- 创建生命周期、文件、网络、PID、内存、命令超时、TTL 自动测试 Runner；
- 创建宿主机 `docker inspect` 安全参数检查器；
- 创建 Linux 预检和 Ubuntu/Debian gVisor 安装脚本；
- Python、TOML、Docker Compose 静态校验通过；
- Sample Skill 与 Runner 镜像构建通过；
- 直接 Docker 隔离烟雾测试通过；
- OpenSandbox 本机端到端回归通过。

## 本机功能回归

结果文件：[poc-results.json](../poc/results/poc-results.json)

| 用例 | 结果 | 说明 |
|---|---:|---|
| 生命周期与固定 Skill | PASS | 创建、命令、文件 API、JSON Contract、结果读取均成功；UID/GID 为 10001 |
| 默认拒绝网络 | PASS | 公网、云元数据地址、私网测试地址均不可达 |
| 命令超时 | PASS | 2 秒超时的命令约 3.09 秒被终止，`exit=-1` |
| PID 限制 | PASS | 在生成 256 个子进程前被拒绝 |
| 内存限制 | PASS | 128 MiB 沙箱中申请 512 MiB 失败，`exit=-1` |
| Sandbox TTL | PASS | 60 秒后沙箱不可再查询 |

## 宿主机参数验收

结果文件：[container-verification.json](../poc/results/container-verification.json)

已通过：

- 运行时与预期一致：本机为 `runc`；
- 非 root：`10001:10001`；
- 非 privileged；
- 非 Host Network、Host PID、Host IPC；
- `CapDrop` 包含 `ALL`，且实测 `CapAdd` 为空；
- `no-new-privileges=true`；
- PID 上限 128；
- 内存上限 256 MiB；
- CPU 上限 500m；
- 没有 Docker Socket、危险 Host Bind 或 Host Device。

未通过：

- `ReadonlyRootfs=false`。

OpenSandbox 0.2.2 的官方 Docker 配置项没有只读根文件系统开关。后续需要在适配层或上游 Docker backend 中增加 `read_only=true`，并为以下位置提供显式、限额、可回收的可写挂载：

- `/opt/opensandbox`：execd 注入与运行；
- `/run/skill`：输入 Contract；
- `/output`：执行结果；
- `/workspace`：任务临时工作区；
- 必要时 `/tmp`：限额 tmpfs。

## 本机联调中修复的问题

1. OpenSandbox 服务端最初只连接 `internal` 控制网络，无法回连 Docker Desktop 发布的 egress sidecar 端口。现已增加仅服务端使用的 `runtime-access` 网络，Runner 仍留在内部控制网络。
2. 首次冷启动会拉取 egress/execd 镜像，30 秒客户端请求超时不足。Runner 控制面请求预算调整为 90 秒；命令级超时仍独立限制。
3. execd 文件上传协议把模式按八进制文本解释，SDK 的 `0o600` 会被序列化为十进制 `384`。PoC 按协议传 `600`。
4. 显式 `RunCommandOpts.uid/gid` 在当前组合中返回不完整退出状态。PoC 改为由镜像 `USER 10001:10001` 强制非 root，并通过输出与宿主机 Inspect 双重验证。

## 当前环境

- Docker Desktop Engine：29.4.3；
- Docker Desktop：4.73.1；
- 当前注册运行时：`runc`，没有 `runsc`；
- 本机只适合功能联调，不作为强隔离验收环境。

## 下一步准入条件

在开始多用户业务层之前，建议先满足：

1. 提供一台原生 Ubuntu/Debian Linux 专用测试机；
2. 安装并注册 gVisor `runsc`；
3. 使用 `opensandbox.gvisor.toml` 重跑 `functional` 与 `hold + verify_container.py`；
4. 实现只读根文件系统适配并让宿主机检查全绿；
5. 对并发沙箱做用户 A/B 文件、进程、网络与凭据串扰测试；
6. 再进入 API、调度器、任务队列、审计与多租户数据模型开发。

## 保留产物

- 本地镜像：`skillgo/poc-skill:local`、`skillgo/poc-runner:local`；
- OpenSandbox 固定版本镜像；
- 机器可读测试结果；
- Docker Compose、gVisor 配置和验收脚本。

PoC 服务与临时沙箱会在本轮结束时关闭；镜像和持久卷保留，后续可直接复测。
