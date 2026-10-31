# 安装

三种形态：本地 CLI、单容器、compose 双容器。


最短路径（外部已有 PostgreSQL，证书就绪）：

```bash
cp docker/stack.env.example docker/stack.env   # 填 DB、API key、webhook secret、PAT
mkdir -p docker/certs && cp <fullchain.pem> <privkey.pem> docker/certs/
docker compose --env-file docker/stack.env up -d
curl -k https://127.0.0.1:18080/readyz        # {"status":"ready",...}
```

下面按"镜像 → 端口与变量 → 部署形态 → 初始化 → 升级 → 探针与连接池 → 排障"展开。

### 镜像

```text
registry.cn-shanghai.aliyuncs.com/vistart_public/citracker:latest        # main 的最新提交
registry.cn-shanghai.aliyuncs.com/vistart_public/citracker:sha-<40 位提交号>  # 某一次构建
```

两个标签，含义不同：

| 标签 | 指向 | 什么时候动 |
|---|---|---|
| `latest` | **当前 `main` 构建出来的镜像** | 构建结束时 `main` 仍在这次提交上（或已越过、但没改动镜像输入）才移动；因此旧提交的构建晚完成不会覆盖新镜像 |
| `sha-<sha>` | 那一次构建的提交 | 每次构建都写，永不覆盖别的提交 |

排查"线上到底是哪份代码"、或者要固定版本部署时用 `sha-<sha>`（`CITRACKER_IMAGE` 直接指它）。
`workflow_dispatch` 在 `main` 上是显式重新发布（回滚到旧提交也走这条），会移动 `latest`；
在其它分支上只发布 `sha-<sha>`，不会污染 `latest`。

- 多架构：`linux/amd64`、`linux/arm64`。
- 镜像里只有 Python 运行时、服务代码和入口脚本；**不含数据库、证书、任何代理配置、pip 镜像或索引覆盖**。
  构建由 `images.yml` 在 GitHub runner 上用官方 PyPI 直接完成，`HTTP(S)_PROXY`、`PIP_INDEX_URL`、
  `mirrors.aliyun.com` 等都不会进入镜像。新增构建步骤时由 `tests/app/test_image_hygiene.py`
  和 `images.yml` 的守卫步骤拦截。
- 代理是**运行时**选择：全球用户不需要；中国大陆用户在自己的 compose / 环境里注入即可，见
  [运行时代理](#运行时代理仅中国大陆)。
- 自建：`docker buildx bake --load`（amd64 + arm64，本机只加载当前架构）；全平台用
  `docker buildx bake citracker-all --push`。

### 端口与启动参数

| 端口 | 进程 | 说明 |
|---|---|---|
| `8000` | `serve all` / `serve rest` | REST API，明文 HTTP，默认只在容器网络内 |
| `18080` | `serve all` / `serve webhook` | GitHub webhook 接收端；可终止 TLS，GitHub 要求 https 回调 |
| `8001` | `serve mcp` | MCP Streamable HTTP，明文 HTTP，默认按需发布 |

`docker-compose.yml` 把服务拆成两个常驻容器和一个一次性容器：

- `web`：`serve all`，REST 8000 + webhook 18080；只发布 18080。
- `mcp`：`serve mcp`，8001；只发布 8001。
- `init`：首次启动执行 `./docker/init.d` 下的运营脚本（建库建表、PAT 注入），成功后退出；
  `web`、`mcp` 等它成功后才启动。

两个常驻容器使用同一个镜像，只是 `command` 不同。也可以直接 `docker run` 单独启动其中一个，
但要把 init.d 目录挂进去（否则首次启动会明确报错，而不是带病启动）：

```bash
docker run -d --name citracker-web --restart unless-stopped \
  -p 18080:18080 \
  -v /path/to/certs:/certs:ro \
  -v "$PWD/docker/init.d:/docker-entrypoint-init.d:ro" \
  -e CITRACKER_DB_HOST=<db-host> -e CITRACKER_DB_PORT=5432 \
  -e CITRACKER_DB_USER=<user> -e CITRACKER_DB_PASSWORD=<password> \
  -e CITRACKER_DB_NAME=citracker \
  -e CITRACKER_API_KEY=<bearer> \
  -e CITRACKER_WEBHOOK_SECRET=<github-webhook-secret> \
  -e CITRACKER_BOOTSTRAP_PAT=<fine-grained-pat> \
  -e CITRACKER_WEBHOOK_SSL_CERTFILE=/certs/fullchain.pem \
  -e CITRACKER_WEBHOOK_SSL_KEYFILE=/certs/privkey.pem \
  registry.cn-shanghai.aliyuncs.com/vistart_public/citracker:latest
```

首次启动时容器执行 `10-schema.sh` 与 `20-pat.sh`，随后开始服务；再次启动检测到表已存在就跳过。
多个容器共用一个库时，只让其中一个承担初始化（其余设 `CITRACKER_INITDB=skip`），或按 compose
的方式先运行一次性的 `init` 服务。

**初始化不会清空已有数据。** `10-schema.sh` 只做 `CREATE DATABASE`（库不存在时）与
`CREATE TABLE IF NOT EXISTS`，对已存在的库和表不写入、不删除，所以对生产库重跑 `force init`
是安全的。会丢数据的是另外两个命令，它们与"首次初始化"无关：

| 命令 | 影响 |
|---|---|
| `rhosocial-github-ci-tracker-db reset --yes` | 删除并重建全部 `ct_*` 表，凭据行一起清空，库本身保留 |
| `rhosocial-github-ci-tracker-db drop --yes` | 删除整个数据库（拒绝删 `--maintenance-db`） |
| `docker compose ... down -v` | 一体化形态下删除数据卷，等于删库 |

只有在上表三条之后，才需要重新跑一次 `force init` 恢复 schema 与凭据。

`CITRACKER_PUBLIC_BASE_URL` 是 MCP 返回的绝对 action URL 使用的外部地址，也是 webhook 回调 URL 的
默认前缀。全部环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `CITRACKER_DB_HOST/PORT` | `192.168.1.3:16689`（compose 内为 `postgres:5432`） | PostgreSQL 坐标 |
| `CITRACKER_DB_USER/PASSWORD/NAME` | `root` / `password` / `test_db` | 数据库账号与库名 |
| `CITRACKER_DB_SSLMODE` | `prefer` | libpq sslmode |
| `CITRACKER_POOL_MIN/MAX` | `1` / `10` | 连接池。每个在处理的请求占一条连接，长轮询也算，见[连接池](#连接池一个请求占一条连接) |
| `CITRACKER_API_KEY` | 空 | REST / MCP 的 Bearer；空表示不鉴权（启动告警），公网必须设置 |
| `CITRACKER_REST_HOST/PORT` | `0.0.0.0:8000` | REST 监听 |
| `CITRACKER_MCP_HOST/PORT` | `0.0.0.0:8001` | MCP 监听 |
| `CITRACKER_WEBHOOK_HOST/PORT` | `0.0.0.0:18080` | webhook 监听 |
| `CITRACKER_WEBHOOK_SECRET` | 空 | HMAC 密钥，与 GitHub webhook 配置一致 |
| `CITRACKER_WEBHOOK_URL` | 空 | 创建 webhook 时的默认回调 URL |
| `CITRACKER_PUBLIC_BASE_URL` | 空 | MCP action URL 的绝对前缀 |
| `CITRACKER_WEBHOOK_SSL_CERTFILE/KEYFILE` | 空 | webhook 的 fullchain 与私钥路径；空表示明文 HTTP |
| `CITRACKER_DEFAULT_EVENTS` | `workflow_run,workflow_job` | 订阅的 webhook 事件 |
| `CITRACKER_WAIT_MAX_SECONDS` | `300` | `wait_for_run` 单次可等待的上限（`timeout_seconds` 默认 45 秒，超过会被截断）。工具回报的 `p50/p90` 与 `suggested_timeout_seconds` 也受它约束 |
| `CITRACKER_LOG_CAPTURE` | `1` | 是否采集 step 日志。`0/false/no/off` 关闭后，所有日志读取都走整 job 的合并文本（引入 step 日志之前的行为） |
| `CITRACKER_LOG_CAPTURE_WINDOW_MINUTES` | `60` | run 结束后多久之内仍尝试采集 step 分片。这是**实测值**，不是官方承诺；调大没有坏处，调小会少抓 |
| `CITRACKER_LOG_LEVEL` | `INFO` | 日志级别；日志一律走 stdout/stderr |
| `CITRACKER_INITDB` | `auto` | 首次启动是否执行 init.d 脚本：`auto`（表不存在时执行）/ `force` / `skip` |
| `CITRACKER_INITDB_DIR` | `/docker-entrypoint-init.d` | init.d 脚本目录；compose 只读挂载 `./docker/init.d` |
| `CITRACKER_LEGACY` | `0` | `1` 等价于 `CITRACKER_INITDB=skip`（legacy 部署） |
| `CITRACKER_BOOTSTRAP_PAT` | 空 | 首次初始化注入的 fine-grained PAT（由 `20-pat.sh` 读取） |
| `CITRACKER_BOOTSTRAP_PAT_FILE` | 空 | 从文件读取 PAT，避免令牌出现在 `docker inspect` |
| `CITRACKER_BOOTSTRAP_PAT_NAME` | `default` | 凭据别名（CLI 中显示为 `pat/<name>`） |
| `CITRACKER_BOOTSTRAP_PROBE_REPO` | 空 | 用来验证 Actions 写权限的 `owner/repo` |
| `CITRACKER_IMAGE` | 发布镜像 | compose 使用的镜像；本地构建后设为 `citracker:local` |
| `CITRACKER_WEBHOOK_PORT_HOST` | `18080` | 宿主发布的 webhook 端口（容器内固定 18080） |
| `CITRACKER_MCP_PORT_HOST` | `8001` | 宿主发布的 MCP 端口（容器内固定 8001） |
| `CITRACKER_OAUTH_ISSUER` | 空 | **预留**：OAuth 模式下的 issuer；当前不可用，见下方说明 |
| `CITRACKER_RESOURCE_URL` | 空 | **预留**：OAuth 模式下本服务的 resource 标识；当前不可用 |

#### 凭据方式：当前只有静态 Bearer

本服务目前**只提供静态 Bearer 鉴权**（`CITRACKER_API_KEY`），MCP 与 REST 共用同一个 key，
CLI 不使用它。代码里保留了 OAuth 2.1 的分支（`CITRACKER_OAUTH_ISSUER`、`CITRACKER_RESOURCE_URL`），
但本项目不附带授权服务器，也没有可用的 JWKS 来源，因此**不要把这两个变量当作可用功能**：
只设置 `CITRACKER_OAUTH_ISSUER` 而没有 verifier 时，服务仍会按 Bearer 模式启动。等中心授权
服务器落地后，这一节会改写。

### TLS 证书目录

镜像**不包含证书**，约定把证书目录挂到 `/certs`：

```bash
mkdir -p docker/certs
cp <证书目录>/fullchain.pem docker/certs/
cp <证书目录>/privkey.pem  docker/certs/
```

然后在环境里指向容器内路径：

```dotenv
CITRACKER_WEBHOOK_SSL_CERTFILE=/certs/fullchain.pem
CITRACKER_WEBHOOK_SSL_KEYFILE=/certs/privkey.pem
```

`docker-compose.yml` 已把 `./docker/certs` 只读挂载到 `/certs`。证书续期后替换宿主机文件并重启
`web` 即可（`docker compose restart web`）。如果 TLS 由前置反向代理终止，把这两个变量显式设为
**空字符串**，`web` 就监听明文 HTTP。
