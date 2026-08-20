# SkillGo Phase 0：OpenSandbox + gVisor PoC

这个目录只验证沙箱底座，不包含用户、Skill 管理、队列等业务代码。

验证目标：

1. OpenSandbox 生命周期、命令和文件 API；
2. 固定入口 Skill 与 JSON Contract；
3. 默认拒绝网络；
4. CPU/内存/PID 限制；
5. 命令超时、Sandbox TTL 和强制清理；
6. Linux 上所有 Skill Sandbox 强制使用 gVisor `runsc`；
7. 容器没有特权、Host Network、Host PID、Docker Socket 或 root 用户。

## 固定版本

版本集中记录在 [versions.env](versions.env)：

- OpenSandbox Server `0.2.2`
- OpenSandbox Python SDK `0.1.14`
- execd `1.0.21`
- egress `1.1.4`

PoC 不使用 `latest`。

## 目录

```text
poc/
├── compose.yaml
├── opensandbox.local.toml
├── opensandbox.gvisor.toml
├── versions.env
├── runner/
├── sample-skill/
├── scripts/
└── results/
```

## 本机功能联调（Docker Desktop）

Windows Docker Desktop 没有注册 `runsc`，只能验证功能，不能作为安全验收结果。

PowerShell：

```powershell
cd C:\Users\yuan5\Desktop\skillgo\poc
$env:OPENSANDBOX_API_KEY = [Convert]::ToHexString((1..32 | ForEach-Object { Get-Random -Maximum 256 }))
$env:OPENSANDBOX_CONFIG_FILE = "./opensandbox.local.toml"
$env:POC_EXPECTED_RUNTIME = "runc"

docker compose --env-file versions.env build poc-skill poc-runner
docker compose --env-file versions.env up -d opensandbox-server
docker compose --env-file versions.env run --rm poc-runner --suite functional
```

安全参数检查需要两个终端。

终端 A：

```powershell
docker compose --env-file versions.env run --rm poc-runner --suite hold --hold-seconds 90
```

终端 B：

```powershell
python scripts\verify_container.py --expected-runtime runc
```

结束：

```powershell
docker compose --env-file versions.env down --remove-orphans
```

## 原生 Linux + gVisor 验收

要求：

- Ubuntu/Debian 类 Linux，建议内核 5.10+；
- Docker Engine；
- 可安装 `runsc`；
- 专用测试执行机，不与重要生产业务混部；
- 执行机可访问所需镜像仓库。

先做只读检查：

```bash
cd /path/to/skillgo/poc
sh scripts/preflight-linux.sh
```

如未安装 gVisor，在确认该主机允许修改 Docker Runtime 配置后执行：

```bash
sudo sh scripts/install-gvisor-ubuntu.sh
```

运行：

```bash
export OPENSANDBOX_API_KEY="$(openssl rand -hex 32)"
export OPENSANDBOX_CONFIG_FILE="./opensandbox.gvisor.toml"
export POC_EXPECTED_RUNTIME="runsc"

docker compose --env-file versions.env build poc-skill poc-runner
docker compose --env-file versions.env up -d opensandbox-server
docker compose --env-file versions.env run --rm poc-runner --suite functional
```

容器参数检查：

```bash
# 终端 A
docker compose --env-file versions.env run --rm poc-runner --suite hold --hold-seconds 90

# 终端 B
python3 scripts/verify_container.py --expected-runtime runsc
```

验收完成：

```bash
docker compose --env-file versions.env down --remove-orphans
```

## 结果

Runner 把机器可读结果写入：

```text
results/poc-results.json
results/active-sandbox.json
results/container-verification.json
```

PoC 允许本机 `runc` 联调存在 `SKIP`，但进入业务开发前，原生 Linux 的 gVisor 安全验收不得有关键项 `FAIL`。

## 安全说明

- `opensandbox-server` 为管理 Docker 而挂载 Docker Socket，因此该容器等价于宿主机 root 权限；
- Docker Socket 没有挂给 Runner 或 Skill；
- OpenSandbox API 必须设置 API Key；
- Runner 所在的 `control` 是内部网络；可信 OpenSandbox Server 额外连接
  `runtime-access`，用于回连 Docker backend 发布的沙箱控制端口；
- Skill Sandbox 使用默认拒绝的 `networkPolicy`；
- gVisor 配置没有使用 `--network=host`；
- `allowed_host_paths = []`，拒绝调用方挂载宿主机目录；
- OpenSandbox 0.2.2 的 Docker backend 实测 `ReadonlyRootfs=false`，且官方
  TOML 配置没有相应开关；进入生产前必须在适配层/上游补齐，并为 execd、
  Contract、输出和工作区提供显式可写挂载。
