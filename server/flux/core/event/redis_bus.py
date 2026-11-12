"""Redis pub/sub 事件总线（可选后端，默认不启用）。

设计取舍：
- 对外契约与 `EventBus` 完全一致，只是 publish 额外把 `{event, payload, origin}`
  发到 Redis 频道；本进程订阅者始终先被调用，因此单进程部署即使 Redis 不可用
  也仍然工作（降级为进程内分发）。
- 收到的远端消息只做本地分发，`origin` 等于自己的消息会被跳过，避免本进程
  publish 时被 Redis 回环再派发一次。
- history 始终只保存在本进程内存里；Redis 不承担历史存储（测试多处置依赖
  `bus.history`，不能因换后端而要求外部服务）。
- 连接失败一律降级并记录日志，不阻塞容器启动；监听任务不会自动重连，运维
  需要重启进程恢复跨进程广播。日志只记录事件名与载荷键/大小，不落完整载荷。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from typing import Any

from flux.core.event.bus import EventBus
from flux.logging import get_logger

logger = get_logger(__name__)

#: 默认频道名；与 Settings.event_bus_redis_channel 的默认值保持一致。
DEFAULT_CHANNEL = "flux.events"


class RedisEventBus(EventBus):
    """进程内分发 + Redis pub/sub 跨进程广播。

    `client` 可注入（测试用假客户端）；注入的客户端归调用方所有，`aclose()`
    不会关闭它，只关闭本类自己创建的 pubsub/客户端。
    """

    def __init__(
        self,
        redis_url: str,
        *,
        channel: str = DEFAULT_CHANNEL,
        client: Any | None = None,
    ) -> None:
        super().__init__()
        self._redis_url = redis_url
        self._channel = channel or DEFAULT_CHANNEL
        self._client = client
        self._owns_client = client is None
        self._pubsub: Any | None = None
        self._listener: asyncio.Task[None] | None = None
        #: 关闭后不再重新建连广播；仅保留本地分发。
        self._closed = False
        #: 本进程实例标识：用于跳过 Redis 回环回来的自己发的消息。
        self._origin = uuid.uuid4().hex

    @property
    def channel(self) -> str:
        return self._channel

    @property
    def running(self) -> bool:
        """监听任务是否仍在运行。"""
        return self._listener is not None and not self._listener.done()

    def _get_client(self) -> Any:
        if self._client is None:
            # 延迟到真正需要连接时才导入/创建客户端；构造 from_url 本身不建连。
            import redis.asyncio as redis_asyncio

            self._client = redis_asyncio.from_url(
                self._redis_url,
                decode_responses=True,
                socket_connect_timeout=5.0,
                socket_timeout=5.0,
            )
        return self._client

    async def start(self) -> None:
        """订阅频道并拉起监听任务；连接失败只告警并保持进程内可用。"""
        if self.running:
            return
        self._closed = False
        # 上次监听异常退出时可能留下没关闭的 pubsub，先清理再重订阅。
        if self._pubsub is not None:
            await self._close_pubsub()
        client = self._get_client()
        try:
            pubsub = client.pubsub()
            self._pubsub = pubsub
            await pubsub.subscribe(self._channel)
        except Exception:  # noqa: BLE001 - Redis 不可达时降级为进程内总线
            logger.warning(
                "Redis 事件总线连接失败，跨进程广播降级为进程内分发 channel=%s",
                self._channel,
                exc_info=True,
            )
            await self._close_pubsub()
            return
        self._listener = asyncio.create_task(self._listen(pubsub), name="flux-event-bus-redis")
        logger.info("Redis 事件总线已启动 channel=%s", self._channel)

    async def _listen(self, pubsub: Any) -> None:
        """从 Redis 读取消息并做本地分发，直到任务被取消。"""
        try:
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if not isinstance(message, dict) or message.get("type") != "message":
                    continue
                await self._dispatch_remote(message.get("data"))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 监听中断不拖垮本地分发
            logger.exception("Redis 事件监听中断 channel=%s（不再自动重连）", self._channel)

    async def _dispatch_remote(self, raw: Any) -> None:
        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                logger.warning("Redis 事件消息不是合法 UTF-8，已丢弃 channel=%s", self._channel)
                return
        if not isinstance(raw, str):
            return
        try:
            envelope = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning(
                "Redis 事件消息不是合法 JSON，已丢弃 channel=%s size=%s",
                self._channel,
                len(raw),
            )
            return
        if not isinstance(envelope, dict):
            logger.warning("Redis 事件消息结构非法，已丢弃 channel=%s", self._channel)
            return
        if envelope.get("origin") == self._origin:
            # 自己发的消息：发布时已在本地分发过，回环直接忽略。
            return
        event = envelope.get("event")
        payload = envelope.get("payload")
        if not isinstance(event, str) or not isinstance(payload, dict):
            logger.warning(
                "Redis 事件消息缺少 event/payload，已丢弃 channel=%s keys=%s",
                self._channel,
                ",".join(sorted(str(key) for key in envelope)),
            )
            return
        body = dict(payload)
        self._history.append((event, body))
        await self._invoke(event, body)

    async def publish(self, event: str, payload: dict[str, Any] | None = None) -> int:
        """先本地分发，再广播到 Redis；Redis 失败只降级，不影响本地订阅者。"""
        body = dict(payload or {})
        self._history.append((event, body))
        called = await self._invoke(event, body)
        if self._closed:
            # 已关闭：只做本地分发，不再重新建连广播。
            return called
        envelope = {"event": event, "payload": body, "origin": self._origin}
        try:
            encoded = json.dumps(envelope, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            logger.warning("事件载荷无法 JSON 序列化，跳过 Redis 广播 event=%s", event)
            return called
        try:
            await self._get_client().publish(self._channel, encoded)
        except Exception:  # noqa: BLE001 - Redis 故障不得阻断本地流程
            logger.warning(
                "Redis 事件发布失败，仅本地分发 event=%s %s",
                event,
                _payload_summary(body),
            )
        return called

    async def aclose(self) -> None:
        """取消并等待监听任务，再释放 pubsub 与自己创建的客户端。"""
        self._closed = True
        listener = self._listener
        self._listener = None
        if listener is not None:
            if not listener.done():
                listener.cancel()
            try:
                await listener
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - 关闭阶段不再向外抛监听异常
                logger.debug("Redis 事件监听任务退出时异常", exc_info=True)
        await self._close_pubsub()
        if self._owns_client and self._client is not None:
            client = self._client
            self._client = None
            await _close_first(client, "aclose", "close")

    async def _close_pubsub(self) -> None:
        pubsub = self._pubsub
        self._pubsub = None
        if pubsub is None:
            return
        try:
            await _maybe_await_call(pubsub, "unsubscribe", self._channel)
        except Exception:  # noqa: BLE001 - 关闭阶段尽力而为
            logger.debug("Redis 取消订阅失败 channel=%s", self._channel, exc_info=True)
        try:
            await _close_first(pubsub, "aclose", "close")
        except Exception:  # noqa: BLE001
            logger.debug("Redis pubsub 关闭失败 channel=%s", self._channel, exc_info=True)


async def _maybe_await_call(obj: Any, name: str, *args: Any) -> bool:
    """调用对象上的指定方法（兼容同步/异步实现）；方法不存在时返回 False。"""
    method = getattr(obj, name, None)
    if method is None:
        return False
    outcome = method(*args)
    if inspect.isawaitable(outcome):
        await outcome
    return True


async def _close_first(obj: Any, *names: str) -> None:
    """依次尝试关闭方法，命中第一个存在的即返回（redis-py 用 aclose，旧版用 close）。"""
    for name in names:
        if await _maybe_await_call(obj, name):
            return


def _payload_summary(body: dict[str, Any]) -> str:
    """载荷摘要：只给键名与大小，避免把可能含密钥的正文写进日志。"""
    keys = ",".join(sorted(str(key) for key in body))
    return f"keys=[{keys}] size={len(body)}"
