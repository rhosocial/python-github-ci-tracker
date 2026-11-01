# docker/init.d

运营初始化脚本目录。`docker-compose.yml` 把它只读挂载到容器的
`/docker-entrypoint-init.d`，镜像入口在**首次启动**时按文件名顺序执行其中的 `*.sh`：

| 脚本 | 作用 |
|---|---|
| `10-schema.sh` | 建库建表（`rhosocial-github-ci-tracker-db init`，幂等） |
| `20-pat.sh` | 配置了 PAT 时新增或原地轮换凭据（`credential pat ensure`） |

要点：

- “首次启动”由数据库状态判定（`rhosocial-github-ci-tracker-db is-initialized`），
  不看容器文件系统；`web` / `mcp` 不参与初始化。
- 脚本是**部署数据**，不烘进镜像；可以按需增删、改写。
- 任一脚本失败，`init` 失败，`web` / `mcp` 不会带着未初始化的库启动。
- 首次启动之后需要重跑：`docker compose run --rm -e CITRACKER_INITDB=force init`。
- 完全自行管理 schema 与凭据：`CITRACKER_INITDB=skip`（或 `CITRACKER_LEGACY=1`）。

完整说明见 `../README.md` 与仓库根目录 `README.md` 的“部署”一节。
