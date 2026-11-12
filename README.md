# Flux Bot

常驻式 Bot 助手：按例程自己开工、跑任务、留记忆，可以长期挂在一台机器上（NAS / 小主机）。

它是独立项目，与 Flux 本体（对话产品）分开演进。当前仓库由 Flux 后端的执行内核 fork 而来，
后续的 Bot 能力只在这里长。

## 组成

```
server/      Bot 服务（FastAPI）：Bot 定义、例程、群聊、任务执行、记忆
Dockerfile   镜像构建
entrypoint.sh 启动前自动迁移数据库
docker-compose.yml  NAS / 单机部署
```

对外只暴露 Bot 相关接口：`/api/v1/bots`、`/groups`、`/tasks`、`/deliveries`、`/memory`、
`/models`、`/keys`。Flux 的对话、设计工作室、IDE、终端、助理等路由不在这里。

Bot 的 Agent 底座统一是 Claude Code（新老 Bot 一致）。镜像里已装好 Node 与
`@anthropic-ai/claude-code`；要用第三方订阅套餐时，先在 OpenClaw 登录，再把网关地址填到
`FLUX_ANTHROPIC_BASE_URL`。

## 部署（Docker）

```bash
cp .env.example .env
vim .env                 # 至少填 FLUX_AUTH_TOKEN 和模型密钥
docker compose up -d --build
curl http://127.0.0.1:8600/api/v1/health
```

数据全部落在 `./data`：

```
data/flux.db         Bot 定义、例程、任务、记忆
data/runtime-home/    Claude Code 的会话与隔离配置
```

换机器时整目录拷走即可，不需要额外导出。

## 本地开发

```bash
cd server
python -m alembic -x db_url="sqlite+aiosqlite:///./flux.db" upgrade head
PYTHONPATH=. uvicorn flux.bot_node.main:app --reload --port 8600
```

## 和 Flux 的关系

两边共用同一套执行内核（agent_runtime / task_engine / mcp），但产品边界独立：
Flux 做对话，Flux Bot 做常驻自动化。内核修复目前需要两边各自同步，等 Bot 侧稳定后再考虑抽公共包。
