# 测试

## 需要一个真实的 PostgreSQL

测试套件把模型绑到真实服务上，每个测试拿到一份重建后的 schema——**不再使用 SQLite**
（2026-11-01 移除）。服务器由环境变量或共享场景文件指定，CI 用 service container。

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

## 本地测试范围

只跑与改动相关的部分：一次完整套件在远端库上约 3–5 分钟，在本地合规容器上更快。

```bash
PYTHONPATH=tests .venv3.15/Scripts/python -m pytest tests/core -q       # 模型与核心
PYTHONPATH=tests .venv3.15/Scripts/python -m pytest tests/controllers -q # 流程
```

## 静态检查

```bash
.venv3.15/Scripts/ruff check src tests
.venv3.15/Scripts/black --check src tests
.venv3.15/Scripts/isort --check-only src tests
```
