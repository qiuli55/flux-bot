# 微信公众号 / 服务号通道：官方接入选项（调研，不实现）

- 文档日期：2026-11-12（America/Chicago）
- 状态：调研提案。本 PR **不包含实现、迁移或 API**。
- 目的：在实现企业微信 / 钉钉通道的同时，把「个人微信 / 公众号侧」的官方选项写清楚，避免和现有 Supervisor 微信桥、以及企业微信通道混为一谈。

## 1. 先划清三条线

仓库里已经或即将出现的「微信」相关能力不是同一件事：

| 能力 | 对象 | 传输层 | 本提案态度 |
| --- | --- | --- | --- |
| Supervisor 微信桥（已有） | 个人微信私聊 | OpenClaw / 微信 ClawBot 把消息转到本机 `POST /api/v1/supervisor/channels/wechat`，Flux 只做大脑，**没有**公网微信回调，也**没有**微信协议 | 保持不动，不在本通道里复用 |
| 企业微信（WeCom）通道（并行开发） | 企业成员 / 客户联系 | 企业微信官方回调（VerifyURL、AES 解密 `echostr`、加密 XML POST） | 独立通道，不要和公众号共用一套校验 |
| 本提案：公众号 / 服务号 | 关注服务号的 C 端用户（OpenID） | 微信公众平台「消息与事件推送」公网 URL | **暂不实现**；若以后做，按下节官方约束走 |

企业微信文档（2026-11-12 查阅）：[接收消息与事件](https://developer.work.weixin.qq.com/document/path/90930)。钉钉是另一条官方通道（事件订阅 / SyncHTTP），同样不在本提案范围。

## 2. 官方账号类型：建议只考虑已认证服务号

可选项：

1. **服务号**（推荐作为唯一候选）：有完整的消息与事件推送、客服消息、服务端 API（`access_token`）。配置入口在「微信开发者平台 - 我的业务 - 服务号 - 消息与事件推送」。
2. **订阅号 / 历史「公众号」形态**：能力更窄，群发与互动限制更严，不适合做 Bot 一对一通道。
3. **微信开放平台第三方应用代商家授权**：适合将来「多租户替别人的服务号接 Bot」，第一版不必上；若做，走 `authorizer_access_token` 而不是把商家 AppSecret 收进本机。
4. **小程序**：另有一套订阅消息 / 客服消息，不是本通道。

结论：若以后实现官方微信通道，第一版只做 **已认证服务号**。未认证账号的接口权限不足，不要为此分叉实现。

## 3. 入站：消息与事件推送

来源（2026-11-12 查阅，文档「最后一次编辑于 2026-03-05」）：

- [消息与事件推送介绍](https://developers.weixin.qq.com/doc/service/guide/dev/push/)
- [消息加解密说明](https://developers.weixin.qq.com/doc/service/guide/dev/push/encryption.html)
- [接收普通消息](https://developers.weixin.qq.com/doc/service/guide/product/message/Receiving_standard_messages.html)

要点：

- 开发者配置 **URL、Token、EncodingAESKey**。URL 必须以 `http://`（仅 80 端口）或 `https://`（仅 443 端口）开头。
- 加解密方式有三种：**明文**（默认，官方不建议）、**兼容**（明文+密文，官方不建议生产使用）、**安全模式**（纯密文，**强烈推荐**）。服务号推送页写明数据格式「仅支持 XML」；加解密说明里的示例同时给了 JSON/XML 回包形状，实现时以当时后台可选格式为准，但生产只开安全模式。
- **GET 验签**（接入时）：将 `token`、`timestamp`、`nonce` 字典序排序后做 SHA-1，与 `signature` 比较；通过则**原样返回 `echostr` 明文**。
- **安全模式 POST**：不要再用 `signature` 验签。应将 `token`、`timestamp`、`nonce`、包体 `Encrypt` 四项排序后 SHA-1，与 `msg_signature` 比较。AES-CBC、32 字节密钥（`EncodingAESKey` 补 `=` 后 Base64 解码）、PKCS#7。明文结构为 `random(16B) + msg_len(4B, 网络字节序) + msg + appid`，解密后必须校验 `appid`。被动回复若不是空串 / `success`，也要按同样规则加密回包。
- 时限：微信服务器 **5 秒内**收不到响应会断开并重试，最多三次。排重推荐用 `MsgId`。若 5 秒内做不完业务，应先回空串或 `success`，再异步处理，否则会重复推送。
- 发送方是 **OpenID**（用户对每个服务号唯一），不是微信号，也不是 Supervisor 桥里的 `wxid_`。

与企业微信回调的差异（实现时不要复用 WeCom 校验函数）：企业微信 GET 校验要**解密 `echostr` 再返回明文**（1 秒内）；服务号 GET 验签通过后**直接返回 `echostr`**。两边都是 AES + SHA-1 排序签名，但打包格式、参数名、时限不同。

## 4. 出站：官方允许的三条路径

1. **被动回复**（回调的 HTTP 响应体内）：必须在约 5 秒内返回。Bot 推理几乎一定超时，故 **第一版不要把模型回复放进被动回复**。回调只做验签、解密、排重、入队，然后回空串或 `success`。
2. **客服消息**（主动 HTTP API，推荐作为 Bot 回复通道）：
   - [客服消息介绍](https://developers.weixin.qq.com/doc/service/guide/product/kf/intro.html)（最后编辑 2026-03-05）
   - [发送客服消息](https://developers.weixin.qq.com/doc/service/api/customer/message/api_sendcustommessage.html)（最后编辑 2026-03-16）
   - `POST https://api.weixin.qq.com/cgi-bin/message/custom/send?access_token=ACCESS_TOKEN`
   - 额度（官方表，平台可能更新）：用户发消息后 **48 小时内最多 5 条**；点击自定义菜单 / 关注 / 扫码为 **1 分钟内最多 3 条**。不能把客服消息当群发或营销通道。
3. **订阅通知 / 模板类通知**：适合「用户先订阅、服务号按模板推送」的事务通知，不适合开放式对话。若以后要做提醒类出站，应单独评估，不要和对话回复混用。

调用服务端 API 还需要（[接口调用概述](https://developers.weixin.qq.com/doc/service/guide/dev/api/)，最后编辑 2026-03-05）：

- 用 AppID + AppSecret 换 `access_token`（约 7200 秒，重复获取会使旧令牌失效；应集中刷新，预留至少 512 字符存储）。官方推荐稳定版凭证接口。
- API IP 白名单：未列入的 IP 换票会失败（如 61004）。本机 / 家庭网络出口不稳定时，这是落地最大的运维阻力之一。
- 接口有日限额（例如发送客服消息默认较高，但 `access_token` 获取有上限）。见 [接口调用额度说明](https://developers.weixin.qq.com/doc/service/guide/dev/api/limit.html)（最后编辑 2026-03-05）。

## 5. 若以后实现，建议怎么接到 Flux

这些是设计约束，不是本 PR 的工作项：

- **回调公开路由**同时挂在 `flux.main` 与 `bot_node`（与 hooks / 飞书 / 制品页面公开读取相同），验签与加解密自证身份，不套本机 REST 令牌。
- **出站必须走 `outbound_drafts`**（见 PR #8）：Bot 拟回的客服消息先入草稿，用户批准后再调用 `custom/send`。禁止自动发送。批准链路复用现有 `raise_approval` / `Events.APPROVAL_RESOLVED`。
- 因为 5 秒时限，入站处理应只做验签、解密、`MsgId` 排重、写入会话 / 触发 Bot，然后立即回 `success`。真正的回复在批准后异步发送。
- 凭据（Token、EncodingAESKey、AppSecret、access_token）进密钥库，日志只记 appid / openid / msgid / 字节数，不记明文消息和密钥。
- 加解密可用已有 `cryptography` 依赖自己实现（飞书那套 AES 与微信打包格式不同，不要直接复用 Feishu crypto），或以官方示例代码为对照写测试向量。
- **不要**改 Supervisor 微信桥，也**不要**把个人微信协议 / 非官方客户端做成「官方通道」。
- 公网 URL 必须是 443 上的 HTTPS（不要指望在非 443 端口过验签）。本机开发需要隧道，并处理 IP 白名单。

## 6. 建议与未决问题

**建议：本轮不实现公众号 / 服务号通道。** 企业微信和钉钉更适合当前「工作通讯」核心体验；服务号要认证主体、固定公网 443、API IP 白名单，以及 48 小时 / 5 条客服消息额度，和 Flux 本机 Bot 的部署形态摩擦更大。现有 Supervisor 桥已经覆盖「个人微信私聊进助理」这一条非官方传输路径。

若以后要做，第一版范围应仅是：已认证服务号 + 安全模式回调 + 客服消息经出站草稿发送。不做明文模式、不做订阅号、不做群发、不把模型输出写进 5 秒被动回复。

未决（实现前再核对当时官方文档）：

- 目标用户是否具备已认证服务号与稳定出口 IP。
- 是否走开放平台第三方代商家，而不是在本机存 AppSecret。
- 订阅通知是否作为「非对话提醒」的第二条出站路径。
- 回调数据格式以服务号后台当时可选值为准（推送介绍写 XML，加解密示例含 JSON）。

## 7. 资料（查阅于 2026-11-12）

| 文档 | URL | 页面标注的最后编辑 |
| --- | --- | --- |
| 消息与事件推送介绍 | https://developers.weixin.qq.com/doc/service/guide/dev/push/ | 2026-03-05 |
| 消息加解密说明 | https://developers.weixin.qq.com/doc/service/guide/dev/push/encryption.html | 2026-03-05 |
| 接收普通消息 | https://developers.weixin.qq.com/doc/service/guide/product/message/Receiving_standard_messages.html | 2026-03-05 |
| 客服消息介绍 | https://developers.weixin.qq.com/doc/service/guide/product/kf/intro.html | 2026-03-05 |
| 发送客服消息 | https://developers.weixin.qq.com/doc/service/api/customer/message/api_sendcustommessage.html | 2026-03-16 |
| 接口调用概述 | https://developers.weixin.qq.com/doc/service/guide/dev/api/ | 2026-03-05 |
| 接口调用额度说明 | https://developers.weixin.qq.com/doc/service/guide/dev/api/limit.html | 2026-03-05 |
| 企业微信接收消息与事件（对照，非本通道） | https://developer.work.weixin.qq.com/document/path/90930 | 2026-11-12 查阅 |
