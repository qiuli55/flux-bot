"""事件总线后端测试：进程内（默认）与 Redis pub/sub（可选）。

本文件不连接真实 Redis，也不发任何网络请求：Redis 场景全部注入同步/异步假客户端，
只验证「本地分发 + 广播 + 收远端消息 + 关闭有序」这些契约。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from flux.config import Settings
from flux.core.event.bus import EventBus, Events, build_event_bus
from flux.core.event.redis_bus import DEFAULT_CHANNEL, RedisEventBus


class FakePubSub:
    """最小的 redis.asyncio.client.PubSub 假实现。"""

    def __init__(self, *, fail_subscribe: bool = False) -> None:
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.closed = False
        self.cancelled = False
        #: 监听任务确实跑进 get_message 的同步点（否则 cancel 可能发生在任务启动前）。
        self.entered = asyncio.Event()
        self._fail_subscribe = fail_subscribe

    async def subscribe(self, channel: str) -> None:
        if self._fail_subscribe:
            raise ConnectionError("redis 不可达（测试）")
        self.subscribed.append(channel)

    async def get_message(
        self, ignore_subscribe_messages: bool = False, timeout: float | None = None
    ) -> dict[str, Any] | None:
        self.entered.set()
        try:
            return await self.queue.get()
        except asyncio.CancelledError:
            # aclose() 必须 await 监听任务，否则这个标记来不及置位。
            self.cancelled = True
            raise

    async def unsubscribe(self, channel: str) -> None:
        self.unsubscribed.append(channel)

    async def aclose(self) -> None:
        self.closed = True


class FakeRedis:
    """最小的 redis.asyncio.Redis 假实现。"""

    def __init__(self, *, fail_subscribe: bool = False, fail_publish: bool = False) -> None:
        self.published: list[tuple[str, str]] = []
        self.pubsub_obj = FakePubSub(fail_subscribe=fail_subscribe)
        self.closed = False
        self._fail_publish = fail_publish

    def pubsub(self) -> FakePubSub:
        return self.pubsub_obj

    async def publish(self, channel: str, message: str) -> int:
        if self._fail_publish:
            raise ConnectionError("redis 发布失败（测试）")
        self.published.append((channel, message))
        return 1

    async def aclose(self) -> None:
        self.closed = True


def _settings(**updates: Any) -> Settings:
    """不读开发机 .env 的配置，避免环境变量让用例随机器漂移。"""
    return Settings(_env_file=None, **updates)


async def _wait_for(predicate: Any, timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("等待条件超时")


# --- 默认内存后端 ---


async def test_memory_backend_is_default_and_behaves_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认设置仍是进程内总线：本地分发 + history，不触碰 Redis。"""
    monkeypatch.delenv("FLUX_EVENT_BUS_BACKEND", raising=False)
    settings = _settings()
    assert settings.event_bus_backend == "memory"

    bus = build_event_bus(settings)
    assert type(bus) is EventBus
    assert not isinstance(bus, RedisEventBus)

    received: list[tuple[str, dict[str, Any]]] = []
    bus.subscribe(Events.TASK_CREATED, lambda event, payload: received.append((event, payload)))

    called = await bus.publish(Events.TASK_CREATED, {"task_id": "t-1"})

    assert called == 1
    assert received == [(Events.TASK_CREATED, {"task_id": "t-1"})]
    assert bus.history == [(Events.TASK_CREATED, {"task_id": "t-1"})]
    bus.clear()
    assert bus.history == []


async def test_unknown_backend_falls_back_to_memory() -> None:
    """配置笔误不能拖垮启动，未知后端回退进程内实现。"""
    bus = build_event_bus(_settings(event_bus_backend="carrier-pigeon"))
    assert type(bus) is EventBus


# --- Redis 后端 ---


async def test_redis_backend_is_opt_in() -> None:
    """只有显式 event_bus_backend=redis 才选 Redis；频道名随设置。"""
    bus = build_event_bus(
        _settings(
            event_bus_backend="redis",
            redis_url="redis://example.invalid:6379/3",
            event_bus_redis_channel="test.events",
        )
    )
    assert isinstance(bus, RedisEventBus)
    assert bus.channel == "test.events"
    # 未 start 时没有任务；构造不会建连（本用例也不应该发起网络请求）。
    await bus.aclose()


async def test_container_uses_redis_bus_when_configured(
    settings: Settings, db_schema: None
) -> None:
    """容器按设置装配总线：默认进程内，显式 redis 时才换成 Redis 实现。"""
    from flux.container import Container

    memory = Container(settings)
    assert type(memory.bus) is EventBus
    await memory.dispose()

    redis_container = Container(settings.model_copy(update={"event_bus_backend": "redis"}))
    assert isinstance(redis_container.bus, RedisEventBus)
    # 构造与关闭都不建连：本用例仍不触碰真实 Redis。
    await redis_container.dispose()


async def test_redis_publish_broadcasts_and_calls_local_handlers() -> None:
    """publish 既调用本地订阅者，也把 {event,payload} 发到 Redis 频道。"""
    client = FakeRedis()
    bus = RedisEventBus("redis://unused/0", client=client)
    await bus.start()
    assert bus.running
    assert client.pubsub_obj.subscribed == [DEFAULT_CHANNEL]

    received: list[dict[str, Any]] = []

    async def handler(_event: str, payload: dict[str, Any]) -> None:
        received.append(payload)

    bus.subscribe(Events.TASK_CREATED, handler)
    called = await bus.publish(Events.TASK_CREATED, {"task_id": "t-1"})

    assert called == 1
    assert received == [{"task_id": "t-1"}]
    assert bus.history == [(Events.TASK_CREATED, {"task_id": "t-1"})]
    assert len(client.published) == 1
    channel, raw = client.published[0]
    assert channel == DEFAULT_CHANNEL
    envelope = json.loads(raw)
    assert envelope["event"] == Events.TASK_CREATED
    assert envelope["payload"] == {"task_id": "t-1"}
    assert envelope["origin"]

    await bus.aclose()


async def test_redis_remote_message_invokes_local_handlers() -> None:
    """从 Redis 收到的外部消息要进 history 并调用本地订阅者。"""
    client = FakeRedis()
    bus = RedisEventBus("redis://unused/0", client=client)
    await bus.start()

    received: list[tuple[str, dict[str, Any]]] = []
    bus.subscribe(Events.TASK_FAILED, lambda event, payload: received.append((event, payload)))

    await client.pubsub_obj.queue.put(
        {
            "type": "message",
            "channel": DEFAULT_CHANNEL,
            "data": json.dumps(
                {
                    "event": Events.TASK_FAILED,
                    "payload": {"task_id": "remote-1"},
                    "origin": "another-process",
                }
            ),
        }
    )
    await _wait_for(lambda: bool(received))

    assert received == [(Events.TASK_FAILED, {"task_id": "remote-1"})]
    assert bus.history == [(Events.TASK_FAILED, {"task_id": "remote-1"})]

    await bus.aclose()


async def test_redis_skips_own_echo_from_pubsub() -> None:
    """自己 publish 的消息被 Redis 回环回来时不能再派发一次。"""
    client = FakeRedis()
    bus = RedisEventBus("redis://unused/0", client=client)
    await bus.start()

    received: list[str] = []

    def handler(event: str, _payload: dict[str, Any]) -> None:
        received.append(event)

    bus.subscribe(Events.TASK_CREATED, handler)
    bus.subscribe(Events.TASK_COMPLETED, handler)

    await bus.publish(Events.TASK_CREATED, {"task_id": "t-echo"})
    assert received == [Events.TASK_CREATED]

    raw = client.published[0][1]
    await client.pubsub_obj.queue.put({"type": "message", "channel": DEFAULT_CHANNEL, "data": raw})
    # 再发一条外部消息作为同步点：它被处理时，前面那条回环消息必然已经处理过。
    await client.pubsub_obj.queue.put(
        {
            "type": "message",
            "channel": DEFAULT_CHANNEL,
            "data": json.dumps(
                {"event": Events.TASK_COMPLETED, "payload": {"task_id": "remote"}, "origin": "peer"}
            ),
        }
    )
    await _wait_for(lambda: Events.TASK_COMPLETED in received)

    assert received.count(Events.TASK_CREATED) == 1
    assert received == [Events.TASK_CREATED, Events.TASK_COMPLETED]

    await bus.aclose()


async def test_listener_is_cancelled_and_awaited_on_close() -> None:
    """关闭时必须 cancel 且 await 监听任务，再释放 pubsub；注入的客户端不代关。"""
    client = FakeRedis()
    bus = RedisEventBus("redis://unused/0", client=client)
    await bus.start()
    listener = bus._listener
    assert listener is not None
    assert bus.running
    # 等监听任务真正阻塞在 get_message 上，再触发关闭（否则 cancel 可能发生在启动前）。
    await asyncio.wait_for(client.pubsub_obj.entered.wait(), timeout=1.0)

    await bus.aclose()

    assert listener.done()
    assert listener.cancelled()
    # 标记只在监听任务的 CancelledError 分支里置位：说明 aclose() 等到了它结束。
    assert client.pubsub_obj.cancelled is True
    assert client.pubsub_obj.closed is True
    assert client.pubsub_obj.unsubscribed == [DEFAULT_CHANNEL]
    assert bus.running is False
    assert bus._listener is None
    assert client.closed is False  # 注入的客户端归调用方所有

    # 重复关闭是幂等的。
    await bus.aclose()

    # 关闭后仍可本地 publish，但不再尝试重建 Redis 连接。
    published_before = len(client.published)
    await bus.publish(Events.TASK_CREATED, {"task_id": "after-close"})
    assert len(client.published) == published_before
    assert bus.history == [(Events.TASK_CREATED, {"task_id": "after-close"})]


async def test_redis_start_degrades_when_unreachable() -> None:
    """订阅失败只降级为进程内分发，不抛异常、不留下监听任务。"""
    client = FakeRedis(fail_subscribe=True)
    bus = RedisEventBus("redis://unused/0", client=client)

    await bus.start()

    assert bus.running is False
    assert bus._listener is None
    assert client.pubsub_obj.closed is True

    received: list[dict[str, Any]] = []
    bus.subscribe(Events.TASK_CREATED, lambda _event, payload: received.append(payload))
    called = await bus.publish(Events.TASK_CREATED, {"task_id": "t-local"})

    assert called == 1
    assert received == [{"task_id": "t-local"}]
    assert bus.history == [(Events.TASK_CREATED, {"task_id": "t-local"})]

    await bus.aclose()


async def test_redis_publish_failure_keeps_local_delivery() -> None:
    """发布失败只记日志，本地订阅者与 history 不受影响。"""
    client = FakeRedis(fail_publish=True)
    bus = RedisEventBus("redis://unused/0", client=client)
    await bus.start()

    received: list[dict[str, Any]] = []
    bus.subscribe(Events.TASK_CREATED, lambda _event, payload: received.append(payload))
    called = await bus.publish(Events.TASK_CREATED, {"task_id": "t-local", "secret": "do-not-log"})

    assert called == 1
    assert received == [{"task_id": "t-local", "secret": "do-not-log"}]
    assert bus.history == [(Events.TASK_CREATED, {"task_id": "t-local", "secret": "do-not-log"})]

    await bus.aclose()
