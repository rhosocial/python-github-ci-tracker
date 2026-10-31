# 架构

面向对象分层 + 单向依赖：adapters → controllers → services → models。


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
