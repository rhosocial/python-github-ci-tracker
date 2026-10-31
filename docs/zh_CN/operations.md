# 运维

停机重启、探针、连接池、凭据、清理、验证。


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
