"""行内补全的流式生成（AI_COMPLETION_SPEC §4.2 / §5.4）。

对外只暴露一个异步事件流：`delta`（增量文本）与 `done`（模型 / 用量 / 是否截断），
异常统一变成 `error` 事件——HTTP 头一旦发出就不能再改成 4xx/5xx，错误只能走流内事件。

生成路径：

1. **DeepSeek 原生 FIM**：`POST {base}/beta/completions`，把 `<CURSOR>` 前后的内容分别
   作为 `prompt` / `suffix`，首字延迟最低。端点不支持（400/404/405/422/501）时自动降级。
2. **chat 补全**：把三层上下文拼成结构化提示词，要求模型"只输出插入点处的代码"。
   其他供应商（OpenAI / Anthropic / OpenAI 兼容）都走这条路。

两条路径共用同一个流式清洗器：去掉模型偶尔吐出的 Markdown 围栏、按 `max_lines`
截断、保留行首缩进（缩进是代码语义的一部分，不能 trim）。
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from flux.config import Settings
from flux.core.ide_completion.context import CompletionContextBuilder
from flux.core.ide_completion.prefs import CompletionPrefsStore
from flux.core.ide_completion.resolver import CompletionModelResolver, CompletionTarget
from flux.core.model_gateway.base import ChatMessage
from flux.core.model_gateway.http import map_status_to_error
from flux.core.model_gateway.providers.anthropic import AnthropicProvider
from flux.core.model_gateway.providers.deepseek import DeepSeekProvider
from flux.core.model_gateway.providers.openai import OpenAIProvider
from flux.core.model_gateway.providers.openai_compatible import OpenAICompatibleProvider
from flux.errors import AIOSError, ProviderTimeoutError
from flux.logging import get_logger
from flux.schemas.api import CompletionContextRequest, FimRequest

logger = get_logger(__name__)

#: 系统提示词与 web-dashboard 的补全口径保持一致，保证两端体验一致
AI_SYSTEM_PROMPT = (
    "你是代码补全引擎。只输出应当插入到 <CURSOR> 处的代码本身，"
    "不要解释、不要 Markdown 代码围栏、不要重复光标前后已有的内容、不要输出多余缩进。"
    "补全要短而完整（通常 1-5 行），语法上能独立成立；不确定时宁可不输出。"
)

#: 原生 FIM 的上下文预算（超出会截断；靠近光标的一侧优先）
FIM_PREFIX_MAX_CHARS = 24_000
FIM_SUFFIX_MAX_CHARS = 8_000
#: chat 补全的上下文预算
CHAT_PREFIX_MAX_CHARS = 12_000
CHAT_SUFFIX_MAX_CHARS = 4_000
#: 拼进原生 FIM prompt 的 L1/L2 摘要上限（以注释形式放在文件开头）
FIM_CONTEXT_HEADER_MAX_CHARS = 1_600

#: 这些状态码表示"该端点不支持 FIM"，可以安全降级为 chat
FIM_FALLBACK_STATUS = frozenset({400, 404, 405, 406, 415, 422, 501})

#: 补全输出上限：原生 FIM 默认 512（模型不思考，够用且首字快）
DEFAULT_MAX_TOKENS = 512
MIN_MAX_TOKENS = 64
#: chat 路径下限。reasoning 类模型会先花 token 思考，预算太小会把额度吃光导致 content 为空
CHAT_MIN_MAX_TOKENS = 1024


class _FimUnsupported(Exception):
    """内部信号：原生 FIM 不可用，改用 chat 补全。"""


class _OpenAICompatibleCompletion(OpenAICompatibleProvider):
    """`openai_compatible` 在 `ModelProvider` 枚举里没有对应值，补全路径用字符串占位。"""

    provider = "openai_compatible"  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class CompletionEvent:
    event: str
    data: dict[str, Any]


class _StreamFormatter:
    """把模型输出整理成"可直接插入编辑器"的增量文本。

    - 去掉开头 / 结尾的 Markdown 代码围栏；
    - 超过 `max_lines` 行就截断并停止（不会出现半行）；
    - 保留行首缩进；仅裁掉结尾的空白。
    """

    def __init__(self, max_lines: int) -> None:
        self._max_lines = max(1, max_lines)
        self._head_buffer = ""
        self._decided = False
        self._pending = ""
        self._newlines = 0
        self._closed = False
        self.truncated = False

    def feed(self, text: str) -> str:
        if self._closed or not text:
            return ""
        if not self._decided:
            self._head_buffer += text
            decided = self._decide_head(final=False)
            if decided is None:
                return ""
            self._decided = True
            text = decided
        return self._process(text)

    def finish(self) -> str:
        if self._closed:
            return ""
        self._closed = True
        if not self._decided:
            self._decided = True
            head = self._decide_head(final=True) or ""
            return self._process(head, final=True)
        return self._process("", final=True)

    # --- 内部 ---

    def _decide_head(self, *, final: bool) -> str | None:
        raw = self._head_buffer
        if not raw:
            return "" if final else None
        # 可能正在形成 ``` 围栏：先等够 3 个字符
        if not raw.startswith("```"):
            if raw.strip() == "" and not final:
                return None
            if raw == "`" or raw == "``":
                return None if not final else raw
            return raw
        newline = raw.find("\n")
        if newline == -1:
            # 只有围栏开头（可能还带语言标记），等换行或结束
            return "" if final else None
        # 丢掉 ` ```lang ` 这一行，保留正文
        return raw[newline + 1 :]

    def _process(self, text: str, *, final: bool = False) -> str:
        buffer = self._pending + text
        if final:
            self._pending = ""
            body = self._strip_trailing_fence(buffer)
        else:
            if len(buffer) <= 4:
                self._pending = buffer
                return ""
            body = buffer[:-4]
            self._pending = buffer[-4:]
        return self._emit(body)

    @staticmethod
    def _strip_trailing_fence(buffer: str) -> str:
        lines = buffer.splitlines(keepends=True)
        while lines and not lines[-1].strip():
            lines.pop()
        if lines and lines[-1].strip().startswith("```"):
            lines.pop()
        return "".join(lines).rstrip()

    def _emit(self, body: str) -> str:
        if not body:
            return ""
        allowed_newlines = self._max_lines - 1
        if allowed_newlines <= 0:
            newline = body.find("\n")
            if newline != -1:
                self._closed = True
                self.truncated = True
                return body[:newline]
            return body
        remaining = allowed_newlines - self._newlines
        if remaining <= 0:
            newline = body.find("\n")
            if newline != -1:
                self._closed = True
                self.truncated = True
                return body[:newline]
            return body
        count = body.count("\n")
        if count > remaining:
            position = -1
            for _ in range(remaining + 1):
                position = body.find("\n", position + 1)
                if position == -1:
                    break
            self._closed = True
            self.truncated = True
            return body[:position] if position != -1 else body
        self._newlines += count
        return body


class _LeadingDeduper:
    """剥掉模型在开头原样复读的"光标前内容"。

    chat 降级路径（偶尔原生 FIM 也会）倾向于把 `prefix` 的最后一小段再吐一遍，
    直接插入编辑器就会重复。这里做**保守**判定，宁可不剥也不误伤：

    - 开头与 `prefix` 尾部重叠 ≥ `STRONG_OVERLAP` 个字符 → 直接剥；
    - 重叠 4~7 个字符时，只有重叠末尾不是标识符字符（例如 `(`、`=`、换行）才剥，
      避免把 `foo` 这类正常的续写误判成回显；
    - 纯空白 / 重叠不足 → 原样保留。

    判定前会先缓冲一小段（重叠可能还在增长），所以首个 `delta` 可能晚几毫秒到达。
    """

    MIN_OVERLAP = 4
    STRONG_OVERLAP = 8
    TAIL_WINDOW = 512

    def __init__(self, prefix: str) -> None:
        self._tail = prefix[-self.TAIL_WINDOW :] if prefix else ""
        self._buffer = ""
        self._decided = False

    def feed(self, text: str) -> str:
        if self._decided:
            return text
        if not text:
            return ""
        self._buffer += text
        buffer = self._buffer
        overlap = _longest_overlap(buffer, self._tail)
        if overlap >= self.STRONG_OVERLAP:
            return self._flush(overlap)
        if overlap == len(buffer) and len(buffer) < self.STRONG_OVERLAP:
            # 目前整段都像回显，可能还会继续匹配：等下一块再判
            return ""
        if overlap >= self.MIN_OVERLAP and not _is_identifier_char(buffer[overlap - 1]):
            return self._flush(overlap)
        return self._flush(None)

    def finish(self) -> str:
        if self._decided:
            return ""
        buffer = self._buffer
        overlap = _longest_overlap(buffer, self._tail)
        if overlap >= self.STRONG_OVERLAP or (
            overlap >= self.MIN_OVERLAP
            and overlap <= len(buffer)
            and not _is_identifier_char(buffer[overlap - 1])
        ):
            return self._flush(overlap)
        return self._flush(None)

    def _flush(self, strip: int | None) -> str:
        buffer = self._buffer
        self._buffer = ""
        self._decided = True
        return buffer if strip is None else buffer[strip:]


def _longest_overlap(buffer: str, tail: str) -> int:
    """`buffer` 的开头与 `tail` 结尾的最长重合长度。"""
    if not buffer or not tail:
        return 0
    limit = min(len(buffer), len(tail))
    for size in range(limit, 0, -1):
        if buffer[:size] == tail[-size:]:
            return size
    return 0


def _is_identifier_char(char: str) -> bool:
    return char.isalnum() or char == "_"


def _tail(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[-limit:]


def _head(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit]


def _language_of(request: FimRequest) -> str:
    if request.language_id:
        return request.language_id
    if request.path:
        return request.path.rsplit(".", 1)[-1] if "." in request.path else ""
    return ""


def _comment_prefix(request: FimRequest) -> tuple[str, str]:
    """返回目标语言的块注释起止符（用于把 L1/L2 摘要注入原生 FIM 的 prompt）。"""
    language = _language_of(request).lower()
    path = (request.path or "").lower()
    if language in {"html", "xml", "vue", "svelte", "markdown", "md"} or path.endswith(
        (".html", ".htm", ".xml", ".vue", ".svelte", ".md")
    ):
        return "<!-- ", " -->"
    if language in {"css", "scss", "less"} or path.endswith((".css", ".scss", ".less")):
        return "/* ", " */"
    if language in {"sql"} or path.endswith(".sql"):
        return "-- ", ""
    if language in {"python", "py", "ruby", "rb", "shellscript", "sh", "bash", "yaml", "toml"}:
        return "# ", ""
    return "// ", ""


def _context_text(context: dict[str, Any]) -> str:
    parts: list[str] = []
    l2 = context.get("l2") or {}
    spec = str(l2.get("spec") or "").strip()
    stack = str(l2.get("stack") or "").strip()
    recent = str(l2.get("recent") or "").strip()
    if spec:
        parts.append(f"项目规范：\n{spec}")
    if stack:
        parts.append(f"技术栈：\n{stack}")
    if recent:
        parts.append(f"近期改动：\n{recent}")
    for item in context.get("l1") or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        excerpt = str(item.get("excerpt") or "")
        if path and excerpt:
            parts.append(f"邻接文件 {path}：\n{excerpt}")
    return "\n\n".join(parts)


def _fim_context_header(request: FimRequest, context: dict[str, Any]) -> str:
    body = _context_text(context)
    if not body:
        return ""
    start, end = _comment_prefix(request)
    body = _head(body, FIM_CONTEXT_HEADER_MAX_CHARS)
    lines = [
        f"{start}{line}{end}".rstrip() if line else start.rstrip() for line in body.splitlines()
    ]
    return "\n".join(lines) + "\n\n"


def _fence_for(text: str) -> str:
    fence = "```"
    while fence in text:
        fence += "`"
    return fence


def _chat_messages(
    request: FimRequest,
    *,
    prefix: str,
    suffix: str,
    context: dict[str, Any],
) -> list[ChatMessage]:
    language = _language_of(request) or "text"
    fence = _fence_for(prefix + suffix)
    body = (
        f"{fence}{language}\n{_tail(prefix, CHAT_PREFIX_MAX_CHARS)}"
        f"<CURSOR>{_head(suffix, CHAT_SUFFIX_MAX_CHARS)}\n{fence}"
    )
    context_text = _context_text(context)
    user = ""
    if context_text:
        user += f"# 项目上下文\n{context_text}\n\n"
    user += f"# 当前文件（<CURSOR> 是插入点）\n{body}"
    return [
        ChatMessage(role="system", content=AI_SYSTEM_PROMPT),
        ChatMessage(role="user", content=user),
    ]


class CompletionService:
    """补全上下文 / 偏好 / 流式生成的门面。"""

    def __init__(
        self,
        *,
        settings: Settings,
        builder: CompletionContextBuilder,
        resolver: CompletionModelResolver,
        prefs: CompletionPrefsStore,
    ) -> None:
        self._settings = settings
        self._builder = builder
        self._resolver = resolver
        self._prefs = prefs
        self._http: httpx.AsyncClient | None = None

    # --- 对外 ---

    async def resolve(
        self, *, model: str | None = None, key_id: str | None = None, agent_id: str | None = None
    ) -> CompletionTarget:
        return await self._resolver.resolve(model=model, key_id=key_id, agent_id=agent_id)

    async def build_context(self, payload: CompletionContextRequest) -> dict[str, Any]:
        return await self._builder.build(
            project_id=payload.project_id,
            path=payload.path,
            caret=payload.caret,
            text=payload.text,
            language_id=payload.language_id,
        )

    async def stream(
        self, request: FimRequest, target: CompletionTarget
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """对外事件流：在 `done` 里补上首字延迟 / 总耗时 / 字符数（埋点用）。"""
        started = time.perf_counter()
        first_token_at: float | None = None
        chars = 0

        async for event, data in self._stream_impl(request, target):
            if event == "delta":
                text = str(data.get("text") or "")
                if text:
                    if first_token_at is None:
                        first_token_at = time.perf_counter()
                    chars += len(text)
                yield event, data
                continue
            if event == "done":
                enriched = dict(data)
                enriched["first_token_ms"] = (
                    None if first_token_at is None else round((first_token_at - started) * 1000)
                )
                enriched["total_ms"] = round((time.perf_counter() - started) * 1000)
                enriched["chars"] = chars
                enriched.setdefault("empty", chars == 0)
                yield event, enriched
                continue
            yield event, data

    async def _stream_impl(
        self, request: FimRequest, target: CompletionTarget
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        context = request.context or await self._builder.build(
            project_id=request.project_id,
            path=request.path,
            caret=request.caret,
            text=request.text,
            language_id=request.language_id,
        )
        prefix, suffix = self._split(request)

        # 原生 FIM 只在 DeepSeek 通道尝试；其余供应商直接 chat
        if target.provider == "deepseek":
            produced = False
            try:
                async for event in self._stream_native_fim(
                    request, target, prefix=prefix, suffix=suffix, context=context
                ):
                    if event[0] == "delta":
                        produced = True
                    yield event
                return
            except _FimUnsupported:
                logger.debug(
                    "ide_completion.fim_unsupported provider=%s model=%s",
                    target.provider,
                    target.model,
                )
            except AIOSError as exc:
                yield ("error", self._error_payload(exc))
                return
            except Exception as exc:  # noqa: BLE001 - 连接级故障也要能兜底
                if produced:
                    # 已经吐过增量：不能再从头跑 chat，否则前端会看到重复文本
                    logger.exception("ide_completion.fim_stream_failed model=%s", target.model)
                    yield (
                        "error",
                        {"code": "completion_failed", "message": f"补全失败：{exc}"},
                    )
                    return
                # 一个字都没产出：按端点不可用处理，走 chat 兜底
                logger.warning(
                    "ide_completion.fim_failed_fallback model=%s error=%s",
                    target.model,
                    exc,
                )

        try:
            async for event in self._stream_chat(
                request, target, prefix=prefix, suffix=suffix, context=context
            ):
                yield event
        except AIOSError as exc:
            yield ("error", self._error_payload(exc))
        except Exception as exc:  # noqa: BLE001 - 流内异常必须变成事件，不能冒泡
            logger.exception("ide_completion.stream_failed model=%s", target.model)
            yield (
                "error",
                {
                    "code": "completion_failed",
                    "message": f"补全失败：{exc}",
                },
            )

    async def shutdown(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # --- 原生 FIM ---

    async def _stream_native_fim(
        self,
        request: FimRequest,
        target: CompletionTarget,
        *,
        prefix: str,
        suffix: str,
        context: dict[str, Any],
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        payload: dict[str, Any] = {
            "model": target.model,
            "prompt": _fim_context_header(request, context) + _tail(prefix, FIM_PREFIX_MAX_CHARS),
            "suffix": _head(suffix, FIM_SUFFIX_MAX_CHARS),
            "max_tokens": self._max_tokens(request),
            "stream": True,
        }
        if request.stop:
            payload["stop"] = request.stop

        headers = {
            "Authorization": f"Bearer {target.channel.api_key}",
            "Content-Type": "application/json",
        }
        url = self._fim_url(target.channel.base_url)
        formatter = _StreamFormatter(request.max_lines)
        deduper = _LeadingDeduper(prefix)
        usage: dict[str, int] | None = None
        emitted = False

        client = self._client()
        try:
            async with client.stream(
                "POST",
                url,
                json=payload,
                headers=headers,
                timeout=self._settings.model_timeout_seconds,
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    if response.status_code in FIM_FALLBACK_STATUS:
                        raise _FimUnsupported()
                    raise map_status_to_error(response.status_code, body, provider="deepseek")
                async for line in response.aiter_lines():
                    parsed = _parse_fim_line(line)
                    if parsed is None:
                        continue
                    text, raw_usage = parsed
                    if raw_usage is not None:
                        usage = raw_usage
                    if not text:
                        continue
                    deduped = deduper.feed(text)
                    if not deduped:
                        continue
                    cleaned = formatter.feed(deduped)
                    if cleaned:
                        emitted = True
                        yield ("delta", {"text": cleaned})
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                "补全请求超时", details={"model": target.model, "url": url}
            ) from exc

        leftover = deduper.finish()
        if leftover:
            cleaned = formatter.feed(leftover)
            if cleaned:
                emitted = True
                yield ("delta", {"text": cleaned})

        if not emitted:
            # 200 但一个字都没有：按"端点不可用"处理，走 chat 兜底
            raise _FimUnsupported()

        tail = formatter.finish()
        if tail:
            emitted = True
            yield ("delta", {"text": tail})
        yield (
            "done",
            {
                "mode": "fim",
                "model": target.model,
                "provider": target.provider,
                "key_id": target.key_id,
                "usage": usage,
                "truncated": formatter.truncated,
            },
        )

    @staticmethod
    def _fim_url(base_url: str) -> str:
        base = (base_url or "").rstrip("/")
        for suffix in ("/v1", "/beta"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        return f"{base}/beta/completions"

    # --- chat 补全 ---

    async def _stream_chat(
        self,
        request: FimRequest,
        target: CompletionTarget,
        *,
        prefix: str,
        suffix: str,
        context: dict[str, Any],
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        provider = self._build_provider(target)
        messages = _chat_messages(request, prefix=prefix, suffix=suffix, context=context)
        formatter = _StreamFormatter(request.max_lines)
        deduper = _LeadingDeduper(prefix)
        emitted = False

        async for text in provider.stream(
            messages,
            max_tokens=self._max_tokens(request, floor=CHAT_MIN_MAX_TOKENS),
            temperature=request.temperature if request.temperature is not None else 0.2,
        ):
            deduped = deduper.feed(text)
            if not deduped:
                continue
            cleaned = formatter.feed(deduped)
            if cleaned:
                emitted = True
                yield ("delta", {"text": cleaned})

        leftover = deduper.finish()
        if leftover:
            cleaned = formatter.feed(leftover)
            if cleaned:
                emitted = True
                yield ("delta", {"text": cleaned})

        tail = formatter.finish()
        if tail:
            yield ("delta", {"text": tail})
        yield (
            "done",
            {
                "mode": "chat",
                "model": target.model,
                "provider": target.provider,
                "key_id": target.key_id,
                "usage": None,
                "truncated": formatter.truncated,
                "empty": not emitted and not tail,
            },
        )

    def _build_provider(
        self, target: CompletionTarget
    ) -> OpenAICompatibleProvider | AnthropicProvider:
        channel = target.channel
        common = {
            "api_key": channel.api_key,
            "base_url": channel.base_url,
            "timeout": self._settings.model_timeout_seconds,
            "max_retries": 1,
            "client": self._client(),
        }
        if channel.provider == "anthropic":
            return AnthropicProvider(
                target.model,
                default_max_tokens=self._settings.anthropic_max_tokens,
                **common,
            )
        if channel.provider == "openai":
            return OpenAIProvider(target.model, **common)
        if channel.provider == "deepseek":
            return DeepSeekProvider(target.model, **common)
        return _OpenAICompatibleCompletion(target.model, **common)

    # --- 工具 ---

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(self._settings.model_timeout_seconds, connect=15.0)
            )
        return self._http

    @staticmethod
    def _max_tokens(request: FimRequest, *, floor: int = DEFAULT_MAX_TOKENS) -> int:
        if request.max_tokens:
            return max(MIN_MAX_TOKENS, int(request.max_tokens))
        # 按行数给预算：每行约 48 token，封顶 1024；floor 由调用方按通道给
        return max(floor, min(1024, max(DEFAULT_MAX_TOKENS, request.max_lines * 48)))

    @staticmethod
    def _split(request: FimRequest) -> tuple[str, str]:
        prefix = request.prefix or ""
        suffix = request.suffix or ""
        if not prefix and request.text is not None:
            offset = max(0, min(len(request.text), int(request.caret or 0)))
            prefix = request.text[:offset]
            suffix = suffix or request.text[offset:]
        return prefix, suffix

    @staticmethod
    def _error_payload(exc: AIOSError) -> dict[str, Any]:
        return {
            "code": getattr(exc, "code", "completion_failed"),
            "message": getattr(exc, "message", str(exc)),
            "details": getattr(exc, "details", None),
        }


def _parse_fim_line(line: str) -> tuple[str, dict[str, int] | None] | None:
    """解析 DeepSeek `/beta/completions` 的 SSE 行。

    返回 `(增量文本, 用量)`；`None` 表示这一行可以忽略。兼容两种响应结构：
    FIM 的 `choices[0].text` 与部分网关改写的 `choices[0].delta.content`。
    """
    if not line.startswith("data:"):
        return None
    data = line[5:].strip()
    if not data or data == "[DONE]":
        return None
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    usage = parsed.get("usage")
    parsed_usage: dict[str, int] | None = None
    if isinstance(usage, dict):
        parsed_usage = {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
        }
    choices = parsed.get("choices")
    if not isinstance(choices, list) or not choices:
        return "", parsed_usage
    choice = choices[0]
    if not isinstance(choice, dict):
        return "", parsed_usage
    text = choice.get("text")
    if not isinstance(text, str):
        delta = choice.get("delta")
        text = delta.get("content") if isinstance(delta, dict) else ""
    return (text if isinstance(text, str) else ""), parsed_usage


__all__ = [
    "AI_SYSTEM_PROMPT",
    "CompletionEvent",
    "CompletionService",
    "DEFAULT_MAX_TOKENS",
    "FIM_FALLBACK_STATUS",
]
