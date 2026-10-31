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

- **给 agent**：一次调用回答一个完整问题，并点名下一次调用。失败的 job 会告诉你
  哪一步错了、日志是否已在库——不用翻 shell，不用另起服务。
- **给跑矩阵的团队**：77 个 job 的 run，一次 `get_run`；失败 step 的日志一次返回，默认
  框在最后 200 行。
- **给免费层数据库用户**：容量可见（`db usage`），清理是丢分区，而不是给表上锁的
  `VACUUM`。

## 细节在哪里

- [文档（简体中文）](docs/zh_CN/README.md)——部署、接口、CLI、开发
- [Documentation (English)](docs/en_US/README.md)

## 许可

[Apache License 2.0](LICENSE)。
