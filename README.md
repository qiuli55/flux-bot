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
`/models`、`/keys`、`/dsh`。Flux 的对话、设计工作室、IDE、终端、助理等路由不在这里。

## 部署（Docker）

```bash
cp .env.example .env
vim .env                 # 至少填 FLUX_AUTH_TOKEN 和模型密钥
docker compose up -d --build
curl http://127.0.0.1:8600/api/v1/health
```

### 数据持久化

数据全部落在挂载卷 `./data`，容器重建不会丢失：

```
data/flux.db              Bot 定义、例程、任务、记忆
data/dsh-home/            执行内核的会话与配置
data/home/.flux/          密钥库主密钥、账号云会话、计费/记忆偏好等本机状态
data/uploads/             用户上传的附件
data/deliveries/          AI 下发给用户的文件
data/dsh-ws/              DSH 工作目录
data/workspace/           Bot 读写文件的工作区
```

换机器时整目录拷走即可，不需要额外导出。其中 `data/home/.flux/key-vault.key` 是
加密 API Key / GitHub Token 的主密钥，务必单独备份；也可以设置
`FLUX_KEY_VAULT_SECRET`，这样不再依赖这个文件。

### 升级注意

从旧版本（`HOME` 仍是 `/root`、上传目录仍在 `/opt/flux`）重建容器前，先把旧容器里的
状态拷进 `./data`，否则此前保存的 API Key / GitHub Token 会因为主密钥变化而无法解密：

```bash
docker compose stop flux-bot
mkdir -p ./data/home
docker cp flux-bot:/root/.flux ./data/home/.flux
docker cp flux-bot:/opt/flux/uploads ./data/uploads
docker cp flux-bot:/opt/flux/deliveries ./data/deliveries
docker compose up -d --build
```

旧容器已删除时，也要先从备份里找回那份 `.flux`。主密钥一旦丢失，就只能重新录入
API Key / GitHub 授权。

## 本地开发

```bash
cd server
python -m alembic -x db_url="sqlite+aiosqlite:///./flux.db" upgrade head
PYTHONPATH=. uvicorn flux.bot_node.main:app --reload --port 8600
```

## 和 Flux 的关系

两边共用同一套执行内核（agent_runtime / task_engine / mcp），但产品边界独立：
Flux 做对话，Flux Bot 做常驻自动化。内核修复目前需要两边各自同步，等 Bot 侧稳定后再考虑抽公共包。
