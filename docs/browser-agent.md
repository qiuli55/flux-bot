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
  扩展点是通用入口，不绑定具体实现。

## 通过扩展点接入浏览器

Claude Code 内核支持在隔离配置里追加额外的 MCP server：

- 配置项：`FLUX_CLAUDE_EXTRA_MCP_SERVERS`（`server/flux/config.py`）
- 取值：一个 JSON 对象字符串，形状与 Claude Code 的 `mcpServers` 一致
- 解析与校验：`server/flux/core/agent_runtime/extra_mcp.py`
- 注入位置：每轮 Run 的 `<FLUX_RUNTIME_HOME>/runs/task-<task_id>/claude-home/mcp.json`

只接受 **http / sse 型远程 server**。`stdio` / `command` 型会被直接丢弃：那等于
让一份配置字符串在 Flux 容器里拉起任意进程，属于刻意不开放的攻击面。Flux 自己的
`flux` 是保留名，配置了同名条目也不会覆盖真实端点。

### 1. 起一个 Playwright MCP sidecar

下面只是**示例片段**，请按所选镜像 / 包的官方文档核对启动参数与端点路径；版本号
用占位符表示，接入前自行验证。**不要**把这段直接当成已适配本仓库的编排文件——
本仓库的 `Dockerfile` / `docker-compose.yml` 不需要为浏览器能力做任何修改。

```yaml
# 示例：与 Flux 处于同一个 docker 网络，不向宿主机发布端口
services:
  playwright-mcp:
    image: <playwright-mcp-image>:<version, verify before use>
    command:
      - "--port"
      - "8931"
      - "--host"
      - "0.0.0.0"
    # 不要写 ports:，除非你明确知道谁可以访问它
    networks:
      - flux
```

要点：

- Flux 容器要能按 **服务名** 解析到它（例如 `http://playwright-mcp:8931/mcp`），
  所以两者必须在同一个 docker 网络里。
- 端点路径以所选实现为准；常见是 `/mcp`（streamable HTTP）或 `/sse`。填错时
  Claude Code 只会在运行时连不上，不会在启动时替你校验。
- Playwright 需要浏览器二进制与足够的共享内存；这属于镜像自身要求，按官方文档
  配置（例如 shm 大小），不在 Flux 侧处理。
- 建议给 sidecar 单独的持久化目录 / profile，并按需限制它能访问的站点。

### 2. 配置 Flux

在 `.env` 里写一行（实际值必须是一行 JSON）：

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

配置约束（不满足的条目会被丢弃并留一条 warning）：

| 字段 | 要求 |
| --- | --- |
| server 名 | ASCII `[A-Za-z0-9_-]{1,64}`；`flux` 为保留名 |
| `type` | 只允许 `http` / `sse` |
| `url` | 必须是 `http://` 或 `https://` 字符串 |
| `headers` | 可选；必须是字符串到字符串的对象 |

日志只记录 **server 名与拒绝原因**，不会打印 `headers` 的值（可能含令牌）。

### 3. 重启并确认

重启 Flux 后发起一轮 Bot 任务，启动日志里应出现：

```text
Claude 本轮注入扩展 MCP server：browser
```

同时可以检查该任务目录下的 `mcp.json`，应同时包含 `flux` 和 `browser` 两个条目。
若日志里只有拒绝原因，按上表核对配置。

### 4. 权限模式

扩展 server 的工具**不会**获得额外放行，它们与 `flux` 工具走同一套 Claude Code
权限行为（映射见 `server/flux/core/agent_runtime/runtimes/cli.py` 的
`_CLAUDE_PERMISSION_MODES`）：

- `plan`：只读规划，浏览器工具同样受只读限制；
- `normal`：映射到 Claude Code 的 `default` 模式。Bot 以 `claude -p`（print /
  headless）方式运行，`default` 模式下没有任何人可以应答审批，因此凡是需要授权的
  工具调用（MCP 工具、Bash、文件编辑）都会被**直接拒绝**。也就是说 `normal` 下
  `browser` 等扩展 server 的工具实际不可用，而不是"逐次询问"；
- `unattended` / `full`：映射到 `bypassPermissions`，工具调用整体放行。

需要浏览器时请使用 `unattended` / `full`；或者在后续把
`--permission-prompt-tool` 接到 Flux 的审批卡（这是内核的待办项），届时 `normal`
才能做到"逐次审批"。浏览器操作往往伴随登录态和对外副作用，放开前先确认任务与站点
范围。依据：Claude Code 文档 `permission-modes` 与 `headless`。

## 安全注意事项

1. **allowed origins / HTTP 鉴权不是安全边界。** 对 MCP server 来说，能连上它的
   进程就能驱动浏览器；`Origin` 校验只防浏览器跨站，不防同网络里的其它容器。
   因此：sidecar 不要发布端口到宿主机或公网，只挂在 Flux 所在的内部网络；需要
   鉴权时用 `headers` 令牌，并同时限制网络可达范围。
2. **使用隔离的浏览器 profile。** 不要挂载用户真实浏览器的 profile / Cookie /
   已登录会话。给 sidecar 独立的数据目录，必要时单独容器、单独用户运行；任务结束
   后按需清理 profile，避免不同 Bot / 任务之间串号。
3. **出站动作要审批。** 登录、下单、发帖、提交表单、下载文件都属于对外副作用。
   `unattended` / `full` 模式不会逐次询问，只适合低风险或已明确授权的任务。注意
   `normal` 在 headless 下并不能"逐次审批"——未经预授权的浏览器工具调用会被直接
   拒绝，所以它只适合验证配置，不适合放开副作用。高风险操作应等内核把
   `--permission-prompt-tool` 接到 Flux 审批卡后再放开，或只给 sidecar 配置只读
   站点白名单。
4. **不要把密钥写进仓库。** `headers` 里的令牌只放部署环境的 `.env`；日志虽然不打
   印 header 值，但 `.env`、`mcp.json`（0600）本身仍需按凭据管理。
5. **不要用 noVNC 桌面地址充当 MCP 端点。** 云桌面是人工入口，不是 Agent 接口；
   把隧道地址填进 `FLUX_CLAUDE_EXTRA_MCP_SERVERS` 只会让内核连到一个网页而不是
   MCP 服务。
