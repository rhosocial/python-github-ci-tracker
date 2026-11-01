# python-ci-tracker 项目规则

## 项目定位

GitHub CI 状态跟踪与自动化服务。核心价值是将"触发 CI → 等待 → 取回结果"的循环自动化，
让大模型和第三方工具可以简单地异步调用，稍后查询结果。

## 架构原则

1. **GitHub 是 run 状态的源头，数据库是它的缓存与历史**：run 级问题（存在性、状态、列表、latest）每次都回源 GitHub，接口不可访问就报错，绝不降级返回缓存；响应与本地不同则写回。服务实例无状态
2. **用户自带令牌**：每个用户提供自己的 GitHub Fine-grained PAT，系统加密存储，不维护中心令牌池。进程没有 ambient PAT 时，按 `ct_repository_bindings` 逐仓库解析；未绑定即匿名读
3. **单向分层**：`adapters（MCP / REST / CLI）→ controllers → services → models`，只允许向下调用
4. **被动式双通道**：webhook 推送只降低延迟、补历史，不是权威来源。无后台轮询、无定时任务、无队列
5. **探针分开**：`/livez` 只证明进程活着（**零 I/O**，失败才该重启）；`/readyz` 证明这个实例现在能接请求（**一次数据库往返**，失败只摘流）。`/health`、`/healthz` 是 `/readyz` 的别名。数据库只进 readiness；GitHub 两个都不进（第三方抖动不该摘掉全部实例，探针还会烧配额，GitHub 可达性归 `ci_status`）

### 探针

| 路径 | 问什么 | 检查 | 失败后果 |
|---|---|---|---|
| `/livez` | 进程活着、事件循环能响应 | 无 | 重启容器 |
| `/readyz`、`/healthz`、`/health` | 这个实例能接请求吗 | `Database.ping()`（`SELECT 1`，2 秒上限） | 摘流，不重启 |

- 探针路径豁免 API key，也豁免每请求的连接作用域（否则 liveness 会被连接池饱和连累成重启循环）。
- schema 一次性在启动时检查（`missing_tables`），结果写进 `/readyz` 的 body，但**不参与就绪判定**：建表是 `init` 的事，摘流不会把表建出来。
- ambient PAT 缺失、台账为空都**不算** not-ready：前者是按仓库绑定的正常模式，后者只是还没收到投递。
- `/livez`、`/readyz` 与 `ci_status.service` 都带 `build` 块（`revision` / `short_revision` /
  `number` / `built_at`）。`version` 是源码常量，区分不了两次构建；判断“线上是不是我刚推的
  那次”只能看 `build.revision`。它来自镜像环境变量（构建时由 `images.yml` 传入），
  **`null` 表示这个镜像是没盖章的（本地/自建），不是旧版本**。

### 读取规则（缓存一致性）

| 问题 | 回答来源 |
|---|---|
| run 的存在性 / 状态 / 列表 / latest | 每次都读 GitHub；读不到即报错 |
| attempt / job / step / log | 问了才读；已终结的执行（含其日志）是终态，此后缓存即答案——**但 step 分片例外**，见下 |
| "webhook 通道活不活" | 投递台账 `ct_webhook_delivery`，只回答通道自身 |

webhook 可能丢失，run 也常在本系统之外创建、第一次被问到；所以正确性不能依赖它。
`find_runs` 这类读路径本身就能补齐漏收的 webhook。ETag 条件请求让"每次回源"不烧配额
（304 不计入额度）。

- **wait_for_run 不是后台轮询**：它是单次调用内的同步等待，逐轮读 GitHub，直到终态或超时。
- **不做的**：后台定时轮询、任务队列、轮询终止能力。

### 日志：一份合并文本，和一份限时才能取回的分片

GitHub 有两个日志来源，**不是一回事**：

| 来源 | 内容 | 可得性 |
|---|---|---|
| `GET /actions/jobs/{job_id}/logs` | 一个 job 的合并文本 | 90 天内随时可读，原样进 `ct_job_logs`（**流程未变**） |
| `GET /actions/runs/{run_id}/logs` | zip：合并文件 + `<job>/<step 号>_<step 名>.txt` + `system.txt` | 合并文件与上面**逐字节相同**；按 step 拆分的文件只由 GitHub 在 run 结束后约 **1 小时**内提供 |

因此：

- **一小时是取回的截止时间，不是保存期限。** 取回并写进 `ct_step_logs` 的文本就是普通数据，
  永久可读，任何读路径都不看这个窗口；窗口只决定"还能不能再补采"。
- **step 日志是采集，不是读取**。窗口外同一接口只返回合并文件，step 边界不可恢复；
  采集发生在 run 完成投递（`workflow_run` completed）与窗口内的读取请求，两者都是尽力而为，
  失败只记日志，不影响投递本身。**不在 `workflow_job` 完成时抓**：归档是 run 级的，
  run 结束前该端点一律 404（实测 12 job 矩阵，每个 job 完成都 404）。
- 归档文件异步写入，run 刚结束可能只有几份；读取路径重试（每次重新拉取，间隔 60 秒，
  最多 6 次），404/502/503 视为"还没好"同样重试。webhook 不做重试，免得占住投递。
- 重试用尽后仍缺时，`get_job(refresh_logs=true)` 是显式的再试入口（同样 6 次预算）；
  `get_job` 的 `capture` 计数是**日志文件数**（`expected_logs` / `captured_logs`），不是 step 数：
  skipped 与从未开始的 step 在 GitHub 侧也没有文件。
  `capture.state` 为 `partial` 表示还可以再等，`closed` 表示 GitHub 已停止
  提供这个 run 的 step 分片、再试也补不回来（已采到的照常可读）——这两个状态就是给
  调用方判断"要不要再试"的。措辞上不要说成"日志过期"：过期的是 GitHub 的提供窗口，
  不是我们存下来的行。
- `ct_step_logs` 与 step 一对一、**允许缺失**；缺行是答案（"没赶上采集"），不是可重试的 cache miss。
  读 step 时缺行必须如实报 `steps_without_logs` 与原因，**绝不拿别的 step 的文本顶替**。
**从 run 走到失败 step 的日志，只有一条路**（三层对象，一层一个工具，不重复）：

```
get_run(run_id)            → run 自身 + job 名册：job_id / name / status /
                             conclusion / duration_seconds / failed
get_job(job_id)            → 一个 job + 它的全部 step：状态、结论、起止、耗时、
                             failed、has_log / log_lines
get_job_log(job_id, step=N)→ 该 step 的原文
get_job_log(job_id)        → 整个 job 的合并文本（has_log=false 时的退路）
```

- **steps 只属于 `get_job`**：`get_run` 不接受 `include_steps`，也不返回 failure 块；
  日志摘录同理，只在 `get_job_log`。run 级调用不会随矩阵规模膨胀，也不会出现两种拿 step 的方式。
- `get_run` 的 `jobs[].failed` 是唯一需要的导航字段；hint 直接给出 `get_job(job_id=…)`。
- `get_job` 的 hint 直接给出 `get_job_log(job_id=…, step=N)`；该 step 没采集到日志时，
  改指整个 job。
- `get_job_log(failed_steps_only=true)` 保留：一次拿全部失败 step 的文本，省掉多次调用；
  它是批量便利，不是第二套"哪个 step 失败"的判定。
- `search_job_log` 与 `get_job_log` **同一个作用域参数**：不带 `step` 搜合并文本，带 `step=N`
  搜该 step 的文本；返回里的 `source`/`step` 说明行号属于哪个作用域。step 没采集到时
  报 `reason`（`capture_closed` / `not_a_step_archive` ...），**不把"没文档"说成"没匹配"**。
- 全绿的 job 不催着读日志——`has_log` 仍如实为 `true`，数据在那儿，只是 hint 不推。
- 失败判定只认 `FAILED_CONCLUSIONS`（failure / cancelled / timed_out / startup_failure /
  action_required / stale）：**`conclusion = null` 不算失败**，那是 GitHub 元数据还在生成中，
  "不是 success 就算失败"会虚报。

**step 日志是尽力而为的**：采集到就存，采集不到就不存。一个 job 的所有 step 都没有行时，
  读路径返回空 `sections` + `reason`，调用方的下一步是**读同一个 job 的合并日志**
  （`ct_job_logs` / 不带 `step`、`failed_steps_only` 的 `get_job_log`）自己分析——
  而不是指望我们再猜一次 step 边界。部分 step 有、部分没有时，同样只报有行的那几段。
- `system.txt` 是 runner 的调度记录（`Requested labels` / `Job defined at`），**不入库**。
- 判别归档形态只看结构：目录里有没有 `<step 号>_*.txt`。`system.txt` 与合并文件两种形态都有，
  不能当信号。
- 合并视图（`get_job_log` 不带 `step` / `failed_steps_only`）走的是 `ct_job_logs`，行为与引入
  step 日志之前完全一致。

### 连接池：一个请求占一条连接

连接作用域是**每请求**的（REST 中间件、MCP 包装层各起一个），所以：

- 每个正在处理的请求占 `CITRACKER_POOL_MAX`（默认 10）里的一个名额；
- **长轮询也算**：`wait_for_run` 在等待期间一直握着自己的连接，300 秒的等待就是 300 秒的占用；
- 因此 `CITRACKER_POOL_MAX` 必须覆盖"并发长轮询 + 并发普通请求"，否则后来者会在 `acquire` 上排队，
  30 秒后得到 `Failed to acquire connection within 30.0 seconds`。

**2026-10-30 的事故**：外部 PostgreSQL 在 10:20:28Z 终止了所有后端（`terminating connection due to
administrator command`），`/readyz` 的 2 秒预算在连接仍在建立时取消了 `acquire`，而库里的
`acquire` 只 `except Exception`——`CancelledError` 是 `BaseException`，于是**每取消一次就漏掉一个
许可**。十来个探测之后池子报 `available=0, in_use=0`，此后每个请求都等满 30 秒再 500，直到重启容器。
修复在 `python-activerecord` 的 `a55ebbd9`（本镜像从 `release/v1.0.0.dev30` 安装该库）；
在此之前若再遇到同样的日志，重启 web/mcp 容器即可恢复。

### 触发：默认照办，去重要显式要

`trigger_workflow` 默认就是**触发**——调用方说触发，就花 CI 分钟。要"确保这个提交跑过"而不是
"再起一次"，传 `if_absent=true`：按 workflow + sha 查 GitHub（不是查缓存，别人起的 run 恰恰可能是
缓存里没有的那个），已有则返回它并带 `dispatched: false`，一个 run 都不新起。MCP / REST / CLI 三处
参数同名（CLI 为 `--if-absent`）。

sha 从哪来：优先用调用方传的 `commit_sha`；不传才去读 `ref` 的 head。**读 head 需要 Contents read**，
只授了 Actions 的 fine-grained PAT 读不到（`Resource not accessible by personal access token`），
这时报 `invalid_argument` 并明说要传 `commit_sha`——**绝不因为查不了就照旧 dispatch**，那正是它要防的事。

**"已经跑过"指同一个 workflow**：同一 sha 上 `test.yml` 与 `images.yml` 各是各的，只有请求的那个
workflow 有 run 才算数。

### 连接池是健康的一部分

`ci_status` 的 `health.pool` 报 `in_use` / `available` / `max_size` / `acquire_timeouts`。
`available = 0` 且 `in_use <= 1`（读它的这次调用自己占一条）而 `max_size > 1` 是**许可丢失**而不是饱和：
等待不会恢复，只能重启——2026-10-30 的事故正是这个形状，而当时从外部看服务一切正常。
（不要用 `in_use = 0` 判定：这个调用本身必然持有连接，那个条件永远不会成立。）

### 时间与"最新"的语义

| 字段 / 参数 | 含义 |
|---|---|
| `created_at` | **GitHub 的创建时间**（attempt 的 `github_created_at`），不是本地入库时间 |
| `stored_at` | 本地入库时间，只回答"这个进程最近写了多少"，不要当成 run 的年龄 |
| `started_at` / `completed_at` | GitHub 的执行时间 |
| `latest`（`find_runs` / `/latest-run` / CLI `latest`） | **最新一次 run，不限分支**。要 main 就显式 `branch=main`；隐式回落到默认分支会安静地返回一条陈旧答案 |

**一条 run 必须自解释。** run 的返回（`find_runs` / `recent_runs` / `get_run`）带四组身份信息，缺一样
调用方就会做错决定：

| 组 | 字段 | 回答什么 |
|---|---|---|
| 哪个 workflow | `workflow: {name, path}` | 同一 sha 上常常有两个 run（`test.yml` + `changelog-check.yml`），只有这个能区分 |
| 哪个提交 | `commit: {sha, message, author, committed_at}` | 不用再回 shell 跑 `git log`；`author` 是写代码的人，`actor` 是触发的人 |
| 哪个 PR | `pr_number`、`base_ref` | 从 run 反查它属于哪个 PR、目标分支是什么 |
| 代码从哪来 | `head_repo`、`display_title` | fork PR 的代码不在本仓库；标题是 GitHub 给这次 run 的一句话 |

**`base_ref` 是补出来的，不是 run 自带的。** run 的 `pull_requests` 里才有 `base.ref`，而 GitHub
在 PR 关闭后就不再随 run 返回它——于是同一条 run 会出现 `pr_number: 127` 配 `base_ref: null`，
读起来像数据坏了。读单条 run（`get_run`）时若 `pr_number` 有值而 `base_ref` 为空，就向该 PR
查一次并写回（`QueryController._fill_base_ref`，尽力而为、失败不影响这次读），此后不再产生调用；
`find_runs(pull_request=N)` 走的是同一修复，因为它本来就把 PR 拿在手里。

`ct_workflows.workflow_id = 0` 表示**未解析**，永远不等于"全部 workflow"：按 workflow 过滤时，
0 必须回源解析并写回；解析不到就报 `workflow_not_found`，静默去掉过滤会让调用方拿到全量还以为是筛选结果。

**等待预算来自历史，不是猜测。** `wait_for_run` 的 `timeout_seconds` 由该 workflow 最近完成 run 的
p90 决定：`trigger_workflow` / `rerun_run` 返回 `typical_duration_seconds`（`samples` / `p50` / `p90`），
超时的 `wait_for_run` 返回 `observed`（同样三项）与 `suggested_timeout_seconds`（p90 向上取整，
限制在 `[10, wait_max_seconds]`）。样本取**所有已终结**的 run，不只成功的：问题"多久结束"里，
二十秒就失败的那种也是一次真实的结束时间，只取成功会让常失败的工作流被低估。
新订阅 / 新 workflow 没有历史时，这两个字段是 `null`——**不要编一个数**，`hint` 会说明原因。

403 不都是权限问题：`This workflow is already running` 一类是 run 的状态冲突（`run_in_progress`），
只有真正的权限拒绝才是凭据问题。分类发生在 HTTP 客户端（`handle_error`），因为一旦抛成
`TokenPermissionError`，后面的 `map_api_error` 只会原样透传。

`ci_status` 的 `health.ingest` 用 `runs_added_last_5m/1h` 与 `backfill_suspected` 区分"还在回填"和"数据没了"。

### 分层：Model / Service / Controller / App

四个角色各有明确含义，**不要混用它们的名字**：

```
┌─────────────────────────────────────────────────────┐
│  App（adapters/*/app.py、container.py）             │
│  - 装配：设置、连接作用域、后台任务、路由、异常映射   │
│  - 把 Container 交给适配层，不构造业务对象           │
└─────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────┐
│  适配层（adapters/mcp、rest、cli）                   │
│  - 参数绑定/验证、认证接入、序列化、错误码映射        │
│  - 只调 controller，不碰模型                         │
└─────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────┐
│  Controller（controllers/）                          │
│  - **一组流程**：触发、查询、订阅、投递、令牌…        │
│  - 取数、调 GitHub、落库的顺序写在这里                │
│  - 唯一写 ORM 查询的地方                             │
└─────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────┐
│  Service（services/github/）                         │
│  - **能力封装**：GitHub 各资源的接口（协议 + 实现）  │
│  - 没有问题流程、没有存储、不 import models          │
└─────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────┐
│  Model（models/）                                    │
│  - 只封装自己的数据行为：表映射、状态机、payload 解析 │
│  - 叶子，不反向依赖任何层                            │
└─────────────────────────────────────────────────────┘
```

**关键推论**：

1. **新增协议零改动核心**：加 GraphQL 或 gRPC 只加适配层
2. **流程只写一份**：判断与顺序在 controller，能力在 service，不因协议重复
3. **测试分层**：业务核心测试一份，协议适配层只测转换正确性

### 硬规则（由测试守住）

| 规则 | 守住它的测试 |
|---|---|
| **禁止 unit of work**：没有跨模型事务、没有"这些模型一起提交"的 scope、没有提交后延迟任务，也没有为它们服务的锁与诊断 | `tests/core/test_no_unit_of_work.py` |
| 适配层不得在运行时 import `models` | `tests/adapters/test_no_model_access.py` |
| `services/` 不得在运行时 import `models`（它只封装能力） | 同上 |
| 必须能 `save()` 一次就是一次完整写；一条投递中途失败就停在那里，由下一次读修复 | 代码评审 + `tests/core/test_webhook_idempotence.py` |

**unit of work 是反模式，全局禁止出现。** 本项目的持久化语义是 ActiveRecord：

- 一个模型写自己的一行，写完整即完成；
- run / attempt / job / step 是一条投递里的**写序列**，不是原子单元。第 3 步失败就停在第 3 步，
  第 4 步不执行；下一次读（以 GitHub 为准）把缺的补上，投递台账记下"这次没处理成功"。
- 因此不需要 `transaction()`、`write_scope()`、`delivery_lock()`、`pending_*` 队列、
  事务状态诊断。出现其中之一，说明思路回到了 unit of work，应当删除而不是迁移。

> **看到 `TransactionError: No active transaction to rollback` 时先看时间。** 这条消息只可能来自
> ORM 的 `TransactionManager.rollback()` 在 level 0 上被调用，而本仓库没有任何代码开启事务
> （`tests/core/test_no_unit_of_work.py` 守住整个事务 API 面）。台账里 2026-10-30 06:13Z 之前的
> 4 条是拆除 UoW 之前的旧镜像留下的；之后的 80 条投递 0 失败。若将来再出现，那不是本项目的调用，
> 而是库的兜底掩蔽缺陷：`AsyncBackendPool.transaction()` 的 `except` 会用 rollback 的
> `TransactionError` 覆盖原始异常，真正的错误（例如重复插入的 `IntegrityError`）被藏起来了。

## 技术栈

- **ORM**: rhosocial-activerecord（PostgreSQL 后端）
- **MCP 基础层**: rhosocial-mcpserver
- **身份/RBAC**: python-user（可选集成）
- **Python**: 3.10+

## 代码规范

1. **文件头注释**: 每个 .py 文件必须以路径注释开头（`# src/rhosocial/github_ci_tracker/xxx.py`）
2. **行宽限制**: 100 字符
3. **类型注解**: 使用 Python 3.8+ 兼容语法
4. **Sync/Async 对称**: 同步和异步 API 方法名完全一致，类名加 `Async` 前缀
### 同步与异步：谁对等，谁只有异步

**服务本身端到端异步**：adapters（REST/MCP 天然异步，CLI 用 `asyncio.run` 起一个循环）
→ controllers → services 的资源组（`GithubRuns` / `GithubRepositories` / `GithubWebhooks` /
`GithubCredentials`）→ `AsyncGithubClient` → `AsyncX` 模型。控制器里没有同步的 I/O 方法，
同步 `def` 只出现在 `__init__`、纯谓词和格式化助手上。

对等只存在于两层：

| 层 | 对等 | 说明 |
|---|---|---|
| `models/` | **是**，11 对 `X` / `AsyncX` | 关系加载器也有 `OrderedHasMany` / `AsyncOrderedHasMany`。同步孪生是给库外使用者保留的，**本服务不调用** |
| `services/github/client.py` | **是**，`GithubClient` / `AsyncGithubClient` | 唯一使用同步版的地方是调试命令 `adapters/cli/github_debug.py`（它会在事件循环里阻塞，仅调试用） |
| services 资源组 / controllers / adapters | **否，只有异步** | 协议（`RunReader` 等）就是异步的；不要期待存在 `SyncGithubRuns` |

`tests/models/test_sync_twins.py` 守住两件事：同步孪生能跑通一条完整链路（repo → … → step log），
以及每个同步类都有同表的 `Async` 孪生。新增实体时两半都要写，否则这条测试会失败。

5. **行结束符**: 使用 LF（Unix 风格）
6. **面向对象**: 领域逻辑放在对象上，不写成模块级自由函数。模块级函数只允许是协议适配层的
   路由/tool/命令处理器，或纯粹的格式化助手

## 测试规范

- 业务核心测试：协议无关，只写一份
- 协议适配层测试：每个协议各一份，只覆盖差异化部分
- 本地测试只涉及当前改动部分，单次任务不超过 180 秒

## 目录结构

```
src/rhosocial/github_ci_tracker/
├── models/          # M：表映射、状态机、payload 解析（叶子）
├── services/        # S：能力封装，目前只有 github/（协议 + REST 客户端）
├── controllers/     # C：一组流程 + 它用到的行操作（run/repository/webhook）
├── views.py         # V：协议无关的返回数据
├── config.py        # 设置：环境变量 → dataclass
├── database.py      # 连接池与每协程连接作用域
├── container.py     # 组合根：一次构造 settings + database + controllers
└── adapters/        # 协议适配层（每个协议一个 App）
    ├── mcp/         #   app.py + tools/ + resources/
    ├── rest/        #   app.py + routes/ + schemas/
    └── cli/         #   main.py + commands/ + eventloop.py + db.py
```

`credentials/`（凭据 provider，自带表）与 `schema/`（DDL）是横切能力包，由 controller 使用。
