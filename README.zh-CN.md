# python-ci-tracker

[![CI](https://github.com/rhosocial/python-github-ci-tracker/actions/workflows/test.yml/badge.svg)](https://github.com/rhosocial/python-github-ci-tracker/actions/workflows/test.yml)
[![Python](https://img.shields.io/badge/python-3.15-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-en_US_%2F_zh__CN-green.svg)](docs/README.md)

**一个自建服务，回答"刚才那次 CI 到底怎么了"——通过 MCP、REST、CLI 三种接口。**

它读取 GitHub Actions 的 run、job、step 与日志：后台没有轮询，不维护中央令牌池，
GitHub 还能回答时，绝不用缓存充当真相。

[English](README.md) | 简体中文

---

## 你能得到什么

| | |
|---|---|
| **一次调用，协议不限** | `ci_status`、`find_runs`、`get_run`、`get_job`、`get_job_log`、`search_job_log`、`wait_for_run`、`trigger_workflow`——MCP（Streamable HTTP）/ REST / CLI 三处同构。一条流程，三种形态；模型和脚本拿到的是同一份答案 |
| **三层对象，一次一层** | `get_run` 指出失败的 job，`get_job` 指出失败的 step，`get_job_log` 给出它的文本。没有哪个调用一次答两层，调用方也就不必判断自己在看哪一层 |
| **step 级日志，存得住** | GitHub 只在 run 结束后约一小时提供按 step 拆分的文件，之后同一接口只剩合并文档。服务趁窗口存在时抓取、以普通行入库、之后永久可读。没抓到就如实报告，绝不用别的 step 文本顶替 |
| **窗口只寻址，不解释** | `tail`、显式行号区间，或你自己的正则。不猜测试摘要、不猜收尾日志——通用跟踪器不知道任何项目会打印什么 |
| **触发与等待，不白烧分钟** | `trigger_workflow` 按请求派发；`if_absent=true` 表示"这个提交已经跑过"，不再起第二次。`wait_for_run` 是有界长轮询，超时预算来自该 workflow 自己的历史 |
| **明确只支持 PostgreSQL** | 日志表按月分区，清旧数据是一次 `DROP TABLE`；schema 由服务自己的渲染器生成；测试套件跑在真实数据库上 |

## 为什么用

三件不同的事，第三件最容易被低估。

- **上下文**：四千行的 job 日志变成 `grep` 加几行上下文，12 job 的矩阵变成 12 行名册。
  有些日志根本装不进上下文窗口，所以这不是"便宜一点"，而是"原本做不了"。
- **时间**：`wait_for_run` 是一次有界调用而不是轮询循环；`cancel_run` 与
  `if_absent=true` 省下本来会白烧的分钟；`trigger_workflow` 还回报 p50/p90，
  让你决定先等哪个。
- **错误结论**：`attempt`/`is_latest` 把容量抖动和坏掉的后端分开；`has_log` + `reason`
  把"没有输出"和"没有采集到"分开；`ci_status.health.alerts` 把自己的静默降级变成
  可执行清单。

详细版——含一位使用者实测的会话数据，以及我们用在自己身上看到的——见
[它省下什么](docs/zh_CN/value.md)。

## 细节在哪里

- [文档（简体中文）](docs/zh_CN/README.md)——部署、接口、CLI、开发
- [Documentation (English)](docs/en_US/README.md)

## 许可

[Apache License 2.0](LICENSE)。
