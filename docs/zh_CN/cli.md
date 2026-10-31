# 命令行

`rhosocial-github-ci-tracker` 与 `rhosocial-github-ci-tracker-db` 两个入口，全局参数
`--format`（`text` / `json`）与 `--verbose` 放在子命令**之前**。

```bash
rhosocial-github-ci-tracker --format json status            # 服务状态（ci_status 的 CLI 版）
rhosocial-github-ci-tracker runs --name owner/repo         # 列出最近的 run
rhosocial-github-ci-tracker latest --name owner/repo       # 最新一次 run
rhosocial-github-ci-tracker jobs --run-id <id> --step 3    # 某个 job 的 step
rhosocial-github-ci-tracker logs --job-id <id> --step 3    # step 日志
rhosocial-github-ci-tracker trigger --workflow ci.yml --ref main --name owner/repo
rhosocial-github-ci-tracker repo subscribe --name owner/repo --credential pat/work
```

## 数据库维护：`rhosocial-github-ci-tracker-db`

```bash
rhosocial-github-ci-tracker-db is-initialized        # 0=已初始化
rhosocial-github-ci-tracker-db init                  # 建表（幂等）
rhosocial-github-ci-tracker-db stats                 # 各表行数
rhosocial-github-ci-tracker-db usage                 # 各表体积与增长（待分区落地后完善）
rhosocial-github-ci-tracker-db cleanup --runs-before 90d --logs-before 365d
rhosocial-github-ci-tracker-db deliveries --unhandled --limit 20
```
