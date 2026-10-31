# 部署

两种部署形态，外部 PostgreSQL 与一体化。

### 形态一：web + mcp 双容器（外部 PostgreSQL）

```bash
cp docker/stack.env.example docker/stack.env
# 编辑 docker/stack.env：webhook secret、API key、数据库、PAT
mkdir -p docker/certs && cp <fullchain.pem> <privkey.pem> docker/certs/

docker compose --env-file docker/stack.env up -d
```

启动顺序由 `depends_on` 保证：`init` 建库建表并处理 PAT → `web` 与 `mcp` 启动。数据库可以是
任意 PostgreSQL（本仓库的开发库是 `192.168.1.3:16689`），不要求和应用同机。

### 形态二：一体化 `postgres:18`

在形态一的基础上叠加 `docker-compose.bundled.yml`：

```bash
docker compose -f docker-compose.yml -f docker-compose.bundled.yml \
  --env-file docker/stack.env up -d
```

这个 overlay 只增加官方 `postgres:18` 服务，并把 `init` / `web` / `mcp` 的
`CITRACKER_DB_HOST` 固定为 `postgres`，因此 `stack.env` 里残留的外部数据库地址不会误生效。
数据在命名卷 `pgdata`；`docker compose ... down` 保留数据，`down -v` 删除。

### 首次启动初始化（init.d）

数据表模式与 PAT 是**运营数据**，不是镜像里的代码逻辑。镜像只提供一个通用 runner；真正的
初始化脚本在 `docker/init.d/`，由 compose 只读挂载到 `/docker-entrypoint-init.d`，机制与 MySQL
镜像的 `/docker-entrypoint-initdb.d` 相同。

| 脚本 | 作用 |
|---|---|
| `docker/init.d/10-schema.sh` | `rhosocial-github-ci-tracker-db init`：库不存在则先建库，再建全部表（幂等） |
| `docker/init.d/20-pat.sh` | 配置了 PAT 时执行 `credential pat ensure`：不存在则新增，存在则原地轮换 |

“首次”由数据库状态判定，而不是容器文件系统：runner 先执行
`rhosocial-github-ci-tracker-db is-initialized`，全部 `ct_*` 表都在就跳过脚本，否则按文件名
顺序执行。容器重建、镜像升级都不会重复初始化；脚本任一失败则 `init` 失败，`web` / `mcp`
不会带着未初始化的库启动。

`20-pat.sh` 从环境读取（`docker/stack.env`）：

```dotenv
CITRACKER_BOOTSTRAP_PAT=github_pat_xxx
CITRACKER_BOOTSTRAP_PAT_NAME=work
CITRACKER_BOOTSTRAP_PROBE_REPO=owner/repo
```

`credential pat ensure` 的语义：

1. 数据库里没有 `pat/<name>` → **新增**；
2. 已存在 → **原地轮换**（保留 id 与所有仓库绑定，不需要重新订阅）；
3. 两条路径都先让 GitHub 验证令牌，验证失败则启动失败，不会把坏令牌写进数据库；
4. 设置了 `CITRACKER_BOOTSTRAP_PROBE_REPO` 时，额外探测该仓库的 Actions 写权限，
   结果决定 `can_write_actions`（触发 CI 的前提）。

令牌需要的权限：`Actions: Read and write`、`Metadata: Read`；需要自动创建 webhook 时再加
`Administration: Read and write`。

不想让令牌出现在 `docker inspect` 时，用文件形式：把令牌放进一个**目录**，把该目录挂进 `init` 容器，并设置 `CITRACKER_BOOTSTRAP_PAT_FILE`，例如
`CITRACKER_BOOTSTRAP_PAT_FILE=/run/secrets/citracker-pat`。脚本和 CLI 都支持
`--token-file` / `--token-stdin`，令牌不会出现在进程参数里。

> **挂目录，不要挂单个文件。** 在 Docker Desktop（Hyper-V 后端）上，单文件 bind mount 会让容器在宿主机路径不可达时变成**无法删除**的状态：`docker rm -f`、`docker compose down`、`docker start` 全部阻塞，只能重启引擎。目录挂载没有这个失败模式。本仓库的本地覆盖文件就是按目录挂载的：

```yaml
volumes:
  - ./docker/secrets:/run/secrets:ro      # 目录，不是 ./docker/secrets/citracker-pat
```

本仓库的部署同样走这条路径：`CITRACKER_BOOTSTRAP_PAT_NAME=work`、
`CITRACKER_BOOTSTRAP_PROBE_REPO=rhosocial/python-github-ci-tracker`，首次启动即自动新增。

#### 重跑初始化

首次启动之后脚本不会自动再跑。改过脚本，或首次启动时还没配 PAT、之后才补上令牌时：

```bash
docker compose run --rm -e CITRACKER_INITDB=force init
```

也可以往 `docker/init.d/` 里加自己的 `*.sh`（按文件名排序，任一失败即停止）。这些脚本是部署
数据，不会被烘进镜像。

手工方式（不使用 init.d 时）：

```bash
docker compose exec web rhosocial-github-ci-tracker credential pat add \
  --name work --token <PAT> --probe-repo owner/repo
docker compose exec web rhosocial-github-ci-tracker credential pat list
docker compose exec web rhosocial-github-ci-tracker credential pat show --name work --probe-repo owner/repo
```
