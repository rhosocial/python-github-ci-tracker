# controllers/ —— 一组流程

**控制器代表一组流程。** 一个模块是一个"这件事怎么做完"的单元：触发一次 workflow 并等到结果、
订阅一个仓库、应用一条 webhook 投递、回答一次查询。取数、调 GitHub、落库的顺序写在流程里，
协议有多少种都不重写。

```
adapters (MCP / REST / CLI)     只看得到流程
      ↓
controllers/                    一组流程 + 它用到的行操作为什么放一起
      ↓
services/                       能力：GitHub 各资源的接口（协议 + 实现）
models/                         每个模型封装自己的数据行为
```

## 两种类

| 类 | 是什么 |
|---|---|
| `TriggerController`、`QueryController`、`DispatchController`、`RepositoryController`、`RunController`、`WebhookController`、`WorkflowController`、`TokenController`、`StatusController` | 流程本身：一个方法就是一次完整的"问一次 / 触发一次 / 投递一次" |
| `RunData`、`RepositoryData`、`DeliveryLedger` | 流程内部的行层：唯一构造 ORM 查询的地方，跟拥有它的流程放在同一个文件里，免得每个调用方重新推导顺序 |

## 规则

1. **一次 `save()` 就是一次完整的写。** 没有 unit of work，没有跨模型事务，没有"两个模型必须一起提交"。
   一条投递是一个写序列，中途失败就停在那里；下一次读（以 GitHub 为准）把它补齐，台账记下这次没处理成功。
2. **控制器可以 import `models`，别处不行。** 适配层和 `services/` 都不碰模型。
3. **控制器之间可以互相调用**（`TriggerController` 用 `DispatchController` 和 `RunController`），
   因为一个流程复用另一个流程本来就是流程的含义。
4. **不写 SQL 字符串**，只用 `AsyncActiveRecord` / `AsyncActiveQuery` 的公开方法。
5. **不 import 适配层**；适配层的参数校验、错误码映射留在适配层。

## 判据（必须输出 0）

```powershell
# 适配层不得直接 import models
rg -n '^\s*from \.+models' -g '*.py' src/rhosocial/github_ci_tracker/adapters

# services/ 只剩 GitHub 能力，不得 import models
rg -n '^\s*from \.+models' -g '*.py' src/rhosocial/github_ci_tracker/services

# unit of work 的任何变体（模块、scope 助手、事务、投递锁、提交后延迟任务）
rg -n 'unit_of_work|write_scope|delivery_lock|\.transaction\(' -g '*.py' src/rhosocial/github_ci_tracker
```

最后一条由 `tests/core/test_no_unit_of_work.py` 守住。
