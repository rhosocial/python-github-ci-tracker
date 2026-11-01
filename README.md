# python-ci-tracker

GitHub CI 状态跟踪与自动化服务。

## 功能

- **触发 CI**：通过 GitHub API 触发工作流运行
- **跟踪结果**：通过 Webhook + 轮询自动收集每个 Action 的每个 Job 的每个 Step 的详细信息
- **step 级日志**：run 完成时抓取 GitHub 的日志归档，把每个 step 自己的日志文件入库。
  GitHub 只在 run 结束后约一小时里提供这份按 step 拆分的归档——那是**取回的截止时间**，
  不是保存期限：取回来的日志永久保存、随时可读，没赶上才退回整 job 的合并日志
- **多协议服务**：对外提供 CLI、MCP Server、RESTful API 接口
- **多租户**：每个用户使用自己的 GitHub 访问令牌，支持私有仓库

## 架构

```
adapters（MCP / REST / CLI）   参数绑定、认证、序列化、错误码映射
      ↓
controllers                    一组流程：触发、查询、订阅、投递、凭据…
      ↓
services                       能力：GitHub 各资源的接口（协议 + 实现）
      ↓
models                         每个模型封装自己的数据行为（ActiveRecord）
      ↑
PostgreSQL
```

- **Model**：只封装自己的数据行为。一次 `save()` 就是一次完整的写；**没有 unit of work，没有跨模型事务**。
- **Service**：封装可调用的能力，目前就是 GitHub API。这里没有问题流程，也没有存储。
- **Controller**：代表一组流程。取数、调 GitHub、落库的顺序写在这里，新增协议只加适配层。
- **App**：装配。`container.py` 一次性构造 settings / 数据库 / 控制器，适配层拿到同一个容器。

`config.py`、`database.py`、`container.py`、`views.py` 位于包根目录；`adapters/` 下每个协议一个 App。

## 部署

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

### 形态一：web + mcp 双容器（外部 PostgreSQL）

```bash
cp docker/stack.env.example docker/stack.env
# 编辑 docker/stack.env：webhook secret、API key、数据库、PAT
mkdir -p docker/certs && cp <fullchain.pem> <privkey.pem> docker/certs/

docker compose --env-file docker/stack.env up -d
```

启动顺序由 `depends_on` 保证：`init` 建库建表并处理 PAT → `web` 与 `mcp` 启动。数据库可以是
任意 PostgreSQL（本仓库的开发库是 `192.168.1.3:16689`），不要求和应用同机。

### 形态二：一体化 `postgres:18`

在形态一的基础上叠加 `docker-compose.bundled.yml`：

```bash
docker compose -f docker-compose.yml -f docker-compose.bundled.yml \
  --env-file docker/stack.env up -d
```

这个 overlay 只增加官方 `postgres:18` 服务，并把 `init` / `web` / `mcp` 的
`CITRACKER_DB_HOST` 固定为 `postgres`，因此 `stack.env` 里残留的外部数据库地址不会误生效。
数据在命名卷 `pgdata`；`docker compose ... down` 保留数据，`down -v` 删除。

### 首次启动初始化（init.d）

数据表模式与 PAT 是**运营数据**，不是镜像里的代码逻辑。镜像只提供一个通用 runner；真正的
初始化脚本在 `docker/init.d/`，由 compose 只读挂载到 `/docker-entrypoint-init.d`，机制与 MySQL
镜像的 `/docker-entrypoint-initdb.d` 相同。

| 脚本 | 作用 |
|---|---|
| `docker/init.d/10-schema.sh` | `rhosocial-github-ci-tracker-db init`：库不存在则先建库，再建全部表（幂等） |
| `docker/init.d/20-pat.sh` | 配置了 PAT 时执行 `credential pat ensure`：不存在则新增，存在则原地轮换 |

“首次”由数据库状态判定，而不是容器文件系统：runner 先执行
`rhosocial-github-ci-tracker-db is-initialized`，全部 `ct_*` 表都在就跳过脚本，否则按文件名
顺序执行。容器重建、镜像升级都不会重复初始化；脚本任一失败则 `init` 失败，`web` / `mcp`
不会带着未初始化的库启动。

`20-pat.sh` 从环境读取（`docker/stack.env`）：

```dotenv
CITRACKER_BOOTSTRAP_PAT=github_pat_xxx
CITRACKER_BOOTSTRAP_PAT_NAME=work
CITRACKER_BOOTSTRAP_PROBE_REPO=owner/repo
```

`credential pat ensure` 的语义：

1. 数据库里没有 `pat/<name>` → **新增**；
2. 已存在 → **原地轮换**（保留 id 与所有仓库绑定，不需要重新订阅）；
3. 两条路径都先让 GitHub 验证令牌，验证失败则启动失败，不会把坏令牌写进数据库；
4. 设置了 `CITRACKER_BOOTSTRAP_PROBE_REPO` 时，额外探测该仓库的 Actions 写权限，
   结果决定 `can_write_actions`（触发 CI 的前提）。

令牌需要的权限：`Actions: Read and write`、`Metadata: Read`；需要自动创建 webhook 时再加
`Administration: Read and write`。

不想让令牌出现在 `docker inspect` 时，用文件形式：把令牌放进一个**目录**，把该目录挂进 `init` 容器，并设置 `CITRACKER_BOOTSTRAP_PAT_FILE`，例如
`CITRACKER_BOOTSTRAP_PAT_FILE=/run/secrets/citracker-pat`。脚本和 CLI 都支持
`--token-file` / `--token-stdin`，令牌不会出现在进程参数里。

> **挂目录，不要挂单个文件。** 在 Docker Desktop（Hyper-V 后端）上，单文件 bind mount 会让容器在宿主机路径不可达时变成**无法删除**的状态：`docker rm -f`、`docker compose down`、`docker start` 全部阻塞，只能重启引擎。目录挂载没有这个失败模式。本仓库的本地覆盖文件就是按目录挂载的：

```yaml
volumes:
  - ./docker/secrets:/run/secrets:ro      # 目录，不是 ./docker/secrets/citracker-pat
```

本仓库的部署同样走这条路径：`CITRACKER_BOOTSTRAP_PAT_NAME=work`、
`CITRACKER_BOOTSTRAP_PROBE_REPO=rhosocial/python-github-ci-tracker`，首次启动即自动新增。

#### 重跑初始化

首次启动之后脚本不会自动再跑。改过脚本，或首次启动时还没配 PAT、之后才补上令牌时：

```bash
docker compose run --rm -e CITRACKER_INITDB=force init
```

也可以往 `docker/init.d/` 里加自己的 `*.sh`（按文件名排序，任一失败即停止）。这些脚本是部署
数据，不会被烘进镜像。

手工方式（不使用 init.d 时）：

```bash
docker compose exec web rhosocial-github-ci-tracker credential pat add \
  --name work --token <PAT> --probe-repo owner/repo
docker compose exec web rhosocial-github-ci-tracker credential pat list
docker compose exec web rhosocial-github-ci-tracker credential pat show --name work --probe-repo owner/repo
```

### 停机、重启与升级

四个操作的效果不同，区别只在"容器是否被重建"和"数据卷是否被删"：

| 操作 | 容器 | init 是否再跑 | 数据 |
|---|---|---|---|
| `docker compose stop` / `start` | 保留，仅停止/启动进程 | 会跑一次，`auto` 检测到表存在后立即跳过 | 不变 |
| `docker compose restart web` | 保留，重启进程 | 同上（`web` 设了 `CITRACKER_INITDB=skip`，不跑） | 不变 |
| `docker compose down` → `up -d` | 删除并重建 | 会跑，仍然跳过 | 外部库不变；一体化形态卷保留 |
| `docker compose down -v` | 删除 | 会跑，此时表已不存在 → **真正执行建表** | 一体化形态卷被删，数据丢失 |

要点：

- `restart: unless-stopped` 写在 `x-app` 上，所以**宿主机重启后 `web` / `mcp` 会自动回来**；
  `init` 是 `restart: "no"`，它只在每次 `up` 时跑一次然后退出（`Exited (0)` 是正常状态，
  不是失败）。
- 停机的服务不会自行恢复数据：webhook 在停机期间投递会失败，GitHub 会记一次失败投递。
  恢复后直接调用 `find_runs`（它每次都读 GitHub）即可补齐停机期间漏掉的 run，这条路径不依赖 webhook。
- 升级镜像：

  ```bash
  docker compose --env-file docker/stack.env pull
  docker compose --env-file docker/stack.env up -d
  docker compose --env-file docker/stack.env ps      # web / mcp 应为 healthy
  curl -k https://127.0.0.1:18080/readyz             # {"status":"ready",...}
  ```

  拉取新镜像后 `up` 会重建容器，`init` 跑一次并跳过，正在服务的库不受影响。

- **schema 变了就重启 `web` / `mcp`。** 连接池在 PostgreSQL 上是服务端预处理语句：另一个
  进程（`init`、`db init`、`db reset`）改了表结构之后，已有连接里缓存的 plan 会以
  `cached plan must not change result type` 失败，直到这些连接被换掉。`up -d` 重建容器
  不存在这个问题（连接是新的）；只跑 `init` 而不重建应用容器时，显式
  `docker compose restart web mcp`。已失败的投递不会丢：`ci_status.health.alerts` 的
  `runs` 列出受影响的 run id，对它们各调一次 `get_run` 就从 GitHub 补回来了。
- **什么改动需要重建镜像**，这一点和普通的"改代码"不同：

  | 改动位置 | 是否自动重建镜像 | 需要做什么 |
  |---|---|---|
  | 本仓库 `src/**`、`pyproject.toml`、`Dockerfile`、`docker/**` | **是**，push 即触发 `images.yml` | 等 CI 绿灯后 `pull` + `up -d` |
  | 只有 `tests/**`、`AGENTS.md`、`README.md` | 否 | 什么都不用做，线上行为不变 |
  | **兄弟库**（`python-activerecord`、`python-activerecord-postgres`、`python-mcpserver`） | **否**（本仓库没变） | 手动触发一次镜像构建 |

  兄弟库那条容易漏：镜像在**构建时**用 `pip install git+https://github.com/rhosocial/python-activerecord.git@<ref>`
  固化进去，运行时不会重新取。手动触发：

  ```bash
  gh workflow run images.yml --ref main
  gh run watch   # 或等通知；multi-arch 构建通常 7–8 分钟
  docker compose --env-file docker/stack.env pull && docker compose --env-file docker/stack.env up -d
  ```

  只有本地验证时不必走 registry：`CITRACKER_IMAGE=citracker:local docker compose build` 然后
  `CITRACKER_IMAGE=citracker:local docker compose up -d`。
- 证书续期只影响 `web`：替换 `docker/certs/` 下的文件后 `docker compose restart web`。

### 探针：`/livez` 与 `/readyz`

两个问题，两个路径，**不要混用**：

| 路径 | 问什么 | 检查 | 失败时该做什么 |
|---|---|---|---|
| `/livez` | 进程活着、事件循环还能响应 | **无 I/O** | 重启容器 |
| `/readyz` | 这个实例现在能接请求吗 | 一次数据库往返（`SELECT 1`，2 秒上限） | 摘流，**不重启** |

`/health` 与 `/healthz` 是 `/readyz` 的别名（旧部署和外部监控指向这两个名字）。四个路径都不需要
API key，也不占用每请求的连接作用域。

- `docker-compose.yml` 的 healthcheck 打的是 `/readyz`，两个常驻容器各一份；
- 数据库不可达时是 `503 {"status":"not_ready","checks":{...}}`，**数据库恢复后自动回到 200**，
  不需要重启容器；
- 缺表不参与就绪判定：`checks.schema` 会写 `missing: ...`（启动时查一次），但 `init` 才是建表的地方；
- `ci_status.health` 是给人和 agent 看的运维视图（投递流、回填速率、写入失败），**不是编排器探针**。

#### 这个进程是哪个构建：`build` 块

`version` 是源码里的常量，同一份代码的每个镜像都答一样的值，**判断不了"线上是不是我刚推的那次构建"**。
所以镜像在构建时把自己盖章进去，`/livez`、`/readyz` 和 `ci_status.service` 都会返回：

```jsonc
{
  "status": "ready",
  "version": "1.0.0.dev1",              // 源码常量，不区分构建
  "build": {
    "revision": "e5815a5f5b31df495dc69638d11602df685a0e4b",  // 完整提交号
    "short_revision": "e5815a5",                            // git log --oneline 里那个
    "number": "57",                                        // 发布工作流的构建号
    "built_at": "2026-10-31T02:27:51Z"                     // UTC
  },
  "checks": {"container": "ok", "database": "ok", "schema": "ok"}
}
```

- 值来自镜像环境变量 `CITRACKER_BUILD_REVISION` / `_NUMBER` / `_BUILT_AT`，由
  `images.yml` 在构建时传入（`GIT_REVISION` / `GIT_BUILD_NUMBER` / `GIT_BUILT_AT`）；
- 本地 `pip install -e .` 或自建镜像没有这些参数时，四个字段都是 `null`——**`null` 表示没盖章，
  不是"旧版本"**；
- 镜像同时带 `org.opencontainers.image.revision` 标签，`docker inspect` 也能看到；
- 部署后想确认拉对了镜像：`curl -k https://127.0.0.1:18080/readyz | jq .build`，与
  `git rev-parse HEAD` 对比；不一致就是 `latest` 没更新或容器没重建。

### 连接池：一个请求占一条连接

连接作用域是**每请求**的（REST 中间件、MCP 包装层各起一个），所以：

- 每个正在处理的请求占 `CITRACKER_POOL_MAX`（默认 10）里的一个名额；
- **长轮询也算**：`wait_for_run` 在等待期间一直握着自己的连接，300 秒的等待就是 300 秒的占用；
- `CITRACKER_POOL_MAX` 要覆盖"并发长轮询 + 并发普通请求"。不够时后来者会在 `acquire` 上排队，
  30 秒后得到 `Failed to acquire connection within 30.0 seconds`。

读这条错误的方法：

| `Pool stats` | 含义 | 处理 |
|---|---|---|
| `in_use` 接近 `max_size` | **饱和**：并发确实超过了池子 | 调大 `CITRACKER_POOL_MAX`，或减少并发长轮询 |
| `available=0, in_use=0` | **池子被掏空**（许可泄漏） | 重启容器；这是 1.0.0.dev1 之前版本的缺陷，`python-activerecord` 的 `a55ebbd9` 已修 |

### 更新 / 替换 PAT

令牌有两种存放形式，更新步骤不同，但都以 `force init` 收尾：

**文件形式（推荐，令牌不进 `docker inspect`）**

```bash
# 1. 改宿主机上的令牌文件（即 compose 只读挂载到 /run/secrets/citracker-pat 的那个文件，
#    见本机 docker-compose.override.yml）；文件里只放一行令牌，不要多余换行
# 2. 重跑初始化，让 20-pat.sh 重新 ensure
docker compose --env-file docker/stack.env run --rm -e CITRACKER_INITDB=force init
```

**环境变量形式**

```bash
# 1. 改 docker/stack.env 里的 CITRACKER_BOOTSTRAP_PAT
# 2. 重跑初始化
docker compose --env-file docker/stack.env run --rm -e CITRACKER_INITDB=force init
```

两种方式背后都是 `credential pat ensure`，因此：

- 同名凭据**原地轮换**：id 不变，仓库绑定全部保留，**不需要重新订阅**；
- 换一个 `CITRACKER_BOOTSTRAP_PAT_NAME` 会**新增**一条凭据，旧凭据及其绑定不受影响——
  想让新令牌接管，要么沿用同名，要么用 `repo bind --name owner/repo --credential pat/<新名>`
  把仓库改绑过去。

验证轮换结果：

```bash
docker compose exec web rhosocial-github-ci-tracker credential pat list
docker compose exec web rhosocial-github-ci-tracker credential pat show \
  --name work --probe-repo owner/repo
```

`can_write_actions` 为 `true` 才表示触发 CI 的能力被验证过（`--probe-repo` 是这项探测的
目标仓库；不给它，能力保持未知）。

#### 手工配置 PAT 时可能遇到什么

| 现象 | 原因 | 处理 |
|---|---|---|
| `init` 退出非 0，`web` / `mcp` 不启动 | GitHub 拒绝了令牌（拼错、已吊销、已过期） | 换令牌后重跑 `force init`。坏令牌不会被写进库 |
| 启动成功但 `ci_status` 的 `can_trigger` 为 `false` | 令牌缺 `Actions: Read and write`，或没给 `--probe-repo` 导致能力未知 | 补权限 / 补 `CITRACKER_BOOTSTRAP_PROBE_REPO` 后重跑 |
| 工具返回 `credential_expired` / `credential_invalid` | 运行期间令牌过期或被吊销（服务不会因为令牌问题停机） | 按 `action_url` 打开设置页或按上一节轮换；不要在同一令牌上重试 |
| 工具返回 `credential_insufficient` | 令牌对该操作权限不足 | 在 GitHub 上补权限，重新录入；换令牌不要绕成"再找一个凭据" |
| `--probe-repo` 指向不存在的仓库 | 探测目标写错 | 换成真实仓库；这不是致命错误，但 `can_write_actions` 会保持未知 |
| 令牌出现在 `docker inspect` 里 | 用了 `CITRACKER_BOOTSTRAP_PAT` 而非 `_FILE` | 改用文件形式；已经泄漏的令牌应立刻在 GitHub 上吊销 |
| 令牌出现在 shell 历史或进程列表 | 手工执行 `credential pat add --token <明文>` | 用 `--token-file` 或 `--token-stdin`；清掉历史记录并吊销旧令牌 |

> 令牌权限最小集：`Actions: Read and write`、`Metadata: Read`；需要本服务代为创建 webhook
> 时再加 `Administration: Read and write`。`Contents` 只在需要解析工作流文件时才用到。

### legacy / 手工模式

已经自行管理 schema 与凭据的部署（旧库、外部迁移工具、手工 PAT）用：

```dotenv
CITRACKER_INITDB=skip
```

`init` 不执行任何脚本，`web` / `mcp` 也不建表、不碰 PAT。`CITRACKER_LEGACY=1` 是等价写法。

legacy 模式下需要自己保证：

```bash
# 只建表（幂等；数据库不存在时先创建）
docker compose run --rm web rhosocial-github-ci-tracker-db init

# 录入凭据
docker compose exec web rhosocial-github-ci-tracker credential pat ensure \
  --name work --token <PAT> --probe-repo owner/repo
```

`credential pat ensure` 同样是幂等的：不存在则新增，存在则轮换，适合放在自己的 init 脚本里。

### 清理数据库

```bash
# 是否已初始化（0=是，1=否；例行检查用）
docker compose exec web rhosocial-github-ci-tracker-db is-initialized

# 统计各表行数
docker compose exec web rhosocial-github-ci-tracker-db stats

# 按时间清理历史（保留最近 90 天的 run、365 天的日志）
docker compose exec web rhosocial-github-ci-tracker-db cleanup \
  --runs-before 90d --logs-before 365d

# 删除并重建全部 ct_* 表（保留数据库本身）
docker compose exec web rhosocial-github-ci-tracker-db reset --yes

# 删除整个数据库（先断开连接；teardown 用）
docker compose exec web rhosocial-github-ci-tracker-db drop --yes

# 一体化形态：连数据卷一起删除
docker compose -f docker-compose.yml -f docker-compose.bundled.yml down -v
```

`drop` 只删 `CITRACKER_DB_NAME` 指向的库，拒绝删除 `--maintenance-db`（默认 `postgres`）。
`reset` 会连同凭据行一起清空，之后要恢复自动注入：

```bash
docker compose run --rm -e CITRACKER_INITDB=force init
```
执行前请确认 `CITRACKER_DB_HOST` / `CITRACKER_DB_NAME` 指向正确的环境。

### 典型场景：citracker 怎么知道有 CI 在跑

**判定只有一条：仓库是否已订阅。** 不看是谁触发的——push、PR、协作者手点、`gh workflow run`、本服务触发，一律采纳。所以"要不要告诉 citracker"这个问题不存在：不需要。

| 场景 | citracker 如何得知 | 需要你做什么 |
|---|---|---|
| 1. agent 提交并 push，工作流按 `on: push` 自动跑 | GitHub 把 `workflow_run` 投递到已配置的 webhook，citracker 直接采纳，run 立刻入库 | 什么都不用做 |
| 2. push 后在 GitHub 页面手点 Run workflow | 同上，`event` 记为 `workflow_dispatch` | 什么都不用做 |
| 3. 通过 citracker 触发（`trigger_workflow` / CLI） | 触发时登记，webhook 后续把状态补齐 | 一次调用 |
| 4. 用 `gh workflow run` 直接触发 | 同场景 1——webhook 照样到达，citracker 照样采纳 | 什么都不用做，但 citracker 不知道"这次是你主动要跑的"，不会替你做幂等判断 |
| 5. 发起 PR，`on: pull_request` 触发 | 同场景 1，`pr_number` 一并记录 | 什么都不用做 |

两个前提，缺一不可：

1. **仓库已订阅**（`repo subscribe`）——未订阅的仓库，投递会到达但被拒绝，只在投递台账里留一条 `handled=false`；
2. **仓库有 hook 指向本服务**（`webhook_url` + `webhook_secret`，可用 `repo subscribe --create-webhook` 或手工在 GitHub 上配）——没有 hook 就什么都收不到。

场景 1–5 都可能发生在**尚未订阅**的仓库上。此时不会有任何 run 入库；已登记的 fine-grained PAT 若有该仓库（或所属组织）的访问权限，运营方补一条 `repo subscribe` 即可开始纳管，**历史 run 可以用 `find_runs` 或 `--sync-runs` 补拉**，不需要等下一次触发。

只有场景 3 需要注意：`trigger_workflow` / `rerun_run` 不是幂等的，响应不明确时先 `find_runs` 核对，不要重试——重试会起第二次运行。

### PAT 失效或权限不足时会怎样

分三个时点，表现完全不同：

| 时点 | 情形 | 表现与处置 |
|---|---|---|
| 首次初始化 | 令牌无效、被吊销、已过期 | `credential pat ensure` 先让 GitHub 验证，失败则 **init 失败**，`web`/`mcp` 不启动；坏令牌不会写进库。换令牌后重跑 `force init` |
| 运行期 | 令牌过期 / 被吊销 | 服务**不停机**。需要访问 GitHub 的操作返回 `credential_expired` / `credential_invalid`（带 `action_url`）；纯读库的工具（`ci_status`）照常工作；`find_runs` / `get_run` 需要访问 GitHub，会返回同一错误。按文档轮换令牌即可，不要用同一令牌重试 |
| 运行期 | 令牌缺某仓库的访问权 | 访问该仓库的调用返回 `github_not_found`（GitHub 对无权仓库回 404，不区分"不存在"与"无权"）；`repo subscribe` 会因 GitHub 不可见而失败。补授权后重新录入，`credential pat ensure` 会原地轮换 |
| 运行期 | 令牌缺 Actions 写权限 | 读一切正常，`trigger_workflow` / `rerun_run` 被拒绝，`ci_status.can_trigger` 为 `false`。补权限后重跑 `credential pat show --probe-repo <repo>` 复测 |
| 任何时点 | webhook 仍会到达 | hook 装在仓库侧，与令牌无关。所以库里的 run 状态还会更新；只有需要访问 GitHub 的动作（`find_runs`、`get_run`、补抓日志）会失败，台账里 `unhandled` 可能出现增长 |

一句话：**令牌决定"能主动访问什么"，hook 决定"能被动收到什么"**。令牌失效不会让推送通道静默，但会让你在需要补数据时卡住。

### 运行时代理（仅中国大陆）

镜像本身没有代理配置。需要代理的部署**在运行时**注入，例如在本地覆盖文件里：

```yaml
services:
  web:
    environment:
      HTTP_PROXY: http://host.docker.internal:7897
      HTTPS_PROXY: http://host.docker.internal:7897
      NO_PROXY: localhost,127.0.0.1,::1,host.docker.internal,192.168.1.3
```

- Docker Desktop 会把宿主代理以 `127.0.0.1:7897` 注入容器，但容器里的 `127.0.0.1` 是容器自身；
  必须覆盖为 `host.docker.internal`。
- `NO_PROXY` 要包含数据库主机，否则连库也会被送进代理。
- 全球用户不需要这些变量；不设置就是直连。

### 验证

```bash
curl -fsS https://<域名>:18080/readyz     # 就绪：含一次数据库往返；失败时 503 + checks
curl -fsS https://<域名>:18080/livez      # 存活：不碰数据库
docker compose logs -f init web mcp
docker compose exec web rhosocial-github-ci-tracker credential pat list
```

MCP 客户端配置见下一节。

#### 投递台账与推送通道健康

每一次到达接收端的 GitHub 投递都会写一行 `ct_webhook_delivery`，无论它是被处理、被忽略，还是因为验签失败被拒。`workflow_run` 和 `workflow_job` 在 GitHub 侧是两条独立的流（payload、生命周期都不同），因此台账用 `event_type` 区分，报告时也分开：

```bash
# 行数、按流统计都走这个命令
docker compose exec web rhosocial-github-ci-tracker-db stats
```

`ci_status` 的 `health` 分为按流的结构，另保留两个旧字段供兼容：

```json
{
  "ledger_available": true,
  "runs_tracked": 15,
  "last_webhook_at": "2026-10-26T15:20:11+00:00",
  "webhooks_in_last_hour": 13,
  "push_channel": "active",
  "alerts": [],
  "streams": {
    "workflow_run": {"last_at": "...", "in_last_hour": 2, "deliveries_total": 37,
                     "redelivered": 1, "unhandled": 0, "unhandled_reasons": {},
                     "write_failures": 0, "last_write_failure": null},
    "workflow_job": {"last_at": "...", "in_last_hour": 11, "deliveries_total": 402,
                     "redelivered": 0, "unhandled": 4,
                     "unhandled_reasons": {"ignored": 4},
                     "write_failures": 0, "last_write_failure": null}
  }
}
```

读法：

- `last_at` 为 `null` 表示**该流**从未到达过；两条流自各独立，不要用一条流的静默去判断整条通道；
- `redelivered` 是在 GitHub 页面点过 "Redeliver" 的次数（同一个 `X-GitHub-Delivery` 出现第二次）。重发与自动投递对"通道是否活着"是同等证据，区别只在时间；
- `unhandled` 是到达但未改变任何状态的条数（未订阅仓库、`ping`、非订阅事件等）。它非零不等于故障，但持续增长通常意味着订阅范围与投递范围不一致；
- `unhandled_reasons` 是这 24 小时内未处理的原因分布，`unhandled` 只是总数；
- **`write_failures` 与 `alerts` 是另一回事**：`ignored` / `unsupported_event` / `invalid_json` / `signature_failed` 是"我们选择不落库"，其余原因（如 `IntegrityError`）是**我们没能把这条投递描述的东西写进库**——那条 run 或 job 因此缺失或陈旧。这类原因会被提升为顶层告警：

```json
"alerts": [{
  "code": "webhook_write_failed",
  "stream": "workflow_job",
  "count": 7,
  "last": {"delivery_id": "...", "received_at": "...", "repository": "...",
           "run_id": 40378123978, "job_id": 121746271630, "reason": "IntegrityError: ..."},
  "hint": "The delivery arrived but storing what it described failed ... call find_runs, which reads GitHub ..."
}]
```

告警带 `delivery_id` 与目标 run/job，所以能定位到具体哪一条投递描述了哪一次运行：

```bash
# 列出未落库的投递（含 delivery_id / run / job / 原因）
docker compose exec web rhosocial-github-ci-tracker-db deliveries --unhandled --limit 20
docker compose exec web rhosocial-github-ci-tracker-db deliveries --stream workflow_job --limit 50
```

修复路径：用 `find_runs` 重新拉一遍即可补回（拉取通道不依赖 webhook）。同一 run 的多个 job 事件同时到达时，只有第一条会去读 GitHub 上的父 run，其余复用它的结果；即使两条同时插库，输的一方也改为读取已存在的行，而不再让整条投递失败。

**一条投递要么整体落库，要么整体回滚。** 一个 `workflow_job` 事件要写 run（若尚未存在）、job 以及它的 steps，这些写入在同一个事务里（`AsyncWorkflowRun.transaction()`，模型级 API；`backend()` 会优先取连接上下文，所以同一次请求里的所有模型共用那条连接）。失败则全部回滚，并在台账记下原因。否则会留下"job 存在但没有 steps"的半截状态——它看起来是完整的，于是没有人会去修，只能等下一次比对 GitHub 才发现。

台账只存关联与结论，**不存 payload**：GitHub 自己的 Recent Deliveries 保留 30 天，凭 `delivery_id` 可以取回原文。因此台账可以定期清理，不影响已落库的运行数据：
```bash
# 清理 30 天前的投递记录；run / job / step 的数据不受影响
docker compose exec web rhosocial-github-ci-tracker-db cleanup --deliveries-before 30d
```

## 开发


```bash
# 创建虚拟环境
py -3.15 -m venv .venv3.15

# 先装兄弟项目：核心库与 Postgres 后端各取自己的 release 线
#   python-activerecord          -> release/v1.0.0.dev30
#   python-activerecord-postgres -> release/v1.0.0.dev17
# （列约束改动已合入这两条线；两个目录先切到对应分支，或使用其 worktree）
# MCP 基础层需要 1.0.0.dev1 或更新。
.venv3.15/Scripts/pip install -e ../python-activerecord -i https://mirrors.aliyun.com/pypi/simple/
.venv3.15/Scripts/pip install -e ../python-activerecord-postgres -i https://mirrors.aliyun.com/pypi/simple/
.venv3.15/Scripts/pip install -e ../python-mcpserver -i https://mirrors.aliyun.com/pypi/simple/

# 最后装本项目及开发依赖
.venv3.15/Scripts/pip install -e ".[dev]" -i https://mirrors.aliyun.com/pypi/simple/
```

**测试需要一个真实的 PostgreSQL**（9.6 以上即可；生产验证 18.6）。测试套件把模型绑到真实服务上，
每个测试拿到一份重建后的 schema——SQLite 已在 2026-11-01 移除，不再维护它的 DDL。服务器由环境变量
或场景文件指定（CI 用 service container）：

```bash
# 方式一：直接给坐标
CITRACKER_DB_HOST=localhost CITRACKER_DB_PORT=5432 \
CITRACKER_DB_USER=citracker CITRACKER_DB_PASSWORD=citracker \
PYTHONPATH=tests .venv3.15/Scripts/python -m pytest tests -q

# 方式二：共享的 postgres_scenarios.yaml（python-activerecord-postgres 里的那个）
CITRACKER_TEST_PG_SCENARIO_FILE=../python-activerecord-postgres/tests/config/postgres_scenarios.yaml \
CITRACKER_TEST_PG_SCENARIO=postgres_18 \
PYTHONPATH=tests .venv3.15/Scripts/python -m pytest tests -q
```

没有服务器时测试**跳过**并说明原因；CI 里 `CITRACKER_TEST_PG_REQUIRED=1` 把它变成失败。

## 接入 OpenCode（MCP）

MCP 适配层通过 Streamable HTTP 暴露，默认端点是 `http://127.0.0.1:8001/mcp`。

### 启动 MCP 服务

```bash
# 默认 0.0.0.0:8001；端口可用 CITRACKER_MCP_PORT 调整
rhosocial-github-ci-tracker serve mcp

# 仅本机使用时，建议显式绑定 loopback
rhosocial-github-ci-tracker serve mcp --host 127.0.0.1 --port 8001
```

`CITRACKER_API_KEY` 留空是允许的：此时 MCP 不启用 Bearer 校验，启动日志会给出告警。
公网部署必须提供该 key，这由部署者自行保证；本服务不会因为 key 为空而拒绝启动。

MCP 工具与 REST、CLI 共用同一套业务核心。首次接入建议先调用 `ci_status`、`list_repos`
验证连通性；`trigger_workflow` / `rerun_run` 会消耗 CI 额度，确认无误后再使用。

### 配置 OpenCode（V2）

配置写在全局 `~/.config/opencode/opencode.jsonc`（Windows 为
`%USERPROFILE%\.config\opencode\opencode.jsonc`），不要写进项目配置：

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "citracker": {
        "type": "remote",
        "url": "http://127.0.0.1:8001/mcp",
        "oauth": false,
        "protocol": "auto",
        "headers": {
          "Authorization": "Bearer {env:CITRACKER_API_KEY}"
        }
      }
    }
  }
}
```

- `oauth: false`：当前没有中心授权服务器，关闭 OpenCode 的 OAuth 发现。
- `protocol: "auto"`：服务端面向 MCP `2026-07-28`，`auto` 会先探测该版本，失败再回退到
  `legacy` 握手。
- `headers.Authorization`：静态 Bearer，与 REST 共用 `CITRACKER_API_KEY`。OpenCode 进程
  必须能读到该环境变量；从桌面图标启动时读不到 shell 里临时设置的变量，此时可把令牌
  直接写进全局配置（该文件不入库）。

也可以用 CLI 添加，再手工补齐 `oauth`、`protocol` 和 `headers`：

```bash
opencode mcp add citracker --global --url http://127.0.0.1:8001/mcp
```

### 验证

```bash
opencode mcp list
```

显示 `citracker connected` 即接通。异常时先用 `opencode mcp debug citracker` 查看 HTTP 与
鉴权细节，或在 TUI 里用 `/mcps` 重连。

### 鉴权

MCP 与 REST 共用 `CITRACKER_API_KEY`。设置了该变量时，`/mcp` 要求
`Authorization: Bearer <key>`，与上面的客户端配置一致；比较是常量时间的，错误的令牌
由 SDK 以 `401` 拒绝。该 key 代表 operator，持有 `citracker:read`、`citracker:write`
与未使用的 `citracker:admin` 全部 scope，请像保护 REST key 一样保护它。

`CITRACKER_API_KEY` 为空时，MCP 和 REST 都不鉴权，启动日志会分别打印
`MCP bearer verification is disabled` / `REST API authentication is disabled` 告警。
这是受支持的部署方式（本机、内网、或由前置网关鉴权），但公网直连时必须由部署者提供
key。CLI 不使用这个 key，`credential`、`serve webhook` 等命令在 key 为空时照常运行。

`serve mcp` 默认绑定 `0.0.0.0`；仅本机使用时请显式使用 `--host 127.0.0.1`。

### 用提示词驱动

工具名与参数由 MCP 提供，agent 自己会挑；用户只需要把意图说清楚，**给仓库名、分支或提交号、
工作流名**这几样，比说"看看 CI"有效得多。下面两段是可直接复制的开场提示。

中文：

```text
用 citracker 看一下 rhosocial/python-activerecord 在 main 上最新一次 test.yml 的结果。
如果还在跑，等到它结束再告诉我；失败了就指出是哪个 job、哪个 step，并把关键错误行贴出来。
```

```text
用 citracker 一次性看一下 python-activerecord 系列 15 个仓库各自最新一次运行的状态，
汇总成一张表：仓库 / 结论 / 分支 / 耗时，失败的要标出来。
```

```text
用 citracker 统计一下 python-activerecord 系列里，过去三次运行中哪些仓库连续失败，
以及每个失败仓库最近一次失败是哪个 job、哪个 step。
```

```text
我刚推了 3 个后端仓库（mysql / postgres / mariadb）的 fix/column-constraint-gates 分支，
用 citracker 分别查一下各自最新一次的运行状态，汇总成一张表：仓库 / 结论 / 耗时 / 失败 job。
```

```text
用 citracker 触发 rhosocial/python-activerecord-postgres 的 test.yml（ref: main），
然后跟踪到结束。失败了先别重跑，把失败原因给我。
```

English:

```text
Use citracker to check the latest test.yml run for rhosocial/python-activerecord on main.
If it is still running, wait until it finishes. If it failed, tell me which job and step
failed and show the key error lines.
```

```text
I just pushed fix/column-constraint-gates to three backend repos (mysql, postgres, mariadb).
Ask citracker for the latest run in each and give me one table: repo / conclusion / duration /
failing job.
```

```text
Ask citracker for the newest run of all fifteen python-activerecord repositories in one go,
then give me only the ones that are red, with the failing job and step for each.
```

```text
Use citracker to dispatch test.yml on rhosocial/python-activerecord-postgres (ref: main) and
track it to completion. If it fails, do not re-run: report the failure first.
```

写提示词时的三条约定：

- **别让 agent 轮询**。说"等到结束"，agent 会调 `wait_for_run`；说"每 10 秒查一次"，它会把
  上下文烧在重复调用上。
- **触发类请求要带 ref**，并且明确"只跑一次"。`trigger_workflow` 不是幂等的，含糊的答复会让
  agent 重复派发，直接多烧一次 CI。
- **失败时先要原因，别先说"重跑"**。先 `get_job_log` 拿到错误行，再决定重跑还是改代码。

### 任务跟踪

一次典型的跟踪是几个工具接力，agent 通常不需要你指挥具体调哪个：

| 阶段 | 工具 | 说明 |
|---|---|---|
| 概览 | `recent_runs` | 一批仓库各自最近 N 次运行的**摘要**，一次调用回答"这些仓库现在什么状态"。上限 20 个仓库 × 10 条；某个仓库失败只在原位报错，不影响其他仓库 |
| 定位 | `find_runs` | 单个仓库按分支/提交/PR/工作流/状态查；每次都读 GitHub，所以能补上漏收的 webhook |
| 等待 | `wait_for_run` | 单次阻塞默认 45 秒，上限由 `CITRACKER_WAIT_MAX_SECONDS` 决定（默认 300 秒）。返回 `timed_out=true` 表示**还在跑，不是失败**，再调一次即可 |
| 归因 | `get_run` → `get_job` → `get_job_log` | `get_run` 给 job 名册与 `failed`；`get_job` 给该 job 的每个 step 与 `has_log`；`get_job_log` 读那一个 step 的文本 |

选择建议：跨多仓库问"谁红了"用 `recent_runs`；盯一个仓库的某次提交或某条 PR 用 `find_runs`。一次投递的写入在 `pool.transaction()` 里，失败则整体回滚；连接由请求上下文解析，不写模型类属性。
两者都只给摘要。要 job 名册再调 `get_run`，要某个 job 的 step 与日志再调 `get_job` /
`get_job_log`——这是刻意的分层：每层只返回自己那一层的对象，避免一次调用把上下文灌满。

`recent_runs` 是批量读：仓库与 workflow 各一条查询，每个仓库一条 run 查询（"每个仓库取最新 N 条"
没法用一条 `IN` 查询表达），15 个仓库约 16 条 SQL、1 秒内。它**不**加载 jobs/steps——摘要用不到。
某个仓库本地一条 run 都没有时会去 GitHub 确认一次，并把"确实没有"记 5 分钟，否则一个空仓库会让
每次批量调用都多花几秒。

```text
# 概览：15 个仓库一次问完（实测 3.9 秒，纯读库）
recent_runs(repositories=[...], limit=1)

# 单仓库定位：某分支的最新一次
find_runs(repository="rhosocial/python-activerecord", branch="main", latest=True)
```

### 列表在哪个键下

每个工具按内容命名自己的列表键，且**空结果与非空结果用同一个键**：

| 工具 | 列表位置 | 空结果也长这样 |
|---|---|---|
| `find_runs` | `items[]` | `{"items": [], "repository": "..."}` |
| `recent_runs` | `repositories[].runs[]` | 每个仓库条目带 `runs: []` 或 `error` |
| `list_repos` | 列表模式 `items[]`；单仓库模式 `workflows[]` | `items: []` / `workflows: []` |
| `get_run` | `jobs[]`（名册，**不含 steps**） | `jobs: []` |
| `get_job` | `steps[]` | `steps: []` |

`find_runs` 曾经在"有结果"时返回 `items`、"无结果"时返回 `runs`——按 `runs` 读的调用方
只会拿到 undefined，且 hint 里没有任何提示。现在两条路径都是 `items`，并且 hint 明说
"Results are under items[]"。

触发与查询都可以用其中任一形式：显示名（`Changelog Check`）、文件名（`changelog-check.yml`）、
完整路径（`.github/workflows/changelog-check.yml`）或数字 id。名字比较**忽略大小写与首尾空格**，
因为 GitHub 会原样保留作者写的大小写。

名字对不上时，错误信息会列出该仓库**全部**可用工作流，形如 `名字 (路径)`。按列出的原文再传一次，
不要猜拼写——`workflow_not_found` 的含义是"没有这一条"，不是"再试试别的写法"。

三种"触发不了"是不同的错误码，处理方式也不同：

| 错误码 | 含义 | 下一步 |
|---|---|---|
| `workflow_not_found` | 本地没有匹配的工作流 | 看错误里列出的 `名字 (路径)`，原文照传 |
| `workflow_not_dispatchable` | 工作流存在，但没声明 `on: workflow_dispatch` | **没有产生 run**，别重试；推一次提交，或给 workflow 加该触发器 |
| `trigger_no_write_capability` | 绑定凭据没有 Actions 写权限 | 补权限后重新探测；这是触发前唯一的门 |

重跑另有一组，**与令牌无关**，不要照着 `credential_*` 去换 token：

| 错误码 | 含义 | 下一步 |
|---|---|---|
| `run_in_progress` | 这次 run 还在跑，GitHub 拒绝重跑（403 "This workflow is already running"） | 用 `wait_for_run` 等它结束，或 `cancel_run` 取消后再重跑；换令牌没有用 |
| `run_state_conflict` | 这次 run 的当前状态不允许该操作（例如对已结束的 run 取消） | `get_run` 看当前状态，别原地重试 |

`rerun_run` 的响应直接带 `run_attempt`、`previous_attempt`、`advanced`：`advanced` 为 true
就是"这次是第 N 次 attempt"，不必再去比 GitHub；为 false 说明 GitHub 还没反映新增的 attempt，
稍后用 `get_run` 确认即可，不要连续重跑。

### 时间戳

所有对外时间戳都是**时区敏感（timezone-aware）**的，并且一律以 **UTC** 表示，ISO-8601 带
显式 `+00:00` 偏移——是"把偏移固定成 UTC"，不是"去掉时区"。数据库用 `timestamptz` 存 UTC，
视图层统一归一化；服务不猜访问者时区、也不做本地化，那是调用方展示层的事。

之前同一个响应里会同时出现 `+08:00`（数据库按连接时区渲染）与 `+00:00`（GitHub 载荷自带偏移），
读起来像两种时间；现在只剩一种。

### 读某一次 attempt

`get_run` 有 `attempt` 参数。不传就读最新一次（GitHub 对 run id 的默认语义）；传了则**每次都
向 GitHub 读这一次**（`/runs/{id}/attempts/{n}`），与本地行比对：缺了补、状态或时间戳不一致就
更正，然后才返回。旧构建留下的脏行、投递丢失留下的陈旧行，都会在被问到时修好——仍然是
"问到了才更新"，只是问的那一刻一定回源。

返回里的 `attempt` 是这次读的序号，`is_latest` 说明它是不是当前那次；读旧 attempt 时 `jobs[]`
是当时存下的 job（GitHub 的 job 端点只返回最新 attempt 的，不能拿来冒充），hint 会明说
"这是第 N 次，共 M 次，当前是第 M 次"。

序号超出范围返回 `attempt_not_found`（run 存在，只是没有这个序号）——它与 `run_not_found`
不同：前者去核对序号，后者去核对 run id。

#### step 日志：GitHub 只提供一小时，取回来就永久保存

GitHub 有两个日志来源，本项目都用：

| 来源 | 内容 | 可得性 |
|---|---|---|
| `GET /actions/jobs/{id}/logs` | 一个 job 的合并文本 | 90 天内随时可读，存进 `ct_job_logs` |
| `GET /actions/runs/{id}/logs` | zip：合并文件 + `<job>/<step 号>_<step 名>.txt` + `system.txt` | 按 step 拆分的文件**只在 run 结束后约一小时内由 GitHub 提供**，之后同一归档只剩合并文件，边界不可恢复 |

**一小时是取回（采集）的截止时间，不是保存期限。** 一旦某个 step 的文本被取回并写进
`ct_step_logs`，它就是本服务的普通数据：与 GitHub 的保留期无关，读多少次、隔多久读都一样。
截止时间唯一决定的是"还能不能再补采"，不影响"已经采到的能不能读"。

三条边界，一次说清：

| 问题 | 答案 |
|---|---|
| 多久之内必须取回？ | run 结束后 `CITRACKER_LOG_CAPTURE_WINDOW_MINUTES`（默认 60 分钟）；过了只能读该 job 的合并日志 |
| 取回之后能存多久？ | 没有时间上限。`ct_step_logs` / `ct_job_logs` 与其它表一样长期保留，读路径不做任何时效判断 |
| 什么会让已存的日志消失？ | 只有运维显式清理：`db cleanup --logs-before <时长>`（默认不动），或 `db reset` / `db drop`；仓库退订也会删掉该仓库的数据 |

因此 step 日志是**采集**，不是随时可读：

- 采集发生在 `workflow_run` completed 投递到达时，以及截止时间之前第一次有人读 step 日志时；
  已采集的不会重复抓。**不在 `workflow_job` 完成时抓**：归档是 run 级的，run 结束前
  `GET /actions/runs/{id}/logs` 一律 404（实测一个 12 job 的矩阵，每个 job 完成都 404）；
- 归档文件是**异步写入**的，run 刚结束时可能只到了几份。读取路径会为此重试：
  每次重新拉归档，间隔 60 秒，最多 6 次；404/5xx 也算"还没好"，同样重试；
  webhook 只做单次尝试，不占用投递时间；
- 存进 `ct_step_logs`，与 step 一对一、允许缺失；`system.txt` 不入库；
- 没赶上采集就**如实返回缺失**（`reason` 是 `capture_closed` / `not_a_step_archive` 等），
  绝不拿另一个 step 的文本、也不从合并文本里猜边界；
- 一个 job 的 step 全都没有日志时，调用方读该 job 的合并日志自行分析——这是设计上的退路。
- `get_job` 的 `capture` 块描述的是**采集**进度：
  `{expected_logs, captured_logs, state, window_closes_at}`。

  两个计数是**日志文件数，不是 step 数**：skipped 的 step、以及从未开始的 step，在 GitHub
  侧同样没有文件，所以 `expected_logs` 会小于 `steps` 的长度——这个差额不是漏采。
  `partial` 表示归档仍在写入，用 `refresh_logs=true` 再等一次；`closed` 表示 GitHub 已不再
  提供这个 run 的 step 分片，缺的补不回来了（已采到的照常可读）；`window_closes_at` 就是那个
  截止时间点。

对应到工具：

```text
get_job(job_id)                      每个 step 带 has_log / log_lines / failed
get_job(job_id, refresh_logs=true)   再采集一次（归档还在写入时最多等 6 分钟）
get_job_log(job_id, step=N)          第 N 个 step 自己的日志文件
get_job_log(job_id)                  整个 job 的合并文本（has_log=false 时的退路）
get_job_log(job_id, failed_steps_only=true)   一次拿全部失败 step 的文本（批量便利）
search_job_log(job_id, query, step=N)         在第 N 个 step 里搜；不带 step 搜合并文本
```

`search_job_log` 与 `get_job_log` 用同一个 `step` 参数：返回里的 `source`（`job`/`step`）与
`step` 说明行号属于哪个作用域，照它把 `from_line` 传回去即可。step 没采集到时返回
`reason`，**不是** `no_match`——没文档和没匹配是两件事。

日志读取的几个参数：`tail`（尾部 N 行）、`grep`（带上下文找错误）、`from_line`/`to_line`
（显式区间）。窗口被截断时响应里带 `truncated`、`next_from_line`、`next_to_line`，
可以按行号续读。

**默认窗口就是最后 200 行，不猜内容。** 本服务是通用 CI 跟踪器，不知道某个项目会打印
pytest 摘要、`##[error]` 标记，还是 runner 的收尾日志；按这些特征去"找重点"，对任何别的项目
都是错的。所以窗口只做寻址：

- 不传参数 → 文档最后 200 行；
- `tail=N` → 最后 N 行；
- `from_line` / `to_line` → 显式区间；
- `grep` → 调用方给的正则（非法时退化为字面量），带 `context` 行上下文。

响应里的相关字段：

| 字段 | 含义 |
|---|---|
| `returned_lines` | **实际返回的行数**。grep 选出的行不连续，`last_line - first_line + 1` 会算多 |
| `truncated` | 选区之外还有行；配合 `first_line` / `last_line` 续读 |
| `matches` | 用了 `grep` 时命中的行号 |
| `empty_reason` | 内容为空时说明是哪个条件筛空的：`grep_no_match` / `window_outside_document` / `log_empty` / `tail_not_positive` / `filters_selected_nothing` |
| `next` | 针对该原因的下一步建议（换短 pattern、`ignore_case`、改用 `tail` 等） |

`total_lines` 是整份日志的行数，`returned_lines` 是本窗口的行数。**两者一起看**：
`total_lines: 3213` 而 `returned_lines: 0` 不是"这个 job 没输出"，而是"筛选条件没匹配到"——
`empty_reason` 会明说是哪个条件。

`failed_steps_only` 读的是**已采集的失败 step**（结论属于 failure / cancelled /
timed_out / startup_failure / action_required / stale 的那些），不解析日志内容去找
`##[error]`；没有任何失败 step 时返回 `reason: no_failed_steps`，有失败 step 但都没采集到
时返回采集的原因。`conclusion` 为 `null` 的 step 不算失败——那是 GitHub 元数据还没填全。

需要在自己的日志格式里精确定位时用 `grep`（合并文本）或 `search_job_log`（可按 step）：

```text
get_job_log(repository=..., job_id=..., grep="FAILED", context=2)
search_job_log(repository=..., job_id=..., query="ModuleNotFound", step=6)
```

长任务（这套 CI 是十几分钟的矩阵）有两种处理方式，二选一：

- 让 agent 反复调 `wait_for_run`（默认每次 45 秒，`timed_out` 就再来一次）；
- 或者先用 `find_runs` 确认 run 存在，把 run id 记下来，过一会儿再查。

`wait_for_run` 单次时长由调用方传 `timeout_seconds` 决定，上限是 `CITRACKER_WAIT_MAX_SECONDS`。
把它调大可以一次等更久，但**客户端自己的执行超时必须相应放宽**，否则请求会先被客户端掐断。
OpenCode 的 `mcp.timeout.execution` 默认 12 小时，通常不用改。

**等待时长不要猜。** 该 workflow 最近的已结束 run 已经给出了答案：

- `trigger_workflow` / `rerun_run` 返回 `typical_duration_seconds: {samples, p50_seconds, p90_seconds}`；
- `wait_for_run` 超时时返回 `observed`（同样三项）和 `suggested_timeout_seconds`（p90 向上取整，
  夹在 `[10, CITRACKER_WAIT_MAX_SECONDS]`），hint 会写清"再等约 N 秒"；
- p90 超过单次上限时，hint 会明确说"这是单次上限，需要不止一次等待"；
- 新订阅或新 workflow 没有历史，这两个字段是 `null`——此时没有可依据的时长，不要编一个。

样本取的是**所有已终结**的 run（成功的和失败的）：问题问的是"多久结束"，二十秒就失败的一次
也是真实的结束时间，只统计成功会让常失败的工作流被低估。

### 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| `opencode mcp list` 显示 `needs authentication` | 没配 `oauth: false`，OpenCode 去做 OAuth 发现 | 补上 `oauth: false`，本服务只支持静态 Bearer |
| `connected` 但每次调用都 401 | 客户端 header 里的 key 与服务端 `CITRACKER_API_KEY` 不一致 | 两边用同一个值；从桌面图标启动的 OpenCode 读不到 shell 临时变量，可写进全局配置 |
| 连接被拒绝 / `fetch failed` | 8001 没有发布到宿主，或绑到了 `127.0.0.1` | 看 `docker compose ps` 的 PORTS 列；需要从别的主机访问时把 `CITRACKER_MCP_PORT_HOST` 打开并确认防火墙 |
| 容器 `Up` 但**所有调用 500**、`/readyz` 503 或 500 | 数据库连不上：`CITRACKER_DB_HOST` 是动态 DNS，IP 变了以后容器里缓存的旧地址失效。日志里是 `Failed to connect to PostgreSQL: ... server closed the connection unexpectedly` | 先 `docker compose ps` 看是否 `(healthy)`（compose 已给 web/mcp 配 `/readyz` 健康检查）；再在宿主上用同一账号 `Test-NetConnection <host> -Port <port>` 对比解析结果；**重启 web 和 mcp** 让它们重新解析，不需要重建镜像 |
| 容器 `(unhealthy)` | 同上，或数据库确实宕了 | `docker logs citracker-mcp-1 --tail 30`；数据库恢复后 `docker restart` 即可 |
| 日志里成片 `Failed to acquire connection within 30.0 seconds` | 连接池耗尽：`Pool stats` 里 `in_use≈max_size` 是并发饱和；`available=0, in_use=0` 是旧版本的许可泄漏 | 前者调大 `CITRACKER_POOL_MAX`（长轮询也占连接）；后者重启容器，并升级到含 `a55ebbd9` 的镜像。详见[连接池](#连接池一个请求占一条连接) |
| `docker compose pull` 之后 `up -d` / `docker start` / `docker rm -f` 全部卡住 | 本机 Docker Desktop 的容器运行时被卡住（与上文单文件 bind mount 那条不同，这条与挂载无关，`pull` 完新镜像后出现） | 退出并重启 Docker Desktop，再跑一次 `up -d`；不需要动 compose 文件 |
| webhook 日志里出现过 `ClientDisconnect` 的 ASGI traceback | GitHub 在发送中途放弃（它自己超时），旧版本把它当成未处理异常打了整段堆栈 | 含本次修复的镜像只记一行 `client disconnected before the body arrived`；GitHub 会重投，或下一次 `find_runs` 补齐，无需处理 |
| `init` 退出码 1、web/mcp 停在 `Created` | 新镜像一启动就失败（典型是 import 环：`ImportError: cannot import name ... from partially initialized module`）。`up -d` 只会说 init 失败，web/mcp 根本不会启动 | 先看 `docker logs citracker-init-1 --tail 40` 拿真实异常；修好重建镜像再 `up -d`。`ps` 里的 `Created` 不是"在跑"，是"没起来" |
| 工具报 `Error executing tool ...` | 这是**预期**的失败形态：MCP 层 `isError: true`，文本是 JSON 信封 | 读信封里的 `code` / `hint` / `action_url`。`repository_not_subscribed` 要运营方加仓库，不是你换参数能解决的 |
| `repository_not_subscribed` | 该仓库没订阅 | 按 `action_url` 加订阅，或在容器里 `repo subscribe --name owner/repo --credential pat/work` |
| `wait_for_run` 一直 `timed_out` | 任务比单次上限长 | 继续调；或调大 `CITRACKER_WAIT_MAX_SECONDS` 并同步放宽客户端执行超时 |
| `trigger_*` 被拒 | 令牌没有 Actions 写权限 | 看 `ci_status` 的 `can_trigger` 与 `trigger_scope`；权限问题由运营方处理 |
| 触发后不确定是否成功 | `trigger_workflow` 非幂等，响应不明确 | **先 `find_runs` 核对**，不要重试——重试会起第二次运行 |
| 日志看起来"就这么多" | 窗口被截断（`truncated: true`） | 用 `next_from_line` / `next_to_line` 续读，或换 `grep` 定位 |
| `get_job` 里某个 step `has_log: false` | 该 step 的文本没取回来：GitHub 提供按 step 拆分文件的时间窗（约一小时）已过，或当时归档里就没有分片 | 读同一个 job 的合并文本（`get_job_log(job_id=...)`）自己定位；已采集到的 step 不受影响，照常可读 |
| 怀疑线上还是旧代码 | `version` 是源码常量，区分不了构建 | `curl -k https://<域名>:18080/readyz` 看 `build.revision` / `build.number`，与 `git rev-parse HEAD` 对比；`null` 表示这个镜像没盖章（自建/本地构建），不是旧版本 |
| `search_job_log(step=N)` 返回 `reason` 而不是匹配 | 这个 step 没有采集到日志，所以**没搜过** | 改搜整 job（去掉 `step`），或用 `get_job_log(job_id=...)` 读合并文本 |

### 什么条件下能触发

**跟踪**（`recent_runs` / `find_runs` / `get_run` / `get_job` / `get_job_log` / webhook 入库）没有额外条件：
任何已订阅仓库的 run 都会进库。

**主动触发**（`trigger_workflow` / `rerun_run`）只有一个门：仓库已订阅，且绑定给它的凭据
带 Actions 写权限。没有白名单、没有每小时配额、没有操作日志表——本服务以"单操作者部署，
fine-grained PAT 的 scope 本身就是授权"为前提，触发动作一定来自人的明确指令。

代价必须清楚：拿到这个 MCP 服务的人可以触发 `publish.yml` 这类发版 workflow，也能在任意
分支烧 CI 分钟。要收紧，就收紧 PAT 的 scope，或不要把服务暴露给不该触发的人。

`trigger_workflow` 不是幂等的：响应不明确时先 `find_runs` 核对，重试会起第二次运行。

## 许可证

MIT
