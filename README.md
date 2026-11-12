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

### 定时与时区

Bot 的 cron（`0 9 * * *`）和例程、每日 token 预算重置都按 `FLUX_TIMEZONE` 解释，默认 UTC。
例如设置 `FLUX_TIMEZONE=Asia/Shanghai` 后，`0 9 * * *` 在本地 09:00 触发，预算也在本地零点重置。

### 鉴权与网络暴露

**`FLUX_AUTH_TOKEN` 是必填项。** 没有它时 Bot 节点会 fail-closed：只接受本机回环直连
（`127.0.0.1` / `::1`，且没有 `X-Forwarded-For` 等转发头），其他请求一律 401。
这会直接拦住创建 Bot、触发无人值守任务等会执行命令的接口。

Docker Compose 默认把端口绑在宿主机回环上（`127.0.0.1:8600`），只有同机进程能访问。
只有反向代理在另一台机器 / 需要局域网直连时，才把 `FLUX_BIND_ADDR` 改成 `0.0.0.0`：

```bash
FLUX_BIND_ADDR=0.0.0.0    # 此时必须同时设置 FLUX_AUTH_TOKEN
```

特别注意：**在 Docker 内，经发布端口进来的请求来自 docker 网关，不是回环地址。**
因此即使从宿主机访问 `127.0.0.1:8600`，容器内看到的客户端也不是 loopback；
没有配置 `FLUX_AUTH_TOKEN` 时这些请求会被 401 拒绝。`/api/v1/health` 是唯一免鉴权的
探针接口，供 Docker `HEALTHCHECK` 使用。

旧的无鉴权行为只能显式开启：`FLUX_ALLOW_NO_AUTH=true`。它仅适用于完全可信的内网，
启动时会打印 WARNING；不要与公网暴露一起使用。

### 设备配对

手机不再共用 `FLUX_AUTH_TOKEN`，而是用一次性配对码换一枚只属于自己的长期设备令牌
（`fbd_...`，数据库只存 sha256）。在服务器上生成配对码：

```bash
# Docker 部署
docker compose exec flux-bot flux devices pair

# 或调用管理 API（需要管理员令牌 / 本机回环）
curl -s -X POST http://127.0.0.1:8600/api/v1/devices/pairing-codes \
  -H "Authorization: Bearer $FLUX_AUTH_TOKEN"
```

把返回的 8 位配对码输入 App 的配对页即可。配对码单次有效，默认 10 分钟过期；
设备令牌默认 90 天过期（`FLUX_DEVICE_TOKEN_TTL_DAYS=0` 表示永不过期）。
失败的配对尝试按来源 IP 限流：每分钟最多 10 次。

管理已配对设备：

```bash
docker compose exec flux-bot flux devices list
docker compose exec flux-bot flux devices revoke <device-id>
```

手机也可以调用 `POST /api/v1/devices/me/revoke` 主动登出。撤销在本进程内立即生效；
多进程部署时，其他进程最多延迟 30 秒（设备鉴权缓存的 TTL）。

### 云桌面票据

`POST /api/v1/bots/{bot_id}/desktop/session` 为某一台 Bot 签发独立的桌面会话。
服务端按 Bot UUID 计算桌面 profile（`bot` + UUID 前 8 位 hex），手机不能自报 profile，
因此不同 Bot 的桌面配置彼此隔离。

配置 `FLUX_DESKTOP_HOST` 与 `FLUX_DESKTOP_GATEWAY_TOKEN` 后该接口才可用，否则返回
503 `desktop_not_configured`。配置 `FLUX_DESKTOP_TICKET_SECRET` 后，响应里会带一个短期
HMAC 票据（默认 300 秒）：

```
ticket = base64url(payload_json) + "." + base64url(HMAC-SHA256(base64url(payload_json), secret))
```

payload 包含 `bot_id`、`profile`、`principal`（设备会话另有 `device_id`）、`exp`（Unix 秒）
与随机 `nonce`。桌面网关的验签步骤：

1. 按 `.` 拆成 payload 段与签名段；
2. 用共享的 `FLUX_DESKTOP_TICKET_SECRET` 对 payload 段重算 HMAC-SHA256，并常量时间比较；
3. 对 payload 段做 base64url 解码（补回 `=` padding）并解析 JSON；
4. 校验 `exp` 大于当前 Unix 秒，且 `profile` / `bot_id` 与请求的桌面实例一致。

票据可用后，把 `FLUX_DESKTOP_SEND_GATEWAY_TOKEN` 设为 `false`：会话响应将不再下发
长期的 `gateway_token` / `bridge_key`，手机只持有短期票据。

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
