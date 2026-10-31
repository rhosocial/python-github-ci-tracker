# 日志与窗口

## 两种读取，两个作用域

```
get_job_log(job_id)                 整个 job 的合并文本（ct_job_logs）
get_job_log(job_id, step=N)         第 N 个 step 自己的文件（ct_step_logs）
get_job_log(job_id, failed_steps_only=true)   全部失败 step 的文本
search_job_log(job_id, query)       在合并文本里搜，返回行号与上下文
search_job_log(job_id, query, step=N)   在某个 step 的文本里搜
```

`search_job_log` 与 `get_job_log` 用同一个 `step` 参数：返回里的 `source`
（`job` / `step`）说明行号属于哪个作用域，照它把 `from_line` 传回去即可。
step 没采集到时返回 `reason`（`capture_closed` / `not_a_step_archive` …），**不把
"没文档"说成"没匹配"**。

## 窗口只寻址，不解释

| 参数 | 语义 |
|---|---|
| 不传 | 文档最后 200 行 |
| `tail=N` | 最后 N 行 |
| `from_line` / `to_line` | 显式区间（1-based，闭区间） |
| `grep` | 调用方给的正则（非法时退化为字面量）+ `context` 行上下文 |

没有测试摘要锚定、没有错误标记、没有"跳过 runner 收尾日志"——这些是某个项目的输出特征，
通用跟踪器猜了就是错的。

## 读 step 不会触发采集

`get_job_log(step=N)` 只回答已存的行。要再抓一次，用
`get_job(job_id, refresh_logs=true)`；它只在 GitHub 仍提供 step 分片时有意义
（`capture.window_closes_at` 之前）。

## 容量与清理

- [部署](deployment.md) 里 `db usage` 看当前占用；
- 日志表按月分区，`db partitions --drop-before <N>m` 丢整月数据（不锁表、不需要 VACUUM）。
