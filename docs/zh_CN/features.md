# 功能

## 三层对象，一层一个工具

从一次失败的 run 走到失败的 step，只有一条路：

```
get_run(run_id)              → run 本体 + job 名册（job_id / name / 状态 / 结论 / 耗时 / failed）
get_job(job_id)              → 一个 job 的全部 step（状态、结论、起止、耗时、failed、has_log）
get_job_log(job_id, step=N)  → 该 step 的原文
get_job_log(job_id)          → 整个 job 的合并文本（has_log=false 时的退路）
```

- `steps` 只属于 `get_job`：`get_run` 不接受 `include_steps`，也不返回 failure 块；
  日志摘录同理，只在 `get_job_log`。
- `get_job_log(failed_steps_only=true)` 保留为批量便利：一次拿全部失败 step 的文本；
  它是"省掉多次调用"，不是第二套"哪个 step 失败"的判定。
- 全绿的 job 不催着读日志——`has_log` 仍如实为 `true`，数据在那儿，只是 hint 不推。

## 两个日志来源

| 来源 | 内容 | 可得性 |
|---|---|---|
| `GET /actions/jobs/{id}/logs` | 一个 job 的合并文本 | 90 天内随时可读，存进 `ct_job_logs` |
| `GET /actions/runs/{id}/logs` | zip：合并文件 + `<job>/<step 号>_<step 名>.txt` | 按 step 拆分的文件只由 GitHub 在 run 结束后约一小时内提供 |

**一小时是取回的截止时间，不是保存期限。** 取回并写进 `ct_step_logs` 的文本就是普通数据，
永久可读；截止时间唯一决定的是"还能不能再补采"。

## 引用一个 workflow：path、文件名、数字 id

显示名不是引用。`name:` 写在 workflow 文件里，所以同一个文件可以在不同分支有不同名字
（`vistart/Dockerfiles` 的 `build_and_deploy.yml` 同时叫 "php - Build and Deploy"、
"vlmcsd - Build and Deploy"、"ubuntu - Build and Deploy"）。用 path 或 id。

## 失败判定

只认 `FAILED_CONCLUSIONS`（failure / cancelled / timed_out / startup_failure /
action_required / stale）：`conclusion = null` **不算失败**，那是 GitHub 元数据还在生成中。

## webhook 是降延迟，不是权威

webhook 可能丢失，run 也常在本系统之外创建；所以 run 的存在性 / 状态 / 列表 / latest
每次都回源 GitHub，读不到即报错，绝不降级返回缓存。
