# 接口

MCP 的三个入口；REST 与 CLI 同构。


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
