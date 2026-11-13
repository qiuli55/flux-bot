"""入站消息的身份闸门：先绑定，再决定是否建对话轮次。

三个正式频道共用同一套规则：

1. 解析不出稳定的外部用户 id → 不按任何人处理，直接回中文说明；
2. 整条消息像一个绑定码 → 原子消费并绑定；成功只回系统消息，**不建 turn、
   不调模型、不走审批**；
3. 已绑定 → 返回该 `cloud_user_id`，后续 turn / 记忆 / 密钥 / 连接器 / 例程
   都以这个人身份运行；
4. 未绑定且不是绑定码 → 回中文绑定指引，**不建 turn、不下载媒体、不碰任何
   人的记忆 / 密钥 / 连接器**。

系统消息直接走频道发送接口，不经过 outbound_drafts / 审批卡：它是平台提示，
不是 Bot 以某个人格生成的内容。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from flux.core.channels.bindings import looks_like_bind_code
from flux.logging import get_logger

logger = get_logger(__name__)

Reply = Callable[[str], Awaitable[None]]

CHANNEL_LABELS = {"feishu": "飞书", "wecom": "企业微信", "dingtalk": "钉钉"}

#: 缺少发送者 id 时的系统提示（不含任何人的数据）。
MISSING_SENDER_TEXT = (
    "这条消息没有带可识别的发送者 ID，我无法判断你是谁，因此不会以任何人的身份处理它。"
    "请在企业微信 / 飞书 / 钉钉的正式客户端里直接与机器人对话。"
)

#: 未绑定时的绑定指引；明确说明在此之前不会使用任何人的私有数据。
UNBOUND_PROMPT_TEXT = (
    "你还没有绑定 Flux 账号，所以这条消息不会以任何人的身份处理，"
    "我也不会读取任何人的私有记忆、密钥或连接器。\n"
    "绑定方法：登录 Flux Bot → 在账号设置里生成一次性绑定码 → "
    "把 8 位绑定码直接发到这个对话里。绑定成功后就可以正常聊天了。"
)

#: 绑定码无效 / 已过期 / 已使用时的提示。
BIND_CODE_INVALID_TEXT = (
    "这个绑定码无效、已过期或已经被使用。请回到 Flux Bot 重新生成一个绑定码，"
    "再把新的 8 位绑定码发到这个对话里。"
)


async def resolve_text_inbound(
    container: Any,
    *,
    channel: str,
    external_user_id: str,
    text: str,
    reply: Reply,
) -> str | None:
    """文本入站：返回应运行本次对话的 cloud_user_id；None 表示已按系统消息处理。"""
    external = (external_user_id or "").strip()
    if not external:
        await _safe_reply(reply, MISSING_SENDER_TEXT)
        return None

    service = getattr(container, "channel_bindings", None)
    if service is None:
        # 未装配绑定服务时 fail-closed：不把消息当成任何人的对话。
        await _safe_reply(reply, UNBOUND_PROMPT_TEXT)
        return None

    if looks_like_bind_code(text):
        user = await service.redeem_code(text)
        if user is not None:
            await service.bind(
                channel=channel,
                external_user_id=external,
                cloud_user_id=user,
            )
            label = CHANNEL_LABELS.get(channel, channel)
            await _safe_reply(
                reply,
                f"绑定成功：这个{label}账号已绑定到 Flux 账号 {user}。现在可以直接聊天了。",
            )
            logger.info(
                "channel.binding.redeemed channel=%s external=%s cloud_user_id=%s",
                channel,
                external,
                user,
            )
            return None
        # 码的形状对但无效：已绑定的人仍可按普通消息继续聊；
        # 未绑定的人只收到绑定码无效提示，不会建 turn。
        binding = await service.get_binding(channel, external)
        if binding is None:
            await _safe_reply(reply, BIND_CODE_INVALID_TEXT)
            return None
        return binding.cloud_user_id

    binding = await service.get_binding(channel, external)
    if binding is not None:
        return binding.cloud_user_id

    await _safe_reply(reply, UNBOUND_PROMPT_TEXT)
    return None


async def resolve_media_inbound(
    container: Any,
    *,
    channel: str,
    external_user_id: str,
    reply: Reply,
) -> str | None:
    """媒体入站：未绑定就不下载、不落盘、不建 turn；已绑定返回 cloud_user_id。"""
    external = (external_user_id or "").strip()
    if not external:
        await _safe_reply(reply, MISSING_SENDER_TEXT)
        return None

    service = getattr(container, "channel_bindings", None)
    if service is None:
        await _safe_reply(reply, UNBOUND_PROMPT_TEXT)
        return None

    binding = await service.get_binding(channel, external)
    if binding is not None:
        return binding.cloud_user_id

    await _safe_reply(reply, UNBOUND_PROMPT_TEXT)
    return None


async def _safe_reply(reply: Reply, text: str) -> None:
    """系统消息发送失败只记日志：不能因为提示发不出去而让平台无限重推。"""
    try:
        await reply(text)
    except Exception:  # noqa: BLE001 - 系统提示是尽力而为
        logger.warning("频道系统提示发送失败", exc_info=True)


__all__ = [
    "BIND_CODE_INVALID_TEXT",
    "CHANNEL_LABELS",
    "MISSING_SENDER_TEXT",
    "Reply",
    "UNBOUND_PROMPT_TEXT",
    "resolve_media_inbound",
    "resolve_text_inbound",
]
