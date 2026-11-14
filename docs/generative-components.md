# 生成式组件（schema_version = 1）契约

对话里的富消息有两种来源：既有的 `file_card` / `plan_card` / `decision` 等类型，
以及 Bot / Agent 新发的 `kind="component"`。新客户端统一读 `payload.component`；
老客户端继续按 `kind` / `content` / 原有 payload 渲染。

## 一条组件消息

```
kind    = "component"
content = 必填纯文本兜底（IM 频道与老客户端展示的就是它）
payload = {
  "schema_version": 1,
  "component": <node>,
  "fallback_text": "<与 content 相同或更短的一句话>"
}
```

`TaskMessage.to_dict()` 对既有类型**不改写数据库**，只在读取时把
`schema_version` / `component` / `fallback_text` 附加到原 payload 旁边。原字段一个不动。

## 节点白名单

每个节点都是对象，`type` 必填；未列出的字段/类型会被服务端拒绝。

| type | 字段 |
| --- | --- |
| `card` | `title?`、`subtitle?`、`children`（节点数组） |
| `text` | `text`、`markdown?`（默认 false，只作为数据） |
| `image` | `url` 或 `delivery_id`（+`index?`）、`alt?`、`mime?`、`width?`、`height?` |
| `file` | `name`、`url` 或 `delivery_id`（+`index?`）、`mime?`、`size?` |
| `audio` | `url` 或 `delivery_id`（+`index?`）、`duration_ms?`、`transcript?` |
| `button` | `label`、`action` |
| `choice` | `name` 或 `id`、`options`（`[{id,label}]`）、`multiple?`、`action?` |
| `input` | `name`、`placeholder?`、`input_type?`（`text` / `secure`）、`label?`、`action?` |
| `progress` | `label?`、`percent?`（0–100）、`status?`（`running` / `done` / `error` / `idle`） |
| `list` | `items`（节点或字符串）、`ordered?` |
| `divider` | 无 |

`status` 可作为 `progress` 的类型别名，服务端会归一成 `progress`。

## 动作

```jsonc
// 客户端处理，不发请求
{"type": "client", "name": "open_desktop"}
{"type": "client", "name": "open_url", "url": "https://..."}

// 服务端处理，action_id 由服务端铸造
{"type": "server", "name": "decision.choose",  "action_id": "...", "decision_id": "...", "option": "..."}
{"type": "server", "name": "decision.reject",  "action_id": "...", "decision_id": "..."}
{"type": "server", "name": "secure_input.submit", "action_id": "..."}
```

客户端把 `action_id`（以及输入值）POST 到：

```
POST /api/v1/tasks/{task_id}/messages/{message_id}/actions
{
  "action_id": "...",
  "value": "安全输入的明文（仅 secure_input）",
  "option": "可选，覆盖动作里预置的选项",
  "note": "可选备注",
  "values": ["choice 多选 id，可选"]
}
```

服务端按 `action_id` 从**已落库的组件树**里找回动作，校验消息属于当前登录人的任务，
不执行客户端给的 URL 或内部动作名。未知/失效动作 404；跨用户任务 404。

卡片是写入时的快照：决策提交后会追加 `decision_result`，密钥提交后会追加
`secure_input_result`。客户端应以结果消息为准收起/禁用原卡片；重复提交会被服务端
拒绝（决策 409 / 密钥 409），不会执行两次。

## 既有类型的映射

| kind | 组件 |
| --- | --- |
| `file_card` | card + image/file/audio（`delivery_id` 引用） |
| `plan_card` | card + 有序 list（步骤 / 涉及文件 / 补充说明） |
| `handoff` | card + 「打开桌面接管」`open_desktop` |
| `confirmation` | card + 需求确认 list |
| `subtask` / `run_finished` | card + progress |
| `secure_input` | card + secure input（`secure_input.submit`） |
| `secure_input_result` | 仅文本回执，不含密钥材料 |
| `decision` / `decision_auto` / `decision_result` | card + 选项按钮（仅 pending 时） |

`attachments`（用户上传的图片/文件/语音）暂不映射：目前没有按 upload_id
取回内容的鉴权接口，App 拿到引用也取不到文件。

## 校验上限

深度 8、节点 200、单节点子项 50、文本 4000 字符、选项 20、整棵树 64KB；
URL 只允许 `https`（拒绝 `javascript:` / `data:` / `file:` / `http:`、带账号密码、
本机与私有地址）；文本不接受 HTML / 脚本片段。校验失败抛中文 `ValidationError`，
非法树**不会**写进数据库。

## 安全边界

- `component.emit` 只写一条对话消息，不发送 IM 附件、不执行命令，也不绕过审批规则；
- 模型不能用工具铸造 `type=server` 的动作；
- `secure_input` 的明文不进 `task_messages`（沿用既有密钥库逻辑）；
- `component` 不在上下文云同步的上传类型里（`CONTEXT_UPLOAD_KINDS` 保持不变）。
