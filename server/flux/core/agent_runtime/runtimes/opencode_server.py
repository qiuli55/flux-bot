"""OpenCode server 模式：直连 HTTP + SSE，拿到真正的 token 增量。

`opencode run --format json` 只在整段回答生成完后才吐一个 text 事件（1.18.29 实测），
手机端只能「长时间没动静 → 整段蹦出来」。`opencode serve` 的 `/event` SSE 会逐 token
发 `message.part.delta`，本模块把这条链路收在一个客户端里，由 CliRuntime 翻译成
RuntimeEvent，继续复用既有的事件总线与 SSE 管道。

约定：
- 服务端默认按 Run 隔离启动（独立端口、独立 OPENCODE_CONFIG），Run 结束即退出；
  声明 `reusable_process` 的 runtime 由 CliRuntime 复用同一 serve 进程（任务级隔离），
  本模块只负责保证「本轮结束后会话确实静默」，能否复用由 `TurnResult.reusable` 决定；
- 一轮 = `prompt_async` 立即返回（204）+ SSE/状态轮询收口 + `GET /session/{id}/message`
  拉终态：不再用一条阻塞 HTTP 连接等整轮，长回答不会被读超时整轮作废；
- 终态消息（assistant 的 parts / error / finish）是权威，SSE 只负责过程与增量；
- 只有本 session 的事件才处理，用户消息回显与其它会话一律忽略。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from flux.core.agent_runtime.runtimes.base import DEFAULT_RUNTIME_LIMITS, RuntimeLimits
from flux.logging import get_logger

logger = get_logger(__name__)

#: 健康检查与单轮请求的上限（推理型模型首字可能很慢，但必须有个硬上限）
HEALTH_TIMEOUT_SECONDS = 60.0
#: 单次健康探测的超时：刚拉起的 serve 可能先接受连接再卡住不回包，
#: 用整轮超时去等会把一轮 Run 白白拖上十分钟
HEALTH_REQUEST_TIMEOUT_SECONDS = 2.0
#: 建会话的超时：冷启动时 serve 往往先回健康检查、再慢慢登记会话，
#: 10 秒会把偶发的慢登记误判成失败，这里留出冷启动余量
SESSION_TIMEOUT_SECONDS = 30.0
#: 建会话的尝试次数：超时/连接抖动各重试一次，仍失败再报错
SESSION_CREATE_ATTEMPTS = 2
#: 查询会话状态的单次超时：它只是 SSE 的兜底，不能反过来拖住收口
STATUS_REQUEST_TIMEOUT_SECONDS = 5.0
#: 拉终态消息时回看的条数（一轮可能有多条 assistant 消息：工具步 + 最终答复）
TERMINAL_FETCH_LIMIT = 20
#: 提交后先给 serve 一点时间把会话标成 busy：否则首个状态查询可能还是 idle
TURN_SETTLE_SECONDS = 0.5
#: 收到 idle 后再给 SSE 一点收尾时间：最后几个增量通常紧跟其后
DRAIN_SECONDS = 2.0
#: abort 后等会话真正静默的上限：进程要复用，必须先确认本轮已停止写会话
ABORT_QUIESCE_SECONDS = 8.0
#: 增量合并：太碎会让 SSE/手机端每 token 刷一帧，攒到这个量或间隔再推
FLUSH_CHARS = 24
FLUSH_INTERVAL_SECONDS = 0.08

#: SSE 的读超时必须关闭（长思考期间没有事件）；其余三档要显式给值，
#: 否则 httpx 会拒绝「只设默认值/只设 connect+read」的 Timeout 组合
SSE_TIMEOUT = httpx.Timeout(connect=5.0, read=None, write=10.0, pool=10.0)

TextHandler = Callable[[str], Awaitable[None]]
ThinkingHandler = Callable[[str, str], Awaitable[None]]
PartHandler = Callable[[dict[str, Any]], Awaitable[None]]
ErrorHandler = Callable[[str], Awaitable[None]]
#: 原生 bash 审批回调：给命令文本，返回 True 放行 / False 拒绝
ApprovalHandler = Callable[[str], Awaitable[bool]]


def pick_port() -> int:
    """挑一个空闲的本地端口：先绑定再释放，交给 opencode serve。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def serve_argv(executable: str, port: int) -> tuple[str, ...]:
    """opencode serve 的启动参数（日志仍走 stderr，额度错误由 stderr 监控兜底）。"""
    return (
        executable,
        "serve",
        "--hostname",
        "127.0.0.1",
        "--port",
        str(port),
        "--print-logs",
        "--log-level",
        "ERROR",
    )


@dataclass
class TurnResult:
    """一轮对话的结果：终态正文与错误（二者互斥，正文优先用于答复）。"""

    text: str = ""
    error: str = ""
    #: 本轮实际使用的会话 id（用于跨轮复用；空串表示没拿到）
    session_id: str = ""
    #: 是否被截断（超时 / 输出长度上限 / 被中止）：正文仍可用，属"截断完成"
    truncated: bool = False
    #: 截断原因（给下一轮与用户看的短句）
    truncated_reason: str = ""
    #: 本轮结束后 serve 进程是否仍可复用（出错/未能静默时为 False，调用方应驱逐）
    reusable: bool = True


class _DeltaBuffer:
    """把 token 增量攒成小段：保持「逐段长出」的观感，又不至于每 token 推一帧。"""

    def __init__(self, send: Callable[[str], Awaitable[None]]) -> None:
        self._send = send
        self._pending = ""
        self._all: list[str] = []
        self._last = time.monotonic()

    def add(self, text: str) -> None:
        self._pending += text
        self._all.append(text)

    @property
    def pending(self) -> bool:
        return bool(self._pending)

    @property
    def all_text(self) -> str:
        """本轮收到的全部增量：POST 异常时仍能把已生成的部分作为正文。"""
        return "".join(self._all)

    def due(self) -> bool:
        if not self._pending:
            return False
        return (
            len(self._pending) >= FLUSH_CHARS
            or time.monotonic() - self._last >= FLUSH_INTERVAL_SECONDS
        )

    async def flush(self) -> None:
        text, self._pending = self._pending, ""
        self._last = time.monotonic()
        if text:
            await self._send(text)


class OpenCodeServerClient:
    """一轮对话的 HTTP/SSE 客户端；进程生命周期由 CliRuntime 管。"""

    def __init__(
        self,
        base_url: str,
        *,
        on_text: TextHandler,
        on_thinking: ThinkingHandler,
        on_tool: PartHandler,
        on_step: PartHandler,
        on_error: ErrorHandler,
        process_alive: Callable[[], bool] | None = None,
        approval_handler: ApprovalHandler | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._on_text = on_text
        self._on_thinking = on_thinking
        self._on_tool = on_tool
        self._on_step = on_step
        self._on_error = on_error
        self._alive = process_alive or (lambda: True)
        self._approval = approval_handler
        #: 外部（看护器超时/用户取消）要求本轮尽快收口；返回 True 即中止本轮
        self._should_stop = should_stop or (lambda: False)
        self._client: httpx.AsyncClient | None = None
        self._idle = asyncio.Event()
        self._error = ""
        self._assistant_ids: set[str] = set()
        self._part_types: dict[str, str] = {}
        self._text = _DeltaBuffer(on_text)
        self._reasoning = _DeltaBuffer(lambda chunk: on_thinking(chunk, ""))
        #: 最近一次"有进展"的单调时刻（增量 / 工具 / 状态变化），用于 idle 判定
        self._last_progress = time.monotonic()
        #: 服务端确认过 busy：没确认之前不接受"状态=idle"作为收口依据
        self._seen_busy = False
        #: 本轮被截断的原因（超时/中止）；空串 = 正常结束
        self._truncated_reason = ""
        #: 本轮是否已确认服务端停止写会话（abort 后仍未静默 = 进程不可复用）
        self._quiesced = True
        #: 本轮提交时刻（epoch ms）：拉终态时据此排除上一轮遗留的 assistant 消息
        self._turn_started_ms = 0.0

    # --- 对外 ---

    @property
    def streamed_text(self) -> str:
        """本轮已收到的全部增量正文：超时/取消时由看护器取走，避免整轮作废。"""
        return self._text.all_text

    def reusable(self) -> bool:
        """本轮结束后 serve 进程还能不能继续承接下一轮。

        出错、进程已死、或中止后仍未静默（可能还在写会话）都不能复用——
        复用脏进程会让下一轮的增量与终态互相污染。
        """
        return not self._error and self._alive() and self._quiesced

    async def run(
        self,
        instruction: str,
        *,
        title: str = "",
        session_id: str = "",
        model: str = "",
        limits: RuntimeLimits | None = None,
    ) -> TurnResult:
        """执行一轮；传入 `session_id` 时优先续用该会话（不存在则自动新建）。

        opencode 的会话存储随 HOME 持久化，所以上一轮 serve 退出后仍能续聊；
        `TurnResult.session_id` 回传本轮实际使用的会话，调用方据此记住绑定。
        `model`（`provider/model`）是本轮要用的模型：会话与消息接口都会记住模型，
        必须每轮显式下发，否则切换模型后仍会沿用旧会话的模型。

        回合模型：`prompt_async` 只等"提交被接受"，收口靠 SSE `session.idle` +
        `GET /session/status` 轮询兜底，最后用 `GET /session/{id}/message` 拉终态。
        `limits` 提供提交/空闲/绝对上限与轮询步长（见 runtimes/base.py）。
        """
        limits = limits or DEFAULT_RUNTIME_LIMITS
        if model and not all(_split_model(model)):
            logger.warning(
                "opencode 模型标识无法解析（应为 provider/model）：%s，本轮沿用会话默认模型",
                model,
            )
        # 控制面请求（提交 / 建会话 / 查状态 / 拉终态）都用提交上限；
        # 长回合本身不再占用一条 HTTP 连接，读超时只影响单次控制请求。
        timeout = httpx.Timeout(max(limits.submit_timeout_seconds, 1.0), connect=5.0)
        # 目标永远是本机 serve：不能跟着系统代理走，否则开着 HTTP_PROXY 的机器
        # 会把 127.0.0.1 的请求转发出去，健康检查永远不通过、整轮 Run 卡在 running。
        async with httpx.AsyncClient(
            base_url=self._base_url, timeout=timeout, trust_env=False
        ) as client:
            self._client = client
            # 提交时刻（epoch ms）：拉终态时据此排除上一轮遗留的 assistant 消息
            self._turn_started_ms = time.time() * 1000.0
            await self._wait_ready()
            target = await self._resolve_session(session_id, title, model)
            listener = asyncio.create_task(self._listen(target), name=f"opencode-sse-{target}")
            submitted = False
            try:
                # 先订阅再发消息：首字增量可能紧跟在请求之后，不能漏
                await asyncio.sleep(0.05)
                try:
                    await self._prompt_async(target, instruction, model)
                    submitted = True
                except httpx.HTTPStatusError as exc:
                    # 记住的会话可能已被用户清理（opencode 对不存在的会话常回 500）：
                    # 只要本轮本来就在续用旧会话，就重开一个新会话再发一次。
                    if not session_id or exc.response.status_code not in (404, 410, 500):
                        raise
                    logger.warning("opencode 会话 %s 已不存在，改为新建会话重发本轮消息", target)
                    await _cancel(listener)
                    target = await self._create_session(title, model)
                    self._reset_turn_state()
                    listener = asyncio.create_task(
                        self._listen(target), name=f"opencode-sse-{target}"
                    )
                    await asyncio.sleep(0.05)
                    await self._prompt_async(target, instruction, model)
                    submitted = True
            except Exception as exc:  # noqa: BLE001 - 网络/上游错误统一转成失败原因
                message = _http_error_text(exc)
                if not self._error:
                    self._error = message
            finally:
                if submitted and not self._error:
                    await self._await_turn(target, limits=limits)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._idle.wait(), DRAIN_SECONDS)
                await self._flush()
                listener.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await listener
            body = await self._terminal_message(target) if submitted else {}
            final = _final_text(body)
            truncated = bool(self._truncated_reason)
            reason = self._truncated_reason
            reusable = self.reusable()
            if final:
                return TurnResult(
                    text=final,
                    session_id=target,
                    truncated=truncated,
                    truncated_reason=reason,
                    reusable=reusable,
                )
            streamed = self._text.all_text.strip()
            if streamed:
                return TurnResult(
                    text=streamed,
                    session_id=target,
                    truncated=truncated,
                    truncated_reason=reason,
                    reusable=reusable,
                )
            if self._error:
                return TurnResult(error=self._error, session_id=target, reusable=reusable)
            return TurnResult(
                session_id=target,
                truncated=truncated,
                truncated_reason=reason,
                reusable=reusable,
            )

    def _reset_turn_state(self) -> None:
        """换会话重发前清掉上一轮的会话内状态，避免把旧增量算进本轮。"""
        self._assistant_ids.clear()
        self._part_types.clear()
        self._idle.clear()
        self._error = ""
        self._seen_busy = False
        self._truncated_reason = ""
        self._quiesced = True
        self._last_progress = time.monotonic()
        self._turn_started_ms = time.time() * 1000.0

    # --- 回合收口 ---

    async def _await_turn(self, session_id: str, *, limits: RuntimeLimits) -> None:
        """等本轮结束：SSE idle 优先，`/session/status` 轮询兜底，超时转截断。

        不依赖任何一条长连接：SSE 断线、丢事件时仍能靠状态轮询判定结束；
        空闲/绝对上限到点、或看护器/用户要求停止时 abort 并保留已生成正文
        （由调用方标 truncated）。
        """
        started = time.monotonic()
        poll = max(limits.poll_interval_seconds, 0.2)
        idle_timeout = max(limits.idle_timeout_seconds, 0.0)
        hard_timeout = max(limits.hard_timeout_seconds, 0.0)
        await asyncio.sleep(min(TURN_SETTLE_SECONDS, poll))
        while True:
            if self._idle.is_set() or self._error:
                return
            if not self._alive():
                self._error = self._error or "opencode 服务端进程已退出，本轮被中断"
                return
            if self._should_stop():
                # 看护器超时 / 用户取消：主动中止本轮，产出仍保留
                self._truncated_reason = self._truncated_reason or "收到停止请求，本轮被中止"
                await self._abort_and_quiesce(session_id)
                return
            now = time.monotonic()
            if hard_timeout > 0 and now - started > hard_timeout:
                self._truncated_reason = f"超过单轮绝对上限 {hard_timeout:g} 秒"
                await self._abort_and_quiesce(session_id)
                return
            if idle_timeout > 0 and now - self._last_progress > idle_timeout:
                self._truncated_reason = f"{idle_timeout:g} 秒内无任何进展"
                await self._abort_and_quiesce(session_id)
                return
            status = await self._session_status(session_id)
            if status in ("busy", "retry"):
                self._seen_busy = True
            elif status == "idle" and (self._seen_busy or now - started > TURN_SETTLE_SECONDS * 4):
                # 已经确认过 busy（或早已过提交抖动期）且服务端说空闲 = 本轮结束
                return
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._idle.wait(), poll)

    async def _session_status(self, session_id: str) -> str:
        """`GET /session/status`：本会话不在返回表里 = idle（opencode 语义）。"""
        assert self._client is not None
        try:
            response = await self._client.get(
                "/session/status", timeout=STATUS_REQUEST_TIMEOUT_SECONDS
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return ""
        if not isinstance(payload, dict):
            return ""
        entry = payload.get(session_id)
        if not isinstance(entry, dict):
            return "idle"
        return str(entry.get("type") or "")

    async def _abort_and_quiesce(self, session_id: str) -> None:
        """中止本轮并等会话真正静默：进程若还要复用，必须确认它已停止写会话。"""
        self._quiesced = False
        await self._abort(session_id)
        deadline = time.monotonic() + ABORT_QUIESCE_SECONDS
        while True:
            status = await self._session_status(session_id)
            if status not in ("busy", "retry"):
                self._quiesced = True
                return
            if not self._alive() or time.monotonic() >= deadline:
                logger.warning(
                    "opencode 中止后仍未静默 session=%s status=%s（进程将被回收）",
                    session_id,
                    status or "unknown",
                )
                return
            await asyncio.sleep(0.25)

    async def _abort(self, session_id: str) -> None:
        """请求服务端停止本轮（超时/取消收口用）；失败不改变已判定的截断结论。"""
        assert self._client is not None
        try:
            await self._client.post(
                f"/session/{session_id}/abort", timeout=STATUS_REQUEST_TIMEOUT_SECONDS
            )
        except httpx.HTTPError:
            logger.warning("opencode 中止本轮失败 session=%s", session_id)

    async def _terminal_message(self, session_id: str) -> dict[str, Any]:
        """拉本轮最后一条 assistant 消息，转成与旧 POST 响应同形的 `{"parts": [...]}`。"""
        assert self._client is not None
        try:
            response = await self._client.get(
                f"/session/{session_id}/message",
                params={"limit": TERMINAL_FETCH_LIMIT},
                timeout=SESSION_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            messages = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("opencode 拉取终态消息失败 session=%s：%s", session_id, exc)
            return {}
        if not isinstance(messages, list):
            return {}
        chosen: dict[str, Any] | None = None
        for item in messages:
            if not isinstance(item, dict):
                continue
            info = item.get("info")
            if not isinstance(info, dict) or info.get("role") != "assistant":
                continue
            # 只认本轮产生的 assistant 消息（SSE 已记过 id 时按 id 过滤）
            if self._assistant_ids and str(info.get("id") or "") not in self._assistant_ids:
                continue
            # SSE 断线/提交抖动时兜底：按创建时间排除上一轮遗留的答复
            created = _created_ms(info)
            if created and self._turn_started_ms and created < self._turn_started_ms - 1000.0:
                continue
            chosen = item
        if chosen is None:
            return {}
        info = chosen.get("info") if isinstance(chosen.get("info"), dict) else {}
        error = info.get("error") if isinstance(info, dict) else None
        if error:
            name = str(error.get("name") or "") if isinstance(error, dict) else ""
            if name == "MessageAbortedError":
                # 被中止（含我们的 abort）：正文仍是有效产出
                self._truncated_reason = self._truncated_reason or "本轮被中止"
            else:
                self._error = self._error or _error_text(error) or "opencode 会话出错"
        finish = str(info.get("finish") or "") if isinstance(info, dict) else ""
        if finish in ("length", "max_tokens"):
            self._truncated_reason = self._truncated_reason or "模型输出达到长度上限"
        parts = chosen.get("parts")
        return {"parts": parts if isinstance(parts, list) else []}

    # --- 服务端就绪 ---

    async def _wait_ready(self) -> None:
        assert self._client is not None
        deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._should_stop():
                # 池化进程不杀：停止请求只会置标志，这里必须自己退出等待
                raise RuntimeError("收到停止请求，本轮未开始即被中止")
            if not self._alive():
                raise RuntimeError("opencode 服务端进程已退出，未能开始本轮对话")
            try:
                response = await self._client.get(
                    "/global/health", timeout=HEALTH_REQUEST_TIMEOUT_SECONDS
                )
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.1)
        raise RuntimeError(f"opencode 服务端 {HEALTH_TIMEOUT_SECONDS:.0f} 秒内未就绪")

    async def _create_session(self, title: str, model: str = "") -> str:
        assert self._client is not None
        payload: dict[str, Any] = {"title": title} if title else {}
        # 新建会话时就带上模型：否则会话默认落回全局配置里的默认模型
        session_model = _session_model(model)
        if session_model:
            payload["model"] = session_model
        # 冷启动的 serve 偶尔会先挂起再回包：超时/连接抖动重试一次，避免整轮直接失败。
        last_error: httpx.HTTPError | None = None
        for attempt in range(1, SESSION_CREATE_ATTEMPTS + 1):
            try:
                response = await self._client.post(
                    "/session", json=payload, timeout=SESSION_TIMEOUT_SECONDS
                )
                response.raise_for_status()
                session_id = response.json().get("id")
                if not isinstance(session_id, str) or not session_id:
                    raise RuntimeError("opencode 未返回有效的会话 id")
                return session_id
            except (httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError) as exc:
                last_error = exc
                logger.warning(
                    "opencode 建会话失败（第 %s/%s 次）：%s",
                    attempt,
                    SESSION_CREATE_ATTEMPTS,
                    exc,
                )
                if attempt < SESSION_CREATE_ATTEMPTS:
                    await asyncio.sleep(0.5)
        raise RuntimeError(f"opencode 建会话超时（{SESSION_TIMEOUT_SECONDS:.0f} 秒）：{last_error}")

    async def _resolve_session(self, session_id: str, title: str, model: str = "") -> str:
        """优先复用已有会话；确认不可用时新建（不因一条过期绑定让整轮失败）。"""
        assert self._client is not None
        if session_id:
            try:
                response = await self._client.get(
                    f"/session/{session_id}", timeout=SESSION_TIMEOUT_SECONDS
                )
                if response.status_code == 200:
                    logger.info("opencode 复用已有会话 session=%s", session_id)
                    return session_id
                logger.warning(
                    "opencode 会话 %s 不可用（HTTP %s），改为新建",
                    session_id,
                    response.status_code,
                )
            except httpx.HTTPError as exc:
                logger.warning("opencode 校验会话 %s 失败（%s），改为新建", session_id, exc)
        return await self._create_session(title, model)

    async def _prompt_async(self, session_id: str, instruction: str, model: str = "") -> None:
        """提交本轮消息：204 = 已接受，回合结束由 `_await_turn` 收口。

        用异步提交（而不是阻塞的 `POST /message`）是关键：长回答不会再占住一条
        HTTP 连接，读超时只作用于"提交"这一个动作，不会把整轮产出作废。
        """
        assert self._client is not None
        payload: dict[str, Any] = {"parts": [{"type": "text", "text": instruction}]}
        # 会话会记住创建/上次使用的模型：续聊时必须每轮显式带本轮模型，
        # 否则任务切了模型仍沿用旧会话的模型（表现为「切换没生效」）。
        message_model = _message_model(model)
        if message_model:
            payload["model"] = message_model
        response = await self._client.post(
            f"/session/{session_id}/prompt_async",
            json=payload,
        )
        response.raise_for_status()

    # --- SSE 事件 ---

    async def _listen(self, session_id: str) -> None:
        assert self._client is not None
        try:
            # SSE 没有"多久没数据就失败"的语义：read 超时必须关掉，
            # 否则模型长时间思考（无事件）会被客户端单方面掐断。
            async with self._client.stream("GET", "/event", timeout=SSE_TIMEOUT) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload:
                        continue
                    try:
                        obj = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    await self._handle(obj, session_id)
                    if self._idle.is_set():
                        return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - SSE 断开不影响终态：GET message 的 parts 才是权威
            logger.warning("opencode SSE 监听中断 session=%s（改由状态轮询收口）", session_id)
            return

    async def _handle(self, obj: dict[str, Any], session_id: str) -> None:
        props = obj.get("properties")
        props = props if isinstance(props, dict) else {}
        if str(props.get("sessionID") or "") not in ("", session_id):
            return
        # 任何属于本会话的事件都算"有进展"：idle 判定不能把长时间工具调用误判成卡死
        self._last_progress = time.monotonic()
        event = str(obj.get("type") or "")
        if event == "message.updated":
            info = props.get("info")
            if isinstance(info, dict) and info.get("role") == "assistant":
                message_id = info.get("id")
                if isinstance(message_id, str):
                    self._assistant_ids.add(message_id)
            return
        if event == "message.part.updated":
            part = props.get("part")
            if not isinstance(part, dict):
                return
            part_type = str(part.get("type") or "")
            part_id = part.get("id")
            if isinstance(part_id, str):
                self._part_types[part_id] = part_type
            # text / reasoning 的整段快照不重复推：增量走 part.delta，终态走 POST parts
            if part_type == "tool":
                await self._on_tool(part)
                return
            if part_type == "step-start":
                await self._on_thinking("", "step_start")
                return
            if part_type == "step-finish":
                await self._on_step(part)
                return
            return
        if event == "message.part.delta":
            message_id = props.get("messageID")
            if (
                isinstance(message_id, str)
                and self._assistant_ids
                and message_id not in self._assistant_ids
            ):
                return
            delta = props.get("delta")
            if not isinstance(delta, str) or not delta:
                return
            part_id = props.get("partID")
            part_type = (
                self._part_types.get(part_id, "text") if isinstance(part_id, str) else "text"
            )
            if part_type == "reasoning":
                self._reasoning.add(delta)
                if self._reasoning.due():
                    await self._reasoning.flush()
            else:
                self._text.add(delta)
                if self._text.due():
                    await self._text.flush()
            return
        if event == "permission.asked":
            # 原生 bash 的审批请求（`permission.bash=ask`）：交回 Flux 的
            # 风险分类与用户审批，再按结论回复 once / reject
            await self._resolve_permission(props, session_id)
            return
        if event == "session.error":
            message = _error_text(props.get("error"))
            self._error = message or "opencode 会话出错"
            await self._on_error(self._error)
            self._idle.set()
            return
        if event == "session.idle":
            self._idle.set()

    async def _flush(self) -> None:
        await self._reasoning.flush()
        await self._text.flush()

    async def _resolve_permission(self, props: dict[str, Any], session_id: str) -> None:
        """把 opencode 的 `permission.asked` 转成 Flux 审批结论（失败一律拒绝）。"""
        permission_id = props.get("id")
        if not isinstance(permission_id, str) or not permission_id:
            return
        command = _permission_command(props)
        allowed = False
        if command and self._approval is not None:
            try:
                allowed = await self._approval(command)
            except Exception:  # noqa: BLE001 - 审批链异常按拒绝处理，不能默认放行
                logger.exception("opencode 原生命令审批回调异常，按拒绝处理 command=%s", command)
                allowed = False
        response = "once" if allowed else "reject"
        logger.info(
            "opencode 原生命令审批 session=%s permission=%s 结论=%s command=%s",
            session_id,
            permission_id,
            response,
            command[:200],
        )
        assert self._client is not None
        try:
            await self._client.post(
                f"/session/{session_id}/permissions/{permission_id}",
                json={"response": response},
                timeout=SESSION_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError:
            # 回复失败不拖垮整轮：opencode 侧会一直等，最终由 Run 超时收口
            logger.exception("opencode 审批回复失败 permission=%s", permission_id)


def _split_model(model: str) -> tuple[str, str]:
    """把 `provider/model` 拆成 (providerID, modelID)；不含 `/` 视为无有效选择。"""
    provider_id, _, model_id = str(model or "").strip().partition("/")
    if not provider_id or not model_id:
        return "", ""
    return provider_id, model_id


def _message_model(model: str) -> dict[str, str]:
    """`POST /session/{id}/message` 的 model 字段（providerID + modelID）。"""
    provider_id, model_id = _split_model(model)
    if not provider_id:
        return {}
    return {"providerID": provider_id, "modelID": model_id}


def _session_model(model: str) -> dict[str, str]:
    """`POST /session` 的 model 字段（这里模型 id 的键是 `id`）。"""
    provider_id, model_id = _split_model(model)
    if not provider_id:
        return {}
    return {"id": model_id, "providerID": provider_id}


def _permission_command(props: dict[str, Any]) -> str:
    """从 `permission.asked` 里取命令文本：优先 metadata.command，其次 patterns。"""
    metadata = props.get("metadata")
    if isinstance(metadata, dict):
        command = metadata.get("command")
        if isinstance(command, str) and command.strip():
            return command.strip()
    patterns = props.get("patterns")
    if isinstance(patterns, list):
        joined = " && ".join(str(item).strip() for item in patterns if str(item).strip())
        return joined
    return ""


def _final_text(body: dict[str, Any]) -> str:
    """终态消息里的正文 parts 拼成最终答复（`synthetic` 是服务端注入，不算答复）。"""
    parts = body.get("parts")
    if not isinstance(parts, list):
        return ""
    texts = [
        str(item.get("text") or "").strip()
        for item in parts
        if isinstance(item, dict) and item.get("type") == "text" and not item.get("synthetic")
    ]
    return "\n".join(text for text in texts if text)


def _created_ms(info: dict[str, Any]) -> float:
    """消息创建时刻（epoch ms）；缺失或非数值时返回 0（视为未知，不做时间过滤）。"""
    block = info.get("time")
    if not isinstance(block, dict):
        return 0.0
    value = block.get("created")
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _error_text(error: Any) -> str:
    if not isinstance(error, dict):
        return str(error or "").strip()
    data = error.get("data")
    if isinstance(data, dict):
        message = data.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    message = error.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    name = error.get("name")
    return str(name or "").strip()


async def _cancel(task: asyncio.Task[Any]) -> None:
    """取消并回收一个后台任务（换会话时旧 SSE 监听必须停掉）。"""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


def _http_error_text(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        detail = ""
        with contextlib.suppress(Exception):
            payload = exc.response.json()
            if isinstance(payload, dict):
                detail = str(payload.get("message") or payload.get("error") or "")
        return detail.strip() or f"opencode 服务端返回 {exc.response.status_code}"
    return str(exc).strip() or exc.__class__.__name__


__all__ = [
    "OpenCodeServerClient",
    "TurnResult",
    "pick_port",
    "serve_argv",
]
