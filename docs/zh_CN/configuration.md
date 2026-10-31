# 配置

全部设置来自环境变量，服务没有配置文件。时区固定 UTC——连接作用域会执行
`SET TIME ZONE 'UTC'`，因为列是 `timestamptz`，返回值取自会话时区，而那是服务器的配置。

| 变量 | 默认 | 说明 |
|---|---|---|
| `CITRACKER_DB_HOST/PORT` | `192.168.1.3:16689`（compose 内 `postgres:5432`） | PostgreSQL 坐标 |
| `CITRACKER_DB_USER/PASSWORD/NAME` | `root` / `password` / `test_db` | 数据库账号 |
| `CITRACKER_DB_SSLMODE` | `prefer` | libpq sslmode |
| `CITRACKER_POOL_MIN/MAX` | `1` / `10` | 连接池。**每个在处理的请求占一条**，`wait_for_run` 等待期间一直握着 |
| `CITRACKER_API_KEY` | 空 | REST / MCP 的 Bearer；空表示不鉴权（启动告警），公网必须设置 |
| `CITRACKER_REST_HOST/PORT` | `0.0.0.0:8000` | REST 监听 |
| `CITRACKER_MCP_HOST/PORT` | `0.0.0.0:8001` | MCP 监听 |
| `CITRACKER_WEBHOOK_HOST/PORT` | `0.0.0.0:18080` | webhook 监听 |
| `CITRACKER_WEBHOOK_SECRET` | 空 | HMAC 密钥，与 GitHub webhook 配置一致 |
| `CITRACKER_WEBHOOK_URL` | 空 | 创建 webhook 时的默认回调 URL |
| `CITRACKER_PUBLIC_BASE_URL` | 空 | MCP action URL 的绝对前缀 |
| `CITRACKER_WEBHOOK_SSL_CERTFILE/KEYFILE` | 空 | webhook TLS；空表示明文 HTTP |
| `CITRACKER_DEFAULT_EVENTS` | `workflow_run,workflow_job` | 订阅的事件 |
| `CITRACKER_WAIT_MAX_SECONDS` | `300` | 一次 `wait_for_run` 的上限 |
| `CITRACKER_LOG_CAPTURE` | `1` | 是否采集 step 日志；`0` 全部退回整 job 合并文本 |
| `CITRACKER_LOG_CAPTURE_WINDOW_MINUTES` | `60` | run 结束后多久之内仍尝试采集（实测 GitHub 在 56–70 分钟之间压平） |
| `CITRACKER_LOG_CAPTURE_RETRIES` | `6` | 归档仍在写入时的重试次数 |
| `CITRACKER_LOG_CAPTURE_RETRY_DELAY_SECONDS` | `60` | 重试间隔（秒） |
| `CITRACKER_INITDB` | `auto` | `auto` / `force` / `skip` |
| `CITRACKER_INITDB_DIR` | `/docker-entrypoint-init.d` | init.d 目录 |
| `CITRACKER_LEGACY` | `0` | `1` 等价于 `INITDB=skip` |
| `CITRACKER_BOOTSTRAP_PAT` | 空 | 首次初始化注入的 PAT |
| `CITRACKER_BOOTSTRAP_PAT_FILE` | 空 | 从文件读 PAT，避免进入 `docker inspect` |
| `CITRACKER_BOOTSTRAP_PAT_NAME` | `default` | 凭据别名 |
| `CITRACKER_BOOTSTRAP_PROBE_REPO` | 空 | 验证 Actions 写权限用的 `owner/repo` |
| `CITRACKER_IMAGE` | 发布镜像 | compose 使用的镜像 |
| `CITRACKER_WEBHOOK_PORT_HOST` | `18080` | 宿主发布的 webhook 端口 |
| `CITRACKER_MCP_PORT_HOST` | `8001` | 宿主发布的 MCP 端口 |
| `CITRACKER_BUILD_REVISION` | 空 | 镜像构建时写入；`/readyz` 与 `ci_status` 回报 |
| `CITRACKER_BUILD_NUMBER` | 空 | 发布工作流的构建号 |
| `CITRACKER_BUILD_BUILT_AT` | 空 | 构建时间，ISO 8601 UTC |
