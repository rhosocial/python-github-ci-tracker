# 开发

本地虚拟环境与依赖。



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
