"""Codex app-server 模式：JSON-RPC over stdio，拿真正的 token 增量。

`codex exec --json` 只在 item 完成时吐整段结果，无法逐 token；但 codex 自己的
`codex app-server`（JSON-RPC）会发 `item/agentMessage/delta`、`item/reasoning/textDelta`
等增量通知（schema 见 `codex app-server generate-json-schema`，2026-10-25 快照）。

这里只做一件事：把一轮 `thread/start` → `turn/start` 的通知翻译成 Flux 的
text / thinking / tool 事件，交给 CliRuntime 现有的队列与终态收口逻辑。
进程生命周期仍由 CliRuntime + RunSupervisor 管（pid/pgid/心跳/取消升级都不变）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from flux.logging import get_logger

logger = get_logger(__name__)

#: 攒批粒度：与 DSH 旁路一致，8 字符推一帧，模型停顿时最多等 50ms
FLUSH_CHARS = 8
FLUSH_INTERVAL_SECONDS = 0.05

INITIALIZE_TIMEOUT_SECONDS = 60.0
THREAD_START_TIMEOUT_SECONDS = 60.0
TURN_START_TIMEOUT_SECONDS = 60.0
#: 本轮无任何通知的最长等待：正常工具执行也会持续有输出，超过即视为卡死
IDLE_TIMEOUT_SECONDS = 600.0
#: 连续「将重试」且整轮零输出的容忍时间。codex app-server 对网络类错误会无上限重试
#: （实测 chatgpt.com 不可达时每 10~70 秒重试一次、永不结束），不在这里快速失败
#: 就只能干等到 idle 超时（10 分钟），用户看到的是长时间无响应。
RETRY_FAIL_SECONDS = 60.0
#: 上下文压缩（thread/compact/start）本身也是一轮 LLM 调用；给它一个独立的绝对上限，
#: 避免压缩卡住把整轮 Run 一起拖死。
COMPACT_TIMEOUT_SECONDS = 180.0

TextHandler = Callable[[str], Awaitable[None]]
ThinkingHandler = Callable[[str], Awaitable[None]]
ToolHandler = Callable[[dict[str, Any], str], Awaitable[None]]
#: 原生命令审批：给一条命令，返回是否放行
ApprovalHandler = Callable[[str], Awaitable[bool]]


def app_server_argv(executable: str) -> tuple[str, ...]:
    """codex app-server 的启动 argv（默认 stdio 传输）。"""
    return (executable, "app-server", "--listen", "stdio://")


@dataclass(slots=True)
class CodexTurnResult:
    text: str = ""
    status: str = "completed"
    error: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    #: 本轮实际使用的 thread id（用于跨轮续聊）
    thread_id: str = ""
    #: 本轮结束时该 thread 的上下文规模（token，0 = 未报送）；下一轮据此判断要不要压缩
    context_tokens: int = 0


class CodexAppServerSession:
    """一条 app-server 进程上的多轮对话（initialize 只做一次，stdout 只读一条线程）。

    进程跨 Run 复用时，同一时刻只有一个 Run 租用它：每轮先 `bind()` 换绑本轮回调、
    时限与审批入口，再 `run()`。这样常驻进程既不会把上一轮的正文/思考串进新的一轮，
    也不用每轮重新握手（`initialize` + 起读线程）。
    """

    def __init__(self, process: Any) -> None:
        self._popen = process.popen
        #: 本轮绑定的参数（bind 逐轮覆盖；未 bind 不允许 run）
        self._cwd = ""
        self._on_text: TextHandler | None = None
        self._on_thinking: ThinkingHandler | None = None
        self._on_tool: ToolHandler | None = None
        self._approval_handler: ApprovalHandler | None = None
        self._should_stop: Callable[[], bool] | None = None
        self._permission_mode = "normal"
        self._idle_timeout = IDLE_TIMEOUT_SECONDS
        self._retry_fail_seconds = RETRY_FAIL_SECONDS
        #: 上下文预算（0 = 不压缩）：续聊的 thread 上一轮超过该规模就先压缩再执行
        self._compact_tokens = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._notifications: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id = 0
        self._closed = False
        #: 上一轮异常中断（超时/取消/握手失败）：进程里可能还挂着半轮 turn，不得再复用
        self._broken = False
        self._reader_started = False
        self._initialized = False
        #: 当前进程内已打开的 thread：同一 thread 再 resume 一次会被判重复打开
        self._active_thread = ""
        self._pump: _TextPump | None = None
        #: item_id → 是否收到过增量（没收过增量的 agentMessage 用整段兜底）
        self._delta_items: set[str] = set()

    # --- 对外 ---
    def bind(
        self,
        *,
        cwd: str,
        on_text: TextHandler,
        on_thinking: ThinkingHandler,
        on_tool: ToolHandler,
        idle_timeout: float = IDLE_TIMEOUT_SECONDS,
        retry_fail_seconds: float = RETRY_FAIL_SECONDS,
        permission_mode: str = "normal",
        approval_handler: ApprovalHandler | None = None,
        context_compact_tokens: int = 0,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        """把会话换绑到当前 Run：回调、时限、权限模式、审批入口与停止信号。"""
        self._cwd = cwd
        self._on_text = on_text
        self._on_thinking = on_thinking
        self._on_tool = on_tool
        self._permission_mode = permission_mode
        self._approval_handler = approval_handler
        self._should_stop = should_stop
        self._idle_timeout = idle_timeout if idle_timeout > 0 else IDLE_TIMEOUT_SECONDS
        self._retry_fail_seconds = (
            retry_fail_seconds if retry_fail_seconds > 0 else RETRY_FAIL_SECONDS
        )
        self._compact_tokens = max(0, int(context_compact_tokens or 0))

    @property
    def usable(self) -> bool:
        """进程还活着、也没有半轮 turn 残留：只有这时才允许继续复用。"""
        return not self._closed and not self._broken and self._popen.poll() is None

    def _thread_start_params(self) -> dict[str, Any]:
        """`thread/start` 参数：Plan 只读，Normal / Full 让 codex 把不确定的命令交上来。

        codex 自带的 shell / apply_patch 不经过 Flux 的审批闸门，只靠 MCP 白名单挡不住：
        - Plan：sandbox 收到 `read-only`，从沙箱层面兑现"不改代码、不写工作区"；
        - Normal / Full：`approvalPolicy=untrusted` 只让"codex 自己也不确定"的命令走审批
          回调，Normal 由用户点、Full 只问受保护对象；低风险命令 codex 自己就放行了。
        """
        plan = self._permission_mode == "plan"
        return {
            "cwd": self._cwd,
            "approvalPolicy": "never" if plan else "untrusted",
            "sandbox": "read-only" if plan else "workspace-write",
        }

    async def run(
        self,
        instruction: str,
        *,
        thread_id: str = "",
        model: str = "",
        model_provider: str = "",
        context_tokens: int = 0,
    ) -> CodexTurnResult:
        """执行一轮；传入 `thread_id` 时优先续用该 thread（恢复失败则新建）。

        `model` / `model_provider` 是任务当前的模型选择：codex 的 thread 会记住创建时的
        模型，续聊时不显式覆盖就会一直用旧模型（实测 2026-10-27：任务切到 gpt-5.6-luna
        后仍按 thread 里的 gpt-6-sol 请求，原生 ChatGPT 通道直接 400 model_not_found）。

        `context_tokens` 是该 thread 上一轮的上下文规模（平台持久化后传入）：续用且超过
        `context_compact_tokens` 预算时，先让 codex 压缩上下文再跑本轮，避免长任务把
        上下文撑到模型窗口上限后每轮都变慢 / 直接被拒。

        同一进程可连续跑多轮：握手与 stdout 读线程只在第一轮建立，之后复用。
        """
        if self._on_text is None or self._on_thinking is None or self._on_tool is None:
            raise RuntimeError("codex app-server 会话未绑定本轮回调")
        if not self.usable:
            if self._broken:
                raise RuntimeError("codex app-server 会话已作废（上一轮异常中断）")
            raise RuntimeError(self._exit_reason())
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError("codex app-server 会话已绑定其他事件循环，不能跨循环复用")
        self._loop = loop
        self._pump = _TextPump(self._on_text, self._on_thinking)
        self._delta_items.clear()
        # 上一轮结束后可能还有迟到通知（用量/状态）：开新轮前清空，避免串进本轮正文
        self._discard_notifications()
        if not self._reader_started:
            self._reader_started = True
            threading.Thread(
                target=self._read_stdout,
                args=(loop,),
                name="codex-app-server-stdout",
                daemon=True,
            ).start()
        if not self._initialized:
            await self._request(
                "initialize",
                {"clientInfo": {"name": "flux", "title": "Flux", "version": "1"}},
                timeout=INITIALIZE_TIMEOUT_SECONDS,
            )
            self._initialized = True
        overrides = _model_overrides(model, model_provider)
        previous = thread_id.strip()
        thread_id = ""
        if previous and previous == self._active_thread:
            # 同一进程里该 thread 已经打开：再次 resume 会被判成重复打开，直接续用
            thread_id = previous
        elif previous:
            try:
                resumed = await self._request(
                    "thread/resume",
                    {"threadId": previous, **self._thread_start_params(), **overrides},
                    timeout=THREAD_START_TIMEOUT_SECONDS,
                )
                thread_id = _thread_id(resumed) or previous
                logger.info("codex 复用已有 thread=%s", thread_id)
            except Exception as exc:  # noqa: BLE001 - 旧 thread 失效时退回新建
                logger.warning("codex thread %s 恢复失败（%s），改为新建", previous, exc)
        if not thread_id:
            response = await self._request(
                "thread/start",
                {**self._thread_start_params(), **overrides},
                timeout=THREAD_START_TIMEOUT_SECONDS,
            )
            thread_id = _thread_id(response)
            if not thread_id:
                raise RuntimeError("codex app-server 没有返回 threadId")
        elif self._compact_tokens > 0 and context_tokens >= self._compact_tokens:
            # 只有在「真的续用了旧 thread」时才压缩：新建的 thread 上下文是空的
            await self._compact(thread_id, context_tokens)
            if self._broken:
                # 压缩轮被取消/超时：进程里挂着半轮 turn，本轮直接失败，由调用方驱逐
                raise RuntimeError("本轮已被取消")
        self._active_thread = thread_id
        # `turn/start` 没有 modelProvider 字段；模型仍要显式带一次，覆盖 resumed thread 的旧值
        turn_params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": instruction}],
        }
        if model.strip():
            turn_params["model"] = model.strip()
        await self._request(
            "turn/start",
            turn_params,
            timeout=TURN_START_TIMEOUT_SECONDS,
        )
        try:
            result = await self._consume_turn()
        except BaseException:
            # 超时/取消/进程退出：进程里可能还挂着半轮 turn，标记作废，不再复用
            self._broken = True
            raise
        result.thread_id = thread_id
        return result

    def _discard_notifications(self) -> None:
        """丢掉上一轮迟到/遗留的通知（含进程关闭哨兵）。"""
        while True:
            try:
                self._notifications.get_nowait()
            except asyncio.QueueEmpty:
                return

    # --- 上下文压缩 ---
    async def _compact(self, thread_id: str, context_tokens: int) -> None:
        """让 codex 压缩该 thread 的上下文；失败只记日志，本轮照常执行。

        `thread/compact/start` 立即返回空对象，真正的压缩是**另一轮 turn**：随后会收到
        `turn/started` → `item/started(contextCompaction)` → `thread/tokenUsage/updated`
        → `turn/completed`（实测 2026-11-01，13k 上下文压缩耗时 12s，压缩后 5.3k）。
        必须把这一轮完整消化掉，否则它的通知会串进正式回合的正文/时间线。
        """
        logger.info(
            "codex 上下文超预算（%d ≥ %d），先压缩 thread=%s",
            context_tokens,
            self._compact_tokens,
            thread_id,
        )
        try:
            await self._request(
                "thread/compact/start",
                {"threadId": thread_id},
                timeout=TURN_START_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 - 旧版 codex 没这个方法，降级为不压缩
            logger.warning("codex 上下文压缩请求失败（%s），本轮继续", exc)
            return
        try:
            after = await self._consume_compaction()
        except Exception as exc:  # noqa: BLE001 - 压缩失败不影响主任务
            logger.warning("codex 上下文压缩未正常结束（%s），本轮继续", exc)
            return
        logger.info(
            "codex 上下文压缩完成 thread=%s %d → %s tokens",
            thread_id,
            context_tokens,
            after if after is not None else "未知",
        )

    async def _consume_compaction(self) -> int | None:
        """消化压缩轮的全部通知，返回压缩后的上下文规模；压缩失败抛异常。"""
        deadline = time.monotonic() + COMPACT_TIMEOUT_SECONDS
        context_tokens: int | None = None
        while True:
            if self._should_stop is not None and self._should_stop():
                # 取消/超时：不杀常驻进程，但压缩轮还挂在它身上，标记作废后由调用方驱逐
                await self._pump.flush()
                self._broken = True
                raise RuntimeError("本轮已被取消")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"codex 上下文压缩 {COMPACT_TIMEOUT_SECONDS:g} 秒未完成")
            try:
                message = await asyncio.wait_for(self._notifications.get(), timeout=remaining)
            except asyncio.TimeoutError:
                continue
            if message is None:
                if self._closed:
                    raise RuntimeError("codex app-server 已退出（上下文压缩期间）")
                continue
            method = str(message.get("method") or "")
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            if method == "thread/tokenUsage/updated":
                context_tokens = _context_tokens(params) or context_tokens
            elif method == "turn/completed":
                turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                status = str(turn.get("status") or "completed")
                if status != "completed":
                    raise RuntimeError(_turn_error(turn) or f"codex 上下文压缩以 {status} 结束")
                return context_tokens
            elif method == "error":
                error = params.get("error")
                text = (
                    str(error.get("message") or "") if isinstance(error, dict) else str(error or "")
                )
                if not params.get("willRetry"):
                    raise RuntimeError(text or "codex 上下文压缩失败")
            # 其余通知（item 增量 / 状态变化）都属于压缩轮本身，丢弃，不进时间线

    # --- 读 stdout（线程）---
    def _read_stdout(self, loop: asyncio.AbstractEventLoop) -> None:
        stream = self._popen.stdout
        if stream is None:
            self._post(loop, {"method": "__closed__"})
            return
        try:
            for line in stream:
                text = line.strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except ValueError:
                    logger.debug("codex app-server 非 JSON 输出：%s", text[:200])
                    continue
                if not isinstance(message, dict):
                    continue
                self._post(loop, message)
        except Exception:  # noqa: BLE001 - 进程被杀时读流中断属正常
            logger.debug("codex app-server stdout 读取中断", exc_info=True)
        finally:
            # stdout 关闭时进程可能还没真正退出（Windows 上 .cmd 包装进程尤其如此），
            # 先等它落地再上报，否则拿不到退出码，失败原因里只剩一句"已退出"。
            with contextlib.suppress(Exception):
                self._popen.wait(timeout=5)
            self._post(loop, {"method": "__closed__"})

    def _post(self, loop: asyncio.AbstractEventLoop, message: dict[str, Any]) -> None:
        """线程 → 事件循环投递；服务退出时循环可能已关闭，重投失败不影响进程回收。"""
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(self._dispatch, message)

    def _dispatch(self, message: dict[str, Any]) -> None:
        """在事件循环线程里分发一条 JSON-RPC 消息。"""
        if message.get("method") == "__closed__":
            self._closed = True
            reason = "codex app-server 已退出"
            code: int | None = None
            with contextlib.suppress(Exception):
                code = self._popen.poll()
            if code is not None:
                reason = f"{reason}（退出码 {code}）"
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(RuntimeError(reason))
            self._pending.clear()
            self._notifications.put_nowait(None)
            return
        method = message.get("method")
        if method is None and "id" in message:
            future = self._pending.pop(message.get("id"), None)
            if future is not None and not future.done():
                if "error" in message:
                    error = message.get("error")
                    text = error.get("message") if isinstance(error, dict) else str(error)
                    future.set_exception(RuntimeError(str(text or "codex app-server 返回错误")))
                else:
                    future.set_result(message.get("result"))
            return
        if method is not None and "id" in message:
            # 服务端 → 客户端的请求（审批/动态工具）：必须回包，否则 codex 会一直等
            asyncio.create_task(self._answer_server_request(message))
            return
        if method is not None:
            self._notifications.put_nowait(message)

    async def _answer_server_request(self, message: dict[str, Any]) -> None:
        method = str(message.get("method") or "")
        request_id = message.get("id")
        params = message.get("params")
        decision: dict[str, Any] | None = None
        if "requestApproval" in method or method == "execCommandApproval":
            allowed = await self._approve(method, params)
            legacy = method in _LEGACY_APPROVALS
            if allowed:
                decision = {"decision": "approved" if legacy else "accept"}
            else:
                # 拒绝后 codex 会继续本轮、另想办法，不会卡住
                decision = (
                    {"decision": {"denied": {"rejection": "用户拒绝了这条命令"}}}
                    if legacy
                    else {"decision": "decline"}
                )
        elif method in _LEGACY_APPROVALS:
            decision = {"decision": "approved"}
        if decision is None:
            logger.warning("codex app-server 收到不支持的请求，已拒绝 method=%s", method)
            payload = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"Flux 不支持 {method}"},
            }
        else:
            payload = {"jsonrpc": "2.0", "id": request_id, "result": decision}
        try:
            await self._write(payload)
        except Exception:  # noqa: BLE001 - 进程已退出时回包失败不影响主链路
            logger.debug("codex app-server 回包失败 method=%s", method, exc_info=True)

    async def _approve(self, method: str, params: Any) -> bool:
        """决定是否放行 codex 原生 shell 的一条命令。

        codex 自带的 shell 不经过 Flux MCP 的 `terminal.execute` 闸门。Normal / Full 用
        `approvalPolicy=untrusted` 让 codex 把「它自己也不确定」的命令交上来，再按 Flux 的
        风险分类决定：低风险直接放行；高风险（Normal）与受保护对象（任何模式）转成审批卡
        等用户点。没有回调时保持旧的自动批准，避免测试/旧链路被卡住。
        """
        if self._approval_handler is None:
            return True
        if self._permission_mode == "plan":
            # Plan 是只读：沙箱已经挡住写入，这里再兜一层
            return False
        command = _command_text(params)
        if not command:
            # 非命令类审批（如 applyPatch）：维持原有自动批准
            return True
        try:
            return await self._approval_handler(command)
        except Exception:  # noqa: BLE001 - 审批链路异常时失败关闭，命令不执行
            logger.warning(
                "codex 原生命令审批失败，按拒绝处理 method=%s cmd=%s",
                method,
                command[:120],
                exc_info=True,
            )
            return False

    # --- 写 stdin ---
    async def _request(self, method: str, params: dict[str, Any], *, timeout: float) -> Any:
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
        except OSError as exc:
            # 进程已经退出（管道关闭）：用退出码而不是 BrokenPipe 当失败原因
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            raise RuntimeError(self._exit_reason()) from exc
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"codex app-server {method} 超时（{timeout:g}s）") from exc

    def _exit_reason(self) -> str:
        reason = "codex app-server 已退出"
        code: int | None = None
        with contextlib.suppress(Exception):
            code = self._popen.poll()
            if code is None:
                # 写入 stdin 失败时子进程可能刚退出、还没被回收：
                # 稍等一下再取退出码，否则失败原因会丢掉最有用的「退出码 N」。
                code = self._popen.wait(timeout=0.2)
        if code is not None:
            reason = f"{reason}（退出码 {code}）"
        return reason

    async def _write(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False) + "\n"
        await asyncio.to_thread(self._write_sync, data)

    def _write_sync(self, data: str) -> None:
        stdin = self._popen.stdin
        if stdin is None:
            raise RuntimeError("codex app-server 没有 stdin 管道")
        stdin.write(data)
        stdin.flush()

    # --- 一轮通知 ---
    async def _consume_turn(self) -> CodexTurnResult:
        assert self._pump is not None
        texts: list[str] = []
        final_text: str | None = None
        usage: dict[str, Any] = {}
        #: 本轮请求的上下文规模：下一轮据此判断要不要压缩（0 = 未报送）
        context_tokens = 0
        deadline = time.monotonic() + self._idle_timeout
        #: 本轮第一次「将重试」错误的时间与次数：整轮零输出且持续重试 → 快速失败
        retry_started: float | None = None
        retry_count = 0
        last_retry_message = ""
        while True:
            if self._should_stop is not None and self._should_stop():
                # 取消/超时：不杀常驻进程，但本轮 turn 还挂在它身上，标记作废后由调用方驱逐
                await self._pump.flush()
                self._broken = True
                return CodexTurnResult(
                    text="".join(texts),
                    status="failed",
                    error="本轮已被取消",
                    usage=usage,
                    context_tokens=context_tokens,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"codex app-server 本轮 {self._idle_timeout:g} 秒无任何输出")
            wait = min(FLUSH_INTERVAL_SECONDS, remaining)
            if retry_started is not None and not texts and not self._pump.has_thinking:
                # 重试窗口也要能被等超时唤醒检查，否则无通知时永远等不到失败
                wait = min(
                    wait,
                    max(
                        0.001,
                        retry_started + self._retry_fail_seconds - time.monotonic(),
                    ),
                )
            try:
                message = await asyncio.wait_for(self._notifications.get(), timeout=wait)
            except asyncio.TimeoutError:
                await self._pump.flush()
                if (
                    retry_started is not None
                    and not texts
                    and not self._pump.has_thinking
                    and time.monotonic() - retry_started >= self._retry_fail_seconds
                ):
                    return CodexTurnResult(
                        text="",
                        status="failed",
                        error=(
                            f"codex 已连续重试 {retry_count} 次仍未连上上游"
                            f"（{last_retry_message or '未知网络错误'}）。"
                            "请检查网络后重发，或切换其他 Agent。"
                        ),
                        usage=usage,
                    )
                continue
            if message is None:
                if self._closed:
                    raise RuntimeError("codex app-server 已退出（可能被取消或崩溃）")
                continue
            deadline = time.monotonic() + self._idle_timeout
            method = str(message.get("method") or "")
            params = message.get("params") if isinstance(message.get("params"), dict) else {}

            if method == "item/agentMessage/delta":
                delta = str(params.get("delta") or "")
                item_id = str(params.get("itemId") or "")
                if delta:
                    self._delta_items.add(item_id)
                    texts.append(delta)
                    retry_started = None
                    retry_count = 0
                    last_retry_message = ""
                    self._pump.add("text", delta)
                    await self._pump.maybe_flush()
            elif method in (
                "item/reasoning/textDelta",
                "item/reasoning/summaryTextDelta",
            ):
                delta = str(params.get("delta") or "")
                if delta:
                    retry_started = None
                    retry_count = 0
                    last_retry_message = ""
                    self._pump.add("thinking", delta)
                    await self._pump.maybe_flush()
            elif method == "item/started":
                item = params.get("item") if isinstance(params.get("item"), dict) else {}
                await self._emit_tool(item, "started")
            elif method == "item/completed":
                item = params.get("item") if isinstance(params.get("item"), dict) else {}
                item_type = str(item.get("type") or "")
                if item_type == "agentMessage":
                    text = str(item.get("text") or "")
                    item_id = str(item.get("id") or "")
                    if text and item_id not in self._delta_items:
                        # 老版本不推 delta 时的唯一兜底：整段只推一次
                        self._delta_items.add(item_id)
                        texts.append(text)
                        self._pump.add("text", text)
                    final_text = text or final_text
                elif item_type == "reasoning":
                    # 没有增量时用完成态的推理摘要兜底
                    if not self._pump.has_thinking:
                        text = _reasoning_text(item)
                        if text:
                            self._pump.add("thinking", text)
                else:
                    await self._emit_tool(item, "completed")
            elif method == "thread/tokenUsage/updated":
                token_usage = params.get("tokenUsage")
                if isinstance(token_usage, dict):
                    last = token_usage.get("last")
                    if isinstance(last, dict):
                        usage = {
                            "input": last.get("inputTokens"),
                            "output": last.get("outputTokens"),
                            "cached_input_tokens": last.get("cachedInputTokens"),
                        }
                context_tokens = _context_tokens(params) or context_tokens
            elif method == "turn/completed":
                turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                status = str(turn.get("status") or "completed")
                error = _turn_error(turn)
                await self._pump.flush()
                return CodexTurnResult(
                    text="".join(texts) or (final_text or ""),
                    status=status,
                    error=error,
                    usage=usage,
                    context_tokens=context_tokens,
                )
            elif method == "error":
                error = params.get("error")
                message_text = (
                    str(error.get("message") or "") if isinstance(error, dict) else str(error or "")
                )
                if params.get("willRetry"):
                    retry_count += 1
                    last_retry_message = message_text
                    if retry_started is None:
                        retry_started = time.monotonic()
                    # 到点由上面的等待循环统一判失败（这里只记录，避免漏掉无通知的空转）
                    logger.warning("codex app-server 错误（将重试）：%s", message_text)
                else:
                    await self._pump.flush()
                    return CodexTurnResult(
                        text="".join(texts),
                        status="failed",
                        error=message_text or "codex 本轮失败",
                        usage=usage,
                        context_tokens=context_tokens,
                    )
            elif method == "warning":
                logger.warning("codex app-server warning: %s", params.get("message") or params)
            elif method.startswith("turn/"):
                logger.debug("codex app-server 通知 %s", method)

    async def _emit_tool(self, item: dict[str, Any], state: str) -> None:
        fields = codex_item_fields(item)
        if not fields or self._on_tool is None:
            return
        await self._on_tool(fields, state)


class _TextPump:
    """text/thinking 增量攒批；同一段连续输出不拆成多条时间线。"""

    def __init__(self, on_text: TextHandler, on_thinking: ThinkingHandler) -> None:
        self._on_text = on_text
        self._on_thinking = on_thinking
        self._pending: list[tuple[str, str]] = []
        self._chars = 0
        self._opened = 0.0
        self.has_thinking = False

    def add(self, kind: str, text: str) -> None:
        if not text:
            return
        if kind == "thinking":
            self.has_thinking = True
        if self._pending and self._pending[-1][0] == kind:
            self._pending[-1] = (kind, self._pending[-1][1] + text)
        else:
            self._pending.append((kind, text))
        self._chars += len(text)
        if not self._opened:
            self._opened = time.monotonic()

    async def maybe_flush(self) -> None:
        if not self._pending:
            return
        if self._chars >= FLUSH_CHARS or (
            time.monotonic() - self._opened >= FLUSH_INTERVAL_SECONDS
        ):
            await self.flush()

    async def flush(self) -> None:
        pending, self._pending = self._pending, []
        self._chars = 0
        self._opened = 0.0
        for kind, text in pending:
            if not text:
                continue
            if kind == "thinking":
                await self._on_thinking(text)
            else:
                await self._on_text(text)


def _context_tokens(params: Any) -> int:
    """从 `thread/tokenUsage/updated` 取「本次请求的上下文规模」。

    用 `last.totalTokens`（= 输入 + 本轮输出）而不是 `inputTokens`：压缩轮的
    `last.inputTokens` 是 0，但 `last.totalTokens` 反映压缩后的真实上下文，两者只有它
    在压缩前后都可比（实测 2026-11-01：压缩前 13237 → 压缩后 5285）。
    """
    if not isinstance(params, dict):
        return 0
    usage = params.get("tokenUsage")
    if not isinstance(usage, dict):
        return 0
    last = usage.get("last")
    if not isinstance(last, dict):
        return 0
    for key in ("totalTokens", "inputTokens"):
        value = last.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return 0


def _model_overrides(model: str, model_provider: str) -> dict[str, str]:
    """`thread/start` / `thread/resume` 的模型覆盖参数（空值不传，沿用配置默认）。"""
    overrides: dict[str, str] = {}
    if model.strip():
        overrides["model"] = model.strip()
    if model_provider.strip():
        overrides["modelProvider"] = model_provider.strip()
    return overrides


def _thread_id(response: Any) -> str:
    if not isinstance(response, dict):
        return ""
    thread = response.get("thread")
    if not isinstance(thread, dict):
        return ""
    return str(thread.get("id") or "")


def _turn_error(turn: dict[str, Any]) -> str | None:
    error = turn.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        return str(message) if message else "codex 本轮失败"
    if isinstance(error, str) and error.strip():
        return error
    return None


def _reasoning_text(item: dict[str, Any]) -> str:
    text = item.get("text")
    if isinstance(text, str) and text.strip():
        return text
    summary = item.get("summary")
    if isinstance(summary, list):
        return "".join(
            str(block.get("text") or "")
            for block in summary
            if isinstance(block, dict) and str(block.get("text") or "").strip()
        )
    return ""


#: 老 v1 审批方法的决策词是 approved/denied
_LEGACY_APPROVALS = frozenset({"execCommandApproval", "applyPatchApproval"})


def _command_text(params: Any) -> str:
    """从审批请求参数里取命令行文本：新版是字符串，旧版 execCommandApproval 是数组。"""
    if not isinstance(params, dict):
        return ""
    command = params.get("command")
    if isinstance(command, str):
        return command.strip()
    if isinstance(command, list):
        return " ".join(str(part) for part in command).strip()
    return ""


def codex_item_fields(item: dict[str, Any]) -> dict[str, Any]:
    """app-server 的 camelCase item → 时间线字段（与 exec 模式字段同名同义）。"""
    item_type = str(item.get("type") or "")
    fields: dict[str, Any] = {}
    if item_type == "commandExecution":
        fields = {
            "name": "shell",
            "detail": item.get("command"),
            "output": item.get("aggregatedOutput") or item.get("output"),
            "status": str(item.get("status") or ""),
        }
    elif item_type == "mcpToolCall":
        fields = {
            "name": str(item.get("tool") or "mcp"),
            "detail": item.get("arguments"),
            "output": item.get("result") or item.get("error"),
            "status": str(item.get("status") or ""),
        }
    elif item_type == "fileChange":
        paths = item.get("paths")
        fields = {
            "name": "file_change",
            "detail": paths if isinstance(paths, list) else item.get("changes"),
            "output": item.get("status"),
            "status": str(item.get("status") or ""),
        }
    else:
        return {}
    if isinstance(item.get("id"), str):
        fields["call_id"] = item["id"]
    return fields


__all__ = [
    "CodexAppServerSession",
    "CodexTurnResult",
    "app_server_argv",
    "codex_item_fields",
]
