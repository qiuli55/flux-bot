"""Flux MCP Server（目标架构 §3.1）：Streamable HTTP + Bearer，单端点 `POST /mcp`。

挂载在现有 FastAPI 应用内（与 `/api/v1/*` 同进程、同生命周期），只做协议适配：
JSON-RPC 层 → 工具层 → Flux 既有服务。**无状态**——不返回 `Mcp-Session-Id`，
每次请求自带 `Authorization`，因此后端重启不会让已接入的 agent 掉线。

鉴权在解析请求体之前完成（fail-closed）：没有合法令牌就没有任何可调用的方法，
连 `initialize` 也不行。能力不足则是工具级错误（`isError=true`），
不用 JSON-RPC 协议错误——按 MCP 规范，工具执行失败属于"结果"，不是"协议错"。
"""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from flux.api.deps import get_container
from flux.api.response import fail
from flux.container import Container
from flux.core.context.redact import redact
from flux.core.event.bus import Events
from flux.core.mcp.auth import AgentIdentity
from flux.core.mcp.tools import ALL_TOOLS, TOOLS, ToolContext, ToolSpec
from flux.core.mcp.tools.permission_tools import PERMISSION_PROMPT_TOOL_NAME
from flux.core.permissions.modes import (
    allowed_tools,
    denied_message,
    normalize_mode,
    tool_allowed,
)
from flux.enums import StepKind, StepStatus
from flux.errors import AIOSError, AuthenticationError
from flux.logging import get_logger
from flux.version import VERSION

logger = get_logger(__name__)

JSONRPC_VERSION = "2.0"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602

#: 支持的 MCP 协议版本；客户端报的版本在表内就原样回，不在表内回我们自己最新的。
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

SERVER_NAME = "flux"

#: initialize 时下发的使用说明（不占工具的 description，讲的是"先做什么"）
SERVER_INSTRUCTIONS = (
    "Flux 是工程平台，不替你执行也不替你落盘。开工先调 context.boot（带上 task_id）："
    "一次拿齐运行时身份与能力、项目事实与约束、跨会话长期记忆（Environment 平台规则 / "
    "User 偏好 / Project 沉淀），省掉三次往返；需要刷新某一份时再单独调 flux_context / "
    "context.get / memory.recall（均只读），并用 workspace.read 核对文件当前内容；"
    "用户在本轮表达了稳定的偏好、习惯或禁忌时，用 memory.remember 记一条"
    "（每轮最多 3 条，不记项目细节、路径与密钥，已记过的不要重复写）；"
    "改动一律经 proposal.create 提交进人审队列，真实落盘由人审通过后经 "
    "Apply Engine 执行。页面 / UI / 视觉设计任务必须用 design.* 工具在设计工作室产出"
    "（禁止用手写 HTML、截图或静态图替代），完成后调 design.share 把卡片发进对话；"
    "改已有设计优先用 design.ai 只改局部（必要时带 selected_ids），别整份 design.update "
    "覆盖；改坏了用 design.revisions 查历史、design.revert 回滚。任务产出了用户要带走的成品文件"
    "（报告 / 图片 / PDF / 导出数据 / 压缩包等）时，用 file.deliver 发进当前对话："
    "路径相对工作区根，一次最多 9 个，只发成品不发中间产物。历史条目是快照：断言不等于事实，"
    "采信前自行验证。MCP 不可用时，"
    "可用 Flux Server CLI 兜底继续工作（例如 `flux tools call <tool>`）。"
    "【交卷前必须自检】proposal.create 之前，先用 proposal.preview 把同一份 payload 在"
    "临时副本里跑一遍测试命令（它会把改动落进副本再跑；直接 terminal.execute 跑的是磁盘上的"
    "旧代码，绿了也不算数）。测试没过不许提交提案：读失败输出、改自己的改动、再 preview，"
    "直到全绿；单轮最多重试 3 次，仍不绿就把失败输出写进 proposal 的 summary 里交人工判断，"
    "不要假装通过。预演本身不产生提案：只有 proposal.create 才会让改动进入人审队列，"
    "预演通过后必须再提交一次同一份 payload。"
)


class McpServer:
    """JSON-RPC 消息处理器。一个实例可以服务任意多次调用（无会话状态）。"""

    def __init__(self, container: Container) -> None:
        self._container = container

    async def handle(self, message: Any, identity: AgentIdentity) -> dict[str, Any] | None:
        """返回 JSON-RPC 响应；返回 None 表示这是通知（HTTP 层回 202 无正文）。"""
        if not isinstance(message, dict):
            return _error(None, INVALID_REQUEST, "请求必须是 JSON-RPC 对象")
        if message.get("jsonrpc") != JSONRPC_VERSION:
            return _error(
                _safe_id(message), INVALID_REQUEST, f"jsonrpc 字段必须是 {JSONRPC_VERSION}"
            )

        method = message.get("method")
        request_id = _safe_id(message)
        params = message.get("params") or {}
        if not isinstance(method, str):
            return _error(request_id, INVALID_REQUEST, "缺少 method 字段")

        if request_id is None:
            # 通知：不产生响应体（initialized / cancelled 都是这一类）
            logger.debug("MCP 通知 method=%s agent=%s", method, identity.agent_id)
            return None
        if method == "initialize":
            return _result(request_id, self._initialize(params))
        if method == "ping":
            return _result(request_id, {})
        if method == "tools/list":
            whitelist = allowed_tools(identity.permission_mode)
            bot_whitelist = (
                set(identity.allowed_tools) if identity.allowed_tools is not None else None
            )
            tools = [
                spec.to_mcp()
                for spec in ALL_TOOLS
                if (whitelist is None or spec.name in whitelist)
                and (bot_whitelist is None or spec.name in bot_whitelist)
            ]
            return _result(request_id, {"tools": tools})
        if method == "tools/call":
            return await self._call_tool(request_id, params, identity)
        return _error(request_id, METHOD_NOT_FOUND, f"不支持的方法：{method}")

    # --- 内部 ---

    @staticmethod
    def _initialize(params: Any) -> dict[str, Any]:
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        version = (
            requested
            if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS
            else DEFAULT_PROTOCOL_VERSION
        )
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": VERSION},
            "instructions": SERVER_INSTRUCTIONS,
        }

    async def _call_tool(
        self, request_id: Any, params: Any, identity: AgentIdentity
    ) -> dict[str, Any]:
        if not isinstance(params, dict):
            return _error(request_id, INVALID_PARAMS, "tools/call 需要 params 对象")
        name = params.get("name")
        if not isinstance(name, str) or not name.strip():
            return _error(request_id, INVALID_PARAMS, "tools/call 缺少工具名 name")
        name = name.strip()
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _error(request_id, INVALID_PARAMS, "tools/call 的 arguments 必须是对象")

        spec = TOOLS.get(name)
        if spec is None:
            return _error(request_id, INVALID_PARAMS, f"未知工具：{name}")

        # 第一道：模式白名单（Plan 只读）。长期令牌是能力位超集，
        # 只靠 scopes 区分不了模式，所以白名单必须排在能力位检查之前。
        if not tool_allowed(identity.permission_mode, name):
            message = denied_message(identity.permission_mode, name)
            await self._publish(Events.MCP_TOOL_DENIED, identity, spec, error="permission_mode")
            logger.warning(
                "MCP 模式越权调用被拒 agent=%s mode=%s tool=%s",
                identity.agent_id,
                normalize_mode(identity.permission_mode),
                name,
            )
            return _result(request_id, _tool_error(message))

        # 审批桥不属于 Agent 能力：它只把 Claude Code 的权限询问转成人工审批卡，
        # 不执行任何操作。Bot 工具白名单若把它挡住，正常模式下所有需要审批的调用
        # 都会变成"桥不可用"，因此这里豁免（能力位检查仍然生效）。
        if (
            name != PERMISSION_PROMPT_TOOL_NAME
            and identity.allowed_tools is not None
            and name not in identity.allowed_tools
        ):
            message = f"Bot 执行白名单未包含工具 {name}，已拒绝执行"
            await self._publish(Events.MCP_TOOL_DENIED, identity, spec, error="bot_allowed_tools")
            logger.warning("MCP Bot 工具白名单拒绝 agent=%s tool=%s", identity.agent_id, name)
            return _result(request_id, _tool_error(message))

        violation = _policy_violation(identity, name, arguments)
        if violation is not None:
            await self._publish(Events.MCP_TOOL_DENIED, identity, spec, error="bot_policy")
            logger.warning("MCP Bot 执行策略拒绝 agent=%s tool=%s", identity.agent_id, name)
            return _result(request_id, _tool_error(violation))

        if not identity.has(spec.capability):
            message = (
                f"权限不足：令牌所属 agent「{identity.agent_id}」未获授权能力"
                f" {spec.capability}，已拒绝执行 {name}"
            )
            await self._publish(Events.MCP_TOOL_DENIED, identity, spec, error=str(spec.capability))
            logger.warning("MCP 越权调用被拒 agent=%s tool=%s", identity.agent_id, name)
            return _result(request_id, _tool_error(message))

        ctx = ToolContext(container=self._container, identity=identity, tool_name=name)
        try:
            data = await spec.handler(ctx, arguments)
        except AIOSError as exc:
            await self._publish(Events.MCP_TOOL_CALLED, identity, spec, error=exc.code)
            logger.info(
                "MCP 工具调用失败 agent=%s tool=%s error=%s", identity.agent_id, name, exc.code
            )
            # 失败也要摄入（§3.3）：错误是下一轮最需要的上下文，成功路径写、失败路径不写
            # 会让 Agent 在同一个坑里反复踩——它看不到自己刚才失败在哪。
            await self._ingest_context(
                identity,
                spec,
                {"error": {"code": exc.code, "message": exc.message}},
                status=StepStatus.FAIL,
            )
            return _result(request_id, _tool_error(f"{exc.code}: {exc.message}"))
        except Exception as exc:  # noqa: BLE001 - 工具边界：任何异常都要转成显式失败
            await self._publish(Events.MCP_TOOL_CALLED, identity, spec, error="internal_error")
            logger.exception("MCP 工具执行异常 tool=%s agent=%s", name, identity.agent_id)
            await self._ingest_context(
                identity,
                spec,
                {"error": {"code": "internal_error", "message": str(exc)}},
                status=StepStatus.FAIL,
            )
            return _result(request_id, _tool_error(f"工具执行失败：{exc}"))

        await self._publish(Events.MCP_TOOL_CALLED, identity, spec)
        # 审计轨迹：谁（agent/token）调了哪个工具、成没成，落一行日志（入参不落，可能含整份文件）
        logger.info("MCP 工具调用成功 agent=%s tool=%s", identity.agent_id, name)
        # 上下文摄入（机制 §3.3 / §10.1）：能归属到 Run 才写库，且 best-effort。
        await self._ingest_context(identity, spec, data)
        return _result(request_id, _tool_text(data))

    async def _ingest_context(
        self,
        identity: AgentIdentity,
        spec: ToolSpec,
        data: dict[str, Any],
        *,
        status: StepStatus = StepStatus.OK,
    ) -> None:
        """把一次工具结果摄入上下文：redact → CAS 原文 → step 落库 → 脱敏映射（§10.1 契约）。

        成功与失败共用同一条路径：失败只是 `status=FAIL`、正文换成错误对象——step 日志
        因此能回答"这轮做砸了什么"，而不是只留下成功的部分历史。

        归属键 `run_id` 来自 `X-Flux-Run-Id`（可为 None ⇒ 无法归属到 Run，整段跳过，不写库）。
        当前上下文机制没有 turn 粒度，`turn_id` 暂以 `run_id` 近似（占位符/删除走同一把钥匙）。
        摄入是 **best-effort**：任何一步失败只记 warning，绝不改变工具对外结果。
        """
        run_id = identity.run_id
        if run_id is None:
            return
        try:
            text = json.dumps(data, ensure_ascii=False, indent=2)
            result = redact(text)
            body = result.text.encode("utf-8")
            blob = await self._container.content_store.put(body)
            await self._container.context_steps.append(
                run_id=run_id,
                turn_id=run_id,
                kind=StepKind.TOOL_CALL,
                status=status,
                tool=spec.name,
                result_sha256=blob.sha256,
                result_bytes=len(body),
                result_head=result.text[:512],
            )
            await self._container.context_redactions.record(
                run_id=run_id, turn_id=run_id, matches=result.matches
            )
        except Exception:  # noqa: BLE001 - 工具边界不允许因摄入失败而失败
            logger.warning("MCP 上下文摄入失败 tool=%s run=%s", spec.name, run_id, exc_info=True)

    async def _publish(
        self,
        event: str,
        identity: AgentIdentity,
        spec: ToolSpec,
        *,
        error: str | None = None,
    ) -> None:
        """记一条工具调用事件。**不含入参**——提案入参里可能有整份文件内容。"""
        await self._container.bus.publish(
            event,
            {
                "agent_id": identity.agent_id,
                "token_id": identity.token_id,
                "tool": spec.name,
                "capability": str(spec.capability),
                "error": error,
            },
        )


router = APIRouter(tags=["mcp"])


@router.post(
    "/mcp",
    summary="MCP Streamable HTTP 端点（Agent 能力面）",
    response_model=None,
    responses={
        200: {"description": "JSON-RPC 响应（工具结果或协议错误）"},
        202: {"description": "通知类消息，无正文"},
        401: {"description": "缺少或非法/已撤销的 Bearer 令牌"},
    },
)
async def mcp_endpoint(request: Request, container: Container = Depends(get_container)) -> Response:
    """MCP 唯一入口。鉴权先于解析：没有合法令牌，任何方法都不可见。"""
    try:
        identity = await container.agent_tokens.authenticate(
            _bearer_token(request.headers.get("authorization"))
        )
    except AuthenticationError as exc:
        # MCP 规范要求 401 带 WWW-Authenticate；这里不让全局处理器代劳，因为那个不带该头
        return JSONResponse(
            status_code=401,
            content=fail(exc.code, exc.message),
            headers={"WWW-Authenticate": "Bearer"},
        )

    identity = await _attach_run_id(request, container, identity)

    raw = await request.body()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return _json(_error(None, PARSE_ERROR, "请求体不是合法 JSON"))

    if isinstance(payload, list):
        # MCP 2025-06-18 起不再支持批量请求
        return _json(_error(None, INVALID_REQUEST, "不支持 JSON-RPC 批量请求"))

    outcome = await McpServer(container).handle(payload, identity)
    if outcome is None:
        return Response(status_code=202)
    return _json(outcome)


def _bearer_token(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip() or None


async def _attach_run_id(
    request: Request, container: Container, identity: AgentIdentity
) -> AgentIdentity:
    """把 `X-Flux-Run-Id`（或 `X-Flux-Task-Id` 解析出的活动 Run）附加到身份上。

    该头由 Flux 起 Run 时注入到 Agent 的 MCP 配置；用于把 Agent 经 `terminal.execute`
    派生的终端会话绑到具体 Run，Run 终态时据此回收（避免命令进程泄漏）。它**不是权威身份**：
    查不到该 Run、或归属与令牌身份不匹配，一律静默忽略（不 401）——Run 维度的小错误不该
    升级成鉴权失败。校验依据是"该 Run 的 agent_id 与令牌 agent_id 相等"，杜绝跨 Agent 冒充。

    `X-Flux-Run-Id` 缺失时（runtime 声明了进程复用：常驻进程跨 Run 存活，配置里写死某一轮
    的 run_id 会在下一轮变成过期 id），退化为按 `X-Flux-Task-Id` 解析"该任务当前活动的 Run"。
    这条回退由通用层提供，任何 runtime 只要按这两条头之一注入即可。

    模式按 Run 快照判定：切换模式只影响下一轮，正在跑的这一轮始终用启动时的模式。
    """
    run_id = (request.headers.get("x-flux-run-id") or "").strip()
    if run_id:
        row = await container.run_repo.get(run_id)
        if row is None or not row.agent_id or row.agent_id != identity.agent_id:
            logger.warning("mcp 忽略非法 X-Flux-Run-Id run=%s agent=%s", run_id, identity.agent_id)
            return identity
        return await _with_run(container, identity, row, run_id=run_id)
    task_id = (request.headers.get("x-flux-task-id") or "").strip()
    if not task_id:
        return identity
    try:
        rows = await container.run_repo.list_non_terminal()
    except Exception:  # noqa: BLE001 - 解析失败只退回无 run 身份，不影响本轮调用
        logger.warning("mcp 解析 X-Flux-Task-Id 失败 task=%s", task_id, exc_info=True)
        return identity
    matched = [
        row
        for row in rows
        if str(row.task_id or "") == task_id and row.agent_id == identity.agent_id
    ]
    if not matched:
        logger.warning(
            "mcp 未找到活动 Run task=%s agent=%s（X-Flux-Task-Id 回退失效）",
            task_id,
            identity.agent_id,
        )
        return identity
    # 同任务并发两轮时取最新创建的那一轮（与「后来者独占进程」一致）
    row = max(
        matched,
        key=lambda item: (
            item.created_at is not None,
            item.created_at,
            str(item.id),
        ),
    )
    return replace(
        identity,
        run_id=str(row.id),
        permission_mode=normalize_mode(getattr(row, "permission_mode", None)),
    )


async def _with_run(
    container: Container, identity: AgentIdentity, row: Any, *, run_id: str
) -> AgentIdentity:
    """把 Run 维度的模式与 Bot 执行白名单附加到身份上（都来自服务端事实）。"""
    policy = await _bot_policy(container, getattr(row, "task_id", None))
    return replace(
        identity,
        run_id=run_id,
        permission_mode=normalize_mode(getattr(row, "permission_mode", None)),
        **policy,
    )


async def _bot_policy(container: Container, task_id: Any) -> dict[str, Any]:
    """run → task → bot：Bot 开工的 Run 才带执行白名单；普通对话不受影响。"""
    if not task_id:
        return {}
    try:
        task = await container.task_repo.get(task_id)
    except Exception:  # noqa: BLE001 - 查不到任务只是没有 Bot 策略，不影响调用
        return {}
    bot_id = getattr(task, "bot_id", None)
    if not bot_id:
        return {}
    try:
        bot = await container.bots.get(bot_id)
    except Exception:  # noqa: BLE001 - Bot 已删除：本次 Run 仍按模式执行
        return {}
    return {
        "allowed_tools": _as_tuple(getattr(bot, "allowed_tools", None)),
        "allowed_commands": _as_tuple(getattr(bot, "allowed_commands", None)),
        "allowed_paths": _as_tuple(getattr(bot, "allowed_paths", None)),
    }


def _as_tuple(value: Any) -> tuple[str, ...] | None:
    if not value:
        return None
    return tuple(str(item) for item in value if str(item).strip())


#: 复合命令按分隔符切开，每一段的首 token 都要过白名单
_COMMAND_SPLIT = re.compile(r"[;&|\n]+")


def _command_heads(command: str) -> list[str]:
    heads: list[str] = []
    for segment in _COMMAND_SPLIT.split(command):
        tokens = segment.strip().split()
        # 跳过前置的环境变量赋值（FOO=bar cmd）
        while len(tokens) > 1 and "=" in tokens[0] and not tokens[0].startswith("="):
            tokens = tokens[1:]
        if tokens:
            heads.append(tokens[0])
    return heads


def _normalize_rel(value: str) -> str | None:
    """白名单里的路径一律相对工作目录；绝对路径返回 None（fail-closed）。"""
    text = value.replace("\\", "/").strip()
    if not text:
        return None
    if text.startswith("/") or (len(text) > 1 and text[1] == ":"):
        return None
    normalized = posixpath.normpath(text)
    return "." if normalized in ("", ".") else normalized


def _path_allowed(rel: str | None, allowed: tuple[str, ...]) -> bool:
    if rel is None:
        return False
    for prefix in allowed:
        item = posixpath.normpath(prefix)
        if item == "." or rel == item or rel.startswith(item + "/"):
            return True
    return False


def _policy_violation(identity: AgentIdentity, name: str, arguments: dict[str, Any]) -> str | None:
    """Bot 级执行限制：命令白名单（terminal.execute）与路径白名单（path/paths 参数）。"""
    if name == "terminal.execute" and identity.allowed_commands is not None:
        command = arguments.get("command")
        if isinstance(command, str) and command.strip():
            allowed = set(identity.allowed_commands)
            for head in _command_heads(command):
                base = posixpath.basename(head)
                if head not in allowed and base not in allowed:
                    return f"命令 {base} 不在 Bot 允许的命令白名单里，已拒绝执行"
    if identity.allowed_paths is not None:
        for key in ("path", "paths"):
            raw = arguments.get(key)
            if isinstance(raw, str):
                values: list[Any] = [raw]
            elif isinstance(raw, (list, tuple)):
                values = list(raw)
            else:
                continue
            for item in values:
                if not isinstance(item, str) or not item.strip():
                    continue
                if not _path_allowed(_normalize_rel(item), identity.allowed_paths):
                    return f"路径 {item} 不在 Bot 允许的路径白名单里，已拒绝执行"
    return None


def _safe_id(message: Any) -> Any:
    """只接受 JSON-RPC 允许的 id 类型，其它一律当作无 id（避免回显奇怪对象）。"""
    if not isinstance(message, dict):
        return None
    value = message.get("id")
    return value if isinstance(value, (str, int)) and not isinstance(value, bool) else None


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _tool_text(data: dict[str, Any]) -> dict[str, Any]:
    """把工具的结构化结果渲染成一份 JSON 文本（所有 MCP 客户端都能消费）。"""
    return {
        "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}],
        "isError": False,
    }


def _tool_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _json(body: dict[str, Any]) -> JSONResponse:
    return JSONResponse(status_code=200, content=body)
