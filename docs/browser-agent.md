# Bot 浏览器自动化（Browser Agent）

本文说明 Bot 执行内核当前有没有浏览器能力、如何通过扩展 MCP 注入一个浏览器
sidecar，以及这样做时哪些安全边界必须自己守住。

## 当前状态

- **执行内核没有浏览器工具。** Claude Code 内核每轮拿到的隔离 `mcp.json` 里只有
  Flux 自己的 `flux` server（项目读取 / 提案 / 终端），没有导航、点击、截图之类的
  工具。Agent 默认只能读文件、跑命令、走 Flux MCP。
- **`server/flux/core/desktop` 的 noVNC 云桌面不是 Agent 能力。** 它是一套
  「给人看、给人操作」的远程桌面入口：Flux 只负责解析隧道地址、签发短期票据，
  把手机 / 浏览器接到 Hermes 云电脑的 noVNC 页面。Agent 既不能调用它，也拿不到
  页面 DOM 或截图；它解决的是「人远程看这台 Bot 的桌面」，不是「Bot 自己上网」。
- 因此，需要 Bot 真正操作网页时，必须显式接入一个 **MCP 浏览器 server**。下面的
  扩展点是通用入口；本仓库另外附带一个可选的 Playwright MCP overlay 作为已验证
  形状的参考实现。

## 通过扩展点接入浏览器

Claude Code 内核支持在隔离配置里追加额外的 MCP server：

- 配置项：`FLUX_CLAUDE_EXTRA_MCP_SERVERS`（`server/flux/config.py`）
- 取值：一个 JSON 对象字符串，形状与 Claude Code 的 `mcpServers` 一致
- 解析与校验：`server/flux/core/agent_runtime/extra_mcp.py`
- 注入位置：每轮 Run 的 `<FLUX_RUNTIME_HOME>/runs/task-<task_id>/claude-home/mcp.json`

只接受 **http / sse 型远程 server**。`stdio` / `command` 型会被直接丢弃：那等于
让一份配置字符串在 Flux 容器里拉起任意进程，属于刻意不开放的攻击面。Flux 自己的
`flux` 是保留名，配置了同名条目也不会覆盖真实端点。

### 1. 起一个 Playwright MCP sidecar（可选 overlay）

仓库根的 `docker-compose.browser.yml` 是一个**可选** overlay，不改基础镜像、
`Dockerfile` 或 `docker-compose.yml`。它新增：

- `playwright-mcp`：官方 Playwright MCP 镜像，长驻 streamable HTTP 服务，
  端点 `http://playwright-mcp:8931/mcp`；
- 只加入专用 `browser` 网络的 `flux-bot`，并注入 `browser` 这个额外 MCP server。

#### 版本快照

以下值是 **[2026-11-12 快照]**，不是「永远最新」声明：

| 项目 | 值 |
| --- | --- |
| npm 包 | `@playwright/mcp` 最新版 `0.0.85`（2026-11-09 发布） |
| 官方镜像 | `mcr.microsoft.com/playwright/mcp:v0.0.85` |
| 镜像 digest | `sha256:cc70a6922ff856f3aba0fe97d0d94220dc9a7f367f6d332facc6d81d9a91e60e`（多架构索引） |
| MCP 端点 | `http://playwright-mcp:8931/mcp` |

来源：

- npm registry：<https://registry.npmjs.org/@playwright%2Fmcp>
- mcr.microsoft.com tag list：<https://mcr.microsoft.com/v2/playwright/mcp/tags/list>
- 官方 README（长驻服务示例与全部命令行参数）：<https://github.com/microsoft/playwright-mcp>

overlay 里的完整镜像引用是：

```text
mcr.microsoft.com/playwright/mcp:v0.0.85@sha256:cc70a6922ff856f3aba0fe97d0d94220dc9a7f367f6d332facc6d81d9a91e60e
```

tag 与 digest 同时写是刻意的：tag 方便人读，digest 保证就算 tag 被重新指向，
拉到的仍是快照当天的同一份内容。

#### 启动

必须把基础编排一起叠加，否则 `flux-bot` 的镜像、`./data` 卷和 `.env` 都会丢：

```bash
docker compose -f docker-compose.yml -f docker-compose.browser.yml up -d
```

overlay 的关键配置：

- **不写 `ports:`**，只 `expose: "8931"`：端口只在 compose 网络内可见，宿主机和
  局域网都连不上；能连上 sidecar 的只有 `browser` 网络里的 `flux-bot`。
- `--headless --browser chromium --no-sandbox`：容器内无显示器，必须 headless；
  `--no-sandbox` 沿用官方 README 的容器示例（容器内 Chromium 沙箱常不可用）。
  注意镜像默认以 `node`（uid 1000）运行，不是 root。
- `--user-data-dir /home/node/profiles/${FLUX_BROWSER_PROFILE_ID:-default}`：浏览器
  profile（cookies、localStorage、IndexedDB 等）持久化到 `playwright-home` 卷上，
  浏览器进程重启（包括 MCP 空闲超时后重新拉起）与容器重建后登录态都还在。
  **不要同时使用 `--isolated`**：它与 `--user-data-dir` 互斥，且会把 profile 只
  放在内存里。配置、重置与多用户约定见下面的「Profile 持久化」。
- `--allowed-hosts playwright-mcp,playwright-mcp:8931`：该服务默认只接受
  `localhost` / 回环地址和 `--host` 取值作为 Host 头。**必须同时列出带端口的
  `host:port` 形式**，原因见下面的「两个已知坑」。
- `--output-dir /home/node/output` + 命名卷 `playwright-home:/home/node`：截图 /
  下载产物落在卷里，容器重建不丢。输出目录必须在已挂载且属于 `node` 的路径下，
  原因见下面的「两个已知坑」。
- 卷名是 `playwright-home` 而不是 `playwright-output`：它挂的是整个 `/home/node`
  家目录，不只是输出目录；旧名字会误导人以为挂的是 `/data/output`。
- `shm_size: "1gb"` + `init: true`：Chromium 对共享内存的需求远大于 docker 默认
  的 64MB；`init` 负责回收僵尸进程。
- 基础编排里的 `flux-bot` 保留 `default` 网络（宿主机 8600 端口发布照常工作），
  额外加入 `browser` 网络，从而能按服务名解析到 `playwright-mcp`。

#### Profile 持久化（默认）

浏览器登录态默认是**持久化**的，不再使用 `--isolated`。

- **放在哪里。** `--user-data-dir` 指向命名卷 `playwright-home`（挂载在
  `/home/node`）里的 `/home/node/profiles/<profile id>`。这里用的是完整的
  Chromium user-data-dir，而不是 `--storage-state`：cookies、localStorage、
  IndexedDB、已登录会话等都会一起保留。
- **能扛住什么。** 浏览器进程重启（包括 Playwright MCP 空闲超时后的 relaunch）、
  `docker compose stop/start playwright-mcp`、`docker compose down` 后再 `up`，
  只要 `playwright-home` 命名卷不被删除，profile 就还在。删除卷（例如
  `docker compose down -v`）或手工清空 profile 目录才会丢登录态。
- **为什么不用 /data。** profile 目录与输出目录共用 `/home/node` 这一个 node
  可写的 home 卷：`/home/node/profiles/<id>` 放 profile，`/home/node/output`
  放截图 / 下载。不要把 profile 放到镜像里不存在的 `/data/...`，那会被 Docker
  建成 `root:root`，node 写不进去（见下面的「两个已知坑」）。
- **不要复用真实浏览器 profile。** 只持久化 sidecar 自己这份专用 profile；
  不要把宿主机上人的 Chrome / Edge profile 或 Cookie 挂进来。

##### 配置 profile id

profile 目录按 id 分层，默认 id 是 `default`：

```bash
# 默认：/home/node/profiles/default
docker compose -f docker-compose.yml -f docker-compose.browser.yml up -d

# 指定 id（compose 从 shell 环境或项目根的 .env 插值）：
FLUX_BROWSER_PROFILE_ID=alice \
  docker compose -f docker-compose.yml -f docker-compose.browser.yml up -d
# 对应 /home/node/profiles/alice
```

也可以把 `FLUX_BROWSER_PROFILE_ID=alice` 写进项目根的 `.env`（见
`.env.example`）。注意：

- `${FLUX_BROWSER_PROFILE_ID:-default}` 是 **docker compose 的插值**，不是容器内
  的环境变量；变量必须在执行 `docker compose` 的 shell 或项目 `.env` 里设置。
- 需要字面量 `$` 时写成 `$$`。
- 换一个 id 会得到一份全新的空 profile（相当于新登录）；同一个 id 重复启动会
  继续使用已有登录态。
- **多用户隔离（后续方向）**：目录布局已经按 id 分层。等到接入多账号 / 多用户
  路由时，把 `FLUX_BROWSER_PROFILE_ID`（或等价的 per-account 配置）设为账号 /
  用户 id，让每个人有自己的 `/home/node/profiles/<id>`。**不要**让多个用户共享
  同一个 profile 目录，否则 cookie / 登录态会串号。本 PR 只约定目录布局，不实现
  账号管理服务或真正的 per-user 路由。

##### 重置 profile

需要清掉 cookies / 登录态时，只删除 profile 子目录即可；同一个卷里的
`/home/node/output` 会保留。仓库提供了一个简单脚本（需要本机有 docker）：

```bash
# 清空默认 profile，然后重新拉起 sidecar
scripts/reset-browser-profile.sh

# 清空指定 profile id
FLUX_BROWSER_PROFILE_ID=alice scripts/reset-browser-profile.sh
```

手工等价步骤（在仓库根目录执行）：

```bash
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.browser.yml"
$COMPOSE stop playwright-mcp
$COMPOSE run --rm --no-deps --entrypoint sh playwright-mcp \
  -c "rm -rf '/home/node/profiles/default'"
$COMPOSE up -d playwright-mcp
```

不要用 `docker compose down -v` 或删除 `playwright-home` 卷来重置 profile：
那会同时删掉 `/home/node/output` 里的截图与下载产物。

##### 临时使用全新会话（可选）

想要一次性干净会话时，最省事的做法是用一个临时 profile id：

```bash
FLUX_BROWSER_PROFILE_ID=scratch \
  docker compose -f docker-compose.yml -f docker-compose.browser.yml up -d
# 用完清掉
FLUX_BROWSER_PROFILE_ID=scratch scripts/reset-browser-profile.sh
```

如果确实要回到官方的 `--isolated` 内存模式，必须同时**移除**
`--user-data-dir` 两个参数（二者互斥），再临时加上 `--isolated`；这属于手工改
overlay 的一次性操作，不作为默认配置。

#### 两个已知坑（2026-11-13 实机验证）

在 Docker 29 上用钉死的 `v0.0.85` 镜像跑
`docker compose -f docker-compose.yml -f docker-compose.browser.yml up` 时，
下面两处配置各自会让浏览器能力**启动即坏**，且都不容易从日志一眼看出：

1. **Host 头必须带端口。** 只写 `--allowed-hosts playwright-mcp` 时，HTTP 客户端
   （Claude Code、httpx）访问 `http://playwright-mcp:8931/mcp` 发出的 Host 头是
   `playwright-mcp:8931`，server 比对失败后直接回
   `403 Access is only allowed at playwright-mcp`。改成逗号分隔的
   `playwright-mcp,playwright-mcp:8931` 后返回 200。因此 403 不是「网络不通」，
   而是白名单少了 `host:port`；验证连通性时也不能把 403 当成成功。
2. **输出目录不能落在镜像里不存在的路径。** 原来把命名卷挂到 `/data/output`，
   但该路径在官方镜像里不存在：Docker 会以 `root:root`、`0755` 新建挂载点，而
   容器以 `node`（uid 1000）运行，于是 `browser_navigate` 在写页面快照时报
   `EACCES: permission denied, open /data/output/page-....yml`。改为把卷挂到镜像
   内已存在且属于 `node` 的 `/home/node`（空命名卷首次挂载会继承该目录属主），
   输出目录用 `/home/node/output`，node 就能正常写入。挂载家目录后 profile 也确实
   落在该卷的 `/home/node/profiles/<id>` 下——这是默认的持久化行为，不是意外残留；
   重置方式见上面的「Profile 持久化」。

#### 出站网络：为什么不用 `internal: true`

`browser` 网络是普通 bridge 网络，**没有** 设 `internal: true`。原因很直接：
浏览器要访问互联网，而 `internal: true` 会切断 sidecar 的所有外部路由，
Playwright 连一个站点都打不开，浏览器能力就失去了意义。

代价是这个 sidecar 默认拥有不受限的出站访问。需要收紧时，**由运维在 sidecar
之外**做 egress 过滤，例如：

- 在宿主机防火墙 / 网关上只放行任务需要的域名或 IP 段；
- 给 sidecar 配一个正向代理，只允许白名单目标；
- 用单独的网络命名空间 / 独立主机承载高风险任务。

注意：Playwright MCP 的 `--allowed-origins` / `--blocked-origins` 只约束浏览器
能导航到哪些源，**不是**安全边界（官方 README 明确说明），不能拿来替代网络层
egress 控制。

#### 验证

```bash
# 1) 只做静态渲染校验，不拉镜像、不启动容器
docker compose -f docker-compose.yml -f docker-compose.browser.yml config >/dev/null

# 2) 启动并确认两个容器都在运行
docker compose -f docker-compose.yml -f docker-compose.browser.yml up -d
docker compose -f docker-compose.yml -f docker-compose.browser.yml ps

# 3) 看 sidecar 是否已在监听 8931
docker compose -f docker-compose.yml -f docker-compose.browser.yml logs playwright-mcp

# 4) 从 flux-bot 容器内验证按服务名可达。curl 会自动发 Host: playwright-mcp:8931，
#    因此这一步同时校验 Host 白名单：GET 一个 MCP 端点通常返回 400/405/406；
#    返回 403 说明 --allowed-hosts 少了 host:port，连接被拒 / DNS 失败才是网络不通
docker compose -f docker-compose.yml -f docker-compose.browser.yml exec flux-bot \
  curl -sS -o /dev/null -w '%{http_code}\n' http://playwright-mcp:8931/mcp

# 5) 确认输出目录与 profile 目录都对容器内的 node 用户可写
#    （EACCES 说明卷挂载点属主不对）
docker compose -f docker-compose.yml -f docker-compose.browser.yml exec playwright-mcp \
  sh -c 'mkdir -p /home/node/output /home/node/profiles/default && \
         touch /home/node/output/.probe && echo writable'
```

##### 验证登录态确实能持久化（手工 / 集成验证）

自动化测试只做静态 YAML / 文档检查，**不会启动 Docker**，因此下面的持久化验证
必须在有 Docker 的机器上手工执行：

```bash
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.browser.yml"

# 1) 起 sidecar
$COMPOSE up -d playwright-mcp

# 2) 通过 MCP 或一个测试页面写入可识别的 cookie / 完成一次测试登录。
#    例如用 MCP 客户端执行 browser_navigate 打开一个测试站点并登录，
#    或调用 browser_evaluate 写入 cookie：
#      document.cookie = "flux_persist_probe=1; max-age=86400; path=/"

# 3) 只重启浏览器进程 / 容器（不删卷）
$COMPOSE restart playwright-mcp
# 4) 再验证 cookie 还在；随后重建容器（down 不带 -v / up）
$COMPOSE down
$COMPOSE up -d playwright-mcp
#    重新读取 document.cookie 或页面登录态，仍应看到 flux_persist_probe=1 / 已登录。

# 5) 反向验证 reset 只清 profile、不删输出
scripts/reset-browser-profile.sh
#    cookie 应消失；/home/node/output 里的既有文件仍在。
```

若第 4 步 cookie 丢失，先确认 `--user-data-dir` 指向
`/home/node/profiles/<id>`、该路径位于 `playwright-home` 卷内，并确认没有误用
`--isolated` 或执行过 `down -v`。

然后发起一轮 Bot 任务，启动日志里应出现：

```text
Claude 本轮注入扩展 MCP server：browser
```

同时可以检查该任务目录下的 `mcp.json`，应同时包含 `flux` 和 `browser` 两个条目。
若日志里只有拒绝原因，按下面的配置约束核对。若 Claude Code 运行期连不上，先在
`flux-bot` 里重跑第 4 步区分网络问题与端点路径问题。

> **验证范围**：镜像已在 2026-11-13 用 Docker 29 拉取并运行过，上面「两个已知坑」
> 及对应修法均为实机结果；**镜像大小仍未实测**。2026-11-14 的默认持久化 profile
> 改动（`--user-data-dir` + 命名卷 + profile id）**没有**在本次改动环境里跑过
> Docker —— 这台机器没有 docker / daemon，持久化登录态请按上面的手工步骤在有
> Docker 的机器上验证。版本、digest、启动参数与端点来自公开 registry / README。
> 仓库测试只对 compose 文件与文档做静态解析，不会联网或启动容器，所以它只能防
> 配置回退，不能替代手工验证。

#### 不叠加 overlay 时

只想要浏览器能力、不用本仓库 overlay 也可以，把 sidecar 接到与 `flux-bot` 相同的
网络，并在部署环境的 `.env` 里写一行（实际值必须是一行 JSON）：

```dotenv
FLUX_CLAUDE_EXTRA_MCP_SERVERS={"browser":{"type":"http","url":"http://playwright-mcp:8931/mcp"}}
```

需要多个 server 时放进同一个对象即可：

```dotenv
FLUX_CLAUDE_EXTRA_MCP_SERVERS={"browser":{"type":"http","url":"http://playwright-mcp:8931/mcp"},"search":{"type":"sse","url":"http://search-mcp:9000/sse"}}
```

如果 sidecar 需要鉴权，用 `headers` 传令牌（不要把真实密钥提交进仓库）：

```dotenv
FLUX_CLAUDE_EXTRA_MCP_SERVERS={"browser":{"type":"http","url":"http://playwright-mcp:8931/mcp","headers":{"Authorization":"Bearer <token, keep secret>"}}}
```

端点路径以所选实现为准；Playwright MCP 的长驻 HTTP 端点是 `/mcp`。填错时 Claude
Code 只会在运行时连不上，不会在启动时替你校验。

配置约束（不满足的条目会被丢弃并留一条 warning）：

| 字段 | 要求 |
| --- | --- |
| server 名 | ASCII `[A-Za-z0-9_-]{1,64}`；`flux` 为保留名 |
| `type` | 只允许 `http` / `sse` |
| `url` | 必须是 `http://` 或 `https://` 字符串 |
| `headers` | 可选；必须是字符串到字符串的对象 |

日志只记录 **server 名与拒绝原因**，不会打印 `headers` 的值（可能含令牌）。

### 2. 权限模式

扩展 server 的工具**不会**获得额外放行，它们与 `flux` 工具走同一套 Claude Code
权限行为（映射见 `server/flux/core/agent_runtime/runtimes/cli.py` 的
`_CLAUDE_PERMISSION_MODES`）：

- `plan`：只读规划，浏览器工具同样受只读限制；
- `normal`：映射到 Claude Code 的 `default` 模式。Bot 以 `claude -p`（print /
  headless）方式运行，`default` 模式下没有交互式终端可以应答审批，因此凡是需要
  授权的工具调用（MCP 工具、Bash、文件编辑）都会被**直接拒绝**。也就是说目前
  `normal` 下 `browser` 等扩展 server 的工具实际不可用，而不是「逐次询问」；
- `unattended` / `full`：映射到 `bypassPermissions`，工具调用整体放行。

`--permission-prompt-tool` 审批桥已在 PR #23
（<https://github.com/qiuli55/flux-bot/pull/23>，分支 `feat/claude-approval-bridge`，
尚未合并）中实现，不再是内核待办项。合并后 `normal` 会把浏览器等工具的调用转成
Flux 审批卡而不是直接拒绝；在合并之前，`normal` 仍然直接拒绝这类调用，使用浏览器
必须切到 `unattended` / `full`。浏览器操作往往伴随登录态和对外副作用，放开前先确认
任务与站点范围。依据：Claude Code 文档 `permission-modes` 与 `headless`。

## 安全注意事项

1. **allowed origins / allowed hosts 不是安全边界。** 对 MCP server 来说，能连上
   它的进程就能驱动浏览器；`Origin` / `Host` 校验只防浏览器跨站与 DNS rebinding，
   不防同网络里的其它容器。因此：sidecar 不要发布端口到宿主机或公网，只挂在
   Flux 所在的专用网络；需要鉴权时用 `headers` 令牌，并同时限制网络可达范围。
2. **出站范围要自己收紧。** overlay 的 `browser` 网络刻意允许浏览器访问互联网
   （`internal: true` 会让浏览器直接不可用），所以默认没有出站白名单。高风险部署
   应在宿主机 / 网关做 egress 过滤或前置代理，不要依赖 MCP server 的 origins 参数。
3. **只用 sidecar 自己的持久化 profile，且按用户分开。** 默认持久化的是 sidecar
   自己那份专用 Chromium profile，位于 `playwright-home` 卷的
   `/home/node/profiles/<profile id>`。**不要挂载人的真实桌面 Chrome / Edge
   profile、宿主机 Cookie 或已登录会话**：那等于把个人账号完全交给 Agent，且
   会绕过用户自己的安全边界。多用户 / 多账号部署时，为每个用户或账号使用独立的
   `FLUX_BROWSER_PROFILE_ID`（各自独立子目录），不要共享同一份 profile，避免
   cookie / 登录态串号；需要丢弃登录态时用上面的重置步骤。
4. **出站动作要审批。** 登录、下单、发帖、提交表单、下载文件都属于对外副作用。
   `unattended` / `full` 模式不会逐次询问，只适合低风险或已明确授权的任务。注意
   目前 `normal` 在 headless 下并不能「逐次审批」——未经预授权的浏览器工具调用会
   被直接拒绝，所以它只适合验证配置，不适合放开副作用。逐次审批的审批桥已由 PR #23
   （<https://github.com/qiuli55/flux-bot/pull/23>，分支 `feat/claude-approval-bridge`，
   尚未合并）实现，合并后 `normal` 才会把这些调用转成 Flux 审批卡；在那之前高风险
   操作应使用 `unattended` / `full` 并自行确认授权范围，或只给 sidecar 配置只读
   站点白名单（注意白名单不是安全边界）。
5. **不要把密钥写进仓库。** `headers` 里的令牌只放部署环境的 `.env`；日志虽然不打
   印 header 值，但 `.env`、`mcp.json`（0600）本身仍需按凭据管理。
6. **不要用 noVNC 桌面地址充当 MCP 端点。** 云桌面是人工入口，不是 Agent 接口；
   把隧道地址填进 `FLUX_CLAUDE_EXTRA_MCP_SERVERS` 只会让内核连到一个网页而不是
   MCP 服务。
