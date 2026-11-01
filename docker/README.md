# docker

CITracker 的镜像与部署文件。镜像只包含 Python 运行时和服务代码；数据库、证书、PAT
都在运行时提供，代理也只在运行时按需注入（中国大陆用户）。

| 文件 | 用途 |
|---|---|
| `../Dockerfile` | 多阶段构建：builder 装依赖进 venv，runtime 只搬 venv |
| `entrypoint.sh` | 通用 runner：首次启动按序执行 `/docker-entrypoint-init.d/*.sh`，然后 exec 命令 |
| `init.d/` | 运营初始化脚本：`10-schema.sh` 建库建表，`20-pat.sh` 注入 / 轮换 PAT |
| `../docker-compose.yml` | `init` + `web` + `mcp` 三服务，数据库外置 |
| `../docker-compose.bundled.yml` | 叠加官方 `postgres:18` 的一体化形态 |
| `../docker-bake.hcl` | 多架构构建定义 |
| `stack.env.example` | compose 插值变量样例（secrets、数据库、PAT、legacy） |

更完整的说明（端口、环境变量全表、TLS、验证、代理）见仓库根目录 `README.md` 的“部署”一节。

## 架构

多架构，平台列表与 `python:3.15-slim` 官方一致：`linux/amd64`、`linux/arm64/v8`、
`linux/arm/v7`、`linux/arm/v5`、`linux/386`、`linux/ppc64le`、`linux/riscv64`、
`linux/s390x`。

```powershell
# 日常：两个生产平台
docker buildx bake --load

# 全量：八个平台，推送到仓库（一次构建产出多架构 manifest）
docker buildx bake citracker-all --push

# 指定单个平台
docker buildx bake --set citracker.platform=linux/amd64 --load
```

依赖来源是两个 sibling 仓库的分支（main 用于 mcpserver），与 CI 检出的 ref 相同；
核心库从 `release/v1.0.0.dev30` 安装（PR #127 已合入），Postgres 后端从
`release/v1.0.0.dev17` 安装（列约束改动已合入该发布线）。三者均已公开，构建无需 token。将来发布到 PyPI 后，把
`Dockerfile` 里的 git URL 换成版本钉死即可。

镜像不包含任何代理或镜像源配置；`images.yml` 在构建前会扫描 `Dockerfile` 与
`docker/entrypoint.sh`，`tests/app/test_image_hygiene.py` 也做同样的断言。

## 服务划分

```text
init   一次性：首次启动执行 init.d 脚本（建库建表、PAT），成功后退出
web    serve all   REST 8000（容器内）+ webhook 18080（对外）
mcp    serve mcp   MCP 8001（按需对外）
```

`web` / `mcp` 等 `init` 以 `service_completed_successfully` 结束后才启动。
`init` 失败（数据库不可达、建表失败、PAT 校验失败）时两个常驻服务不会启动。

## 形态一：外部 PostgreSQL（web + mcp）

```powershell
# 1. 配置（两个文件都在 docker/ 下，均被 git 忽略）
cp docker\stack.env.example docker\stack.env
#    填 CITRACKER_WEBHOOK_SECRET、CITRACKER_API_KEY、数据库、PAT
cp <证书目录>\fullchain.pem docker\certs\
cp <证书目录>\privkey.pem  docker\certs\

# 2. 启动
docker compose --env-file docker\stack.env up -d

# 3. 确认
curl https://<你的域名>:18080/readyz   # 就绪：含一次数据库往返
docker compose logs -f init web mcp
```

`stack.env` 里以 `CITRACKER_DB_*` 指向外部数据库；`CITRACKER_DB_HOST` 不设时 compose
默认用 `postgres`（只有一体化形态才有这个主机名），所以外置形态必须显式设置。

## 形态二：一体化 postgres:18

```powershell
docker compose -f docker-compose.yml -f docker-compose.bundled.yml `
  --env-file docker\stack.env up -d
```

overlay 只新增 `postgres:18`，并强制 `init` / `web` / `mcp` 使用 `postgres:5432`，
`stack.env` 中的外部数据库地址不会误生效。数据在命名卷 `pgdata`；`down` 保留，
`down -v` 删除。

## 首次启动初始化（init.d）

镜像里只有通用 runner，业务初始化是 `./docker/init.d` 下的运营脚本，机制与 MySQL 镜像的
`/docker-entrypoint-initdb.d` 相同。`init` 服务把该目录只读挂载到
`/docker-entrypoint-init.d`，首次启动按文件名顺序执行：

| 脚本 | 作用 |
|---|---|
| `10-schema.sh` | `rhosocial-github-ci-tracker-db init`：库不存在则先建库，再建全部表（幂等） |
| `20-pat.sh` | 配置了 PAT 时执行 `credential pat ensure`：不存在则新增，存在则原地轮换 |

“首次”按数据库状态判定，不看容器文件系统：runner 先执行
`rhosocial-github-ci-tracker-db is-initialized`，全部 `ct_*` 表都存在就跳过脚本，否则执行。
因此容器重建、镜像升级都不会重复初始化。`web` / `mcp` 显式设置
`CITRACKER_INITDB=skip`，不会与 `init` 抢着建表或轮换同一个凭据。

`20-pat.sh` 从环境读取以下变量（`docker/stack.env`）：

```dotenv
CITRACKER_BOOTSTRAP_PAT=github_pat_xxx
CITRACKER_BOOTSTRAP_PAT_NAME=work
CITRACKER_BOOTSTRAP_PROBE_REPO=owner/repo
```

`credential pat ensure` 会先在 GitHub 校验令牌，再写库：校验失败则 `init` 失败，坏令牌不会
入库；`--probe-repo` 额外测量该仓库的 Actions 写权限。令牌需要的权限：`Actions: Read and
write`、`Metadata: Read`；需要自动创建 webhook 时再加 `Administration: Read and write`。

不想让令牌出现在 `docker inspect` 时用文件形式：把令牌放进一个**目录**，把该目录只读挂载进
`init`，并设置 `CITRACKER_BOOTSTRAP_PAT_FILE=/run/secrets/citracker-pat`。脚本用
`--token-file` / `--token-stdin` 传递，令牌不会出现在进程参数里。

> **挂目录，不要挂单个文件。** Docker Desktop 的 Hyper-V 后端上，单文件 bind mount 会让容器
> 在宿主机路径不可达时变成无法删除的状态：`docker rm -f`、`docker compose down`、
> `docker start` 全部阻塞，只能重启引擎。目录挂载没有这个失败模式：
>
> ```yaml
> volumes:
>   - ./docker/secrets:/run/secrets:ro     # 目录
> ```

### 重跑与自定义

首次启动之后脚本不会自动再跑。改过脚本、或首次启动时还没配 PAT，之后补上令牌时：

```powershell
docker compose run --rm -e CITRACKER_INITDB=force init
```

也可以在 `docker/init.d/` 里加自己的 `*.sh`（按文件名排序，任一失败即停止）。这些脚本是部署
数据，只读挂载，不会被烘进镜像。

手工方式（不使用 init.d 时）：

```powershell
docker compose exec web rhosocial-github-ci-tracker credential pat ensure `
  --name work --token <PAT> --probe-repo owner/repo
docker compose exec web rhosocial-github-ci-tracker credential pat show `
  --name work --probe-repo owner/repo
```

## legacy / 手工模式

```dotenv
CITRACKER_INITDB=skip
```

`init` 不执行任何脚本，`web` / `mcp` 也不建表、不碰 PAT。`CITRACKER_LEGACY=1` 是等价写法。

legacy 模式下自行执行：

```powershell
docker compose run --rm web rhosocial-github-ci-tracker-db init
docker compose exec web rhosocial-github-ci-tracker credential pat add --name work --token <PAT>
```

## 清理

```powershell
docker compose exec web rhosocial-github-ci-tracker-db is-initialized   # 0=已初始化
docker compose exec web rhosocial-github-ci-tracker-db stats
docker compose exec web rhosocial-github-ci-tracker-db cleanup --runs-before 90d --logs-before 365d
docker compose exec web rhosocial-github-ci-tracker-db reset --yes    # 删表重建
docker compose exec web rhosocial-github-ci-tracker-db drop --yes     # 删整个库
docker compose -f docker-compose.yml -f docker-compose.bundled.yml down -v
```

`reset` 会删掉全部表（包括凭据行）。之后要恢复自动注入：

```powershell
docker compose run --rm -e CITRACKER_INITDB=force init
```

## 环境变量

完整列表见根目录 `README.md`。最常用的：

| 变量 | 默认 | 说明 |
|---|---|---|
| `CITRACKER_DB_*` | `postgres:5432` / `citracker` | 数据库坐标；外置形态必须覆盖 |
| `CITRACKER_API_KEY` | 空 | REST / MCP 的 Bearer；公网必须设置 |
| `CITRACKER_REST_HOST/PORT` | `0.0.0.0:8000` | REST 监听，容器内 |
| `CITRACKER_MCP_HOST/PORT` | `0.0.0.0:8001` | MCP 监听 |
| `CITRACKER_WEBHOOK_HOST/PORT` | `0.0.0.0:18080` | webhook 监听，**固定 18080** |
| `CITRACKER_WEBHOOK_SECRET` | 空 | HMAC 密钥，与仓库 webhook 配置一致 |
| `CITRACKER_WEBHOOK_SSL_CERTFILE/KEYFILE` | `/certs/*.pem` | webhook 的 fullchain 与私钥；显式置空可走前置反代 |
| `CITRACKER_PUBLIC_BASE_URL` | 空 | MCP action URL 的绝对前缀 |
| `CITRACKER_BOOTSTRAP_PAT[_FILE]` | 空 | 首次初始化注入的 PAT（由 `20-pat.sh` 读取） |
| `CITRACKER_BOOTSTRAP_PAT_NAME` | `default` | 凭据别名 |
| `CITRACKER_BOOTSTRAP_PROBE_REPO` | 空 | 验证 Actions 写权限的仓库 |
| `CITRACKER_INITDB` | `auto` | `auto` 首次启动执行 init.d / `force` 强制执行 / `skip` 关闭 |
| `CITRACKER_INITDB_DIR` | `/docker-entrypoint-init.d` | init.d 脚本目录（compose 只读挂载） |
| `CITRACKER_LEGACY` | `0` | `1` 等价于 `CITRACKER_INITDB=skip` |
| `CITRACKER_LOG_LEVEL` | `INFO` | 日志级别；一律走 stdout |

## 备注

- **证书不进镜像**：只以 `-v` 只读挂载，续期后替换宿主机文件并重启 `web`。
- **日志不持久化**：uvicorn 访问日志与应用日志都走 stdout/stderr。
- **compose 与已在跑的实例并存**：`CITRACKER_WEBHOOK_PORT_HOST` / `CITRACKER_MCP_PORT_HOST`
  可改宿主机端口，容器内端口不变。
- GitHub webhook 要求 https 回调：没有证书时监听器返回明文 HTTP，GitHub 会拒绝注册。
- **宿主端口可能静默失效（WSL 镜像网络）**：`~/.wslconfig` 为 `networkingMode=mirrored` 时，
  Docker 发布的端口由 WSL 侧持有，宿主机不一定拿得到监听。表现为 `127.0.0.1` 通、局域网 IP
  一律超时。绕法是让 Windows 自己持有一个 Docker **没有**发布的端口，用
  `scripts/tcp_relay.py --port 18444 --target 127.0.0.1:18080` 转发；证书只看域名不看端口。
- **REST 的 8000 不对外发布**：本机探测走 `scripts/rest_check.ps1`（打 18080，同一个 FastAPI 应用）。
- **代理只在运行时**：中国大陆部署自行注入 `HTTP(S)_PROXY`；Docker Desktop 注入的
  `127.0.0.1:7897` 在容器里指向容器自身，需要覆盖为 `host.docker.internal:7897`，并把数据库
  主机加入 `NO_PROXY`。
