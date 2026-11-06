"""Flux Server CLI（NEXT_PHASE_OPTIMIZATION §9）：服务器管理命令。

三条设计取舍（对应 §9「不要为不同入口重复实现 Agent Runtime」）：

1. **进程内直连 `Container`**，不经 HTTP，也不经 MCP。这样 MCP / API 进程出问题时
   CLI 仍可用——它是 MCP 的兜底入口，而不是又一个 HTTP 客户端。
2. **身份不可自报**：只读命令（doctor / status / tools list / task / proposal / logs）
   不需要令牌；任何"以 Agent 身份行动"的命令（tools call）必须显式给 `--token`
   或环境变量 `FLUX_CLI_TOKEN`，身份由 `AgentTokenService.authenticate()` 现查现验。
3. **统一输出 JSON**（人类可读性让位给脚本可消费），错误也是一段 JSON + 非 0 退出码。

工具调用复用 MCP 的同一份 `ToolSpec.handler` 与能力校验——CLI 不复制第二份工具实现。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from flux.config import REPO_ROOT, Settings, get_settings
from flux.container import Container
from flux.core.mcp.tools import ALL_TOOLS, FORBIDDEN_TOOL_NAMES, TOOLS, ToolContext
from flux.db.session import check_database
from flux.enums import RecoveryResolution, TaskStatus, VirtualChangeStatus
from flux.errors import (
    AIOSError,
    BadRequestError,
    ConfigurationError,
    NotFoundError,
    PermissionDeniedError,
)
from flux.models.agent_run import AgentRun
from flux.models.task import Task
from flux.models.workspace import VirtualChange
from flux.version import VERSION

# --- 命令行定义 ---


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flux",
        description="Flux Server CLI：进程内直连 Flux Core 的服务器管理入口（MCP 兜底通道）。",
    )
    parser.add_argument("--version", action="version", version=f"flux {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="环境自检：数据库 / 迁移 / 工作区 / 工具面")
    sub.add_parser("status", help="任务、Run、提案的当前状态统计")

    tools = sub.add_parser("tools", help="MCP 工具面")
    tools_sub = tools.add_subparsers(dest="tools_command", required=True)
    tools_sub.add_parser("list", help="列出全部 MCP 工具及其能力要求")
    call = tools_sub.add_parser("call", help="以 Agent 身份调用一个 MCP 工具")
    call.add_argument("name", help="工具名，如 workspace.read")
    call.add_argument("--params", default="{}", help="工具入参 JSON 对象，默认 {}")
    call.add_argument("--token", default=None, help="Agent 接入令牌；缺省读 FLUX_CLI_TOKEN")

    task = sub.add_parser("task", help="任务")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    task_list = task_sub.add_parser("list", help="按创建时间倒序列出任务")
    task_list.add_argument("--status", choices=[str(s) for s in TaskStatus], default=None)
    task_list.add_argument("--limit", type=int, default=50)
    task_show = task_sub.add_parser("show", help="查看单个任务")
    task_show.add_argument("id", help="任务 UUID")
    task_show.add_argument("--messages", type=int, default=0, help="附带最近 N 条对话消息")

    proposal = sub.add_parser("proposal", help="改动提案（人工审核队列）")
    proposal_sub = proposal.add_subparsers(dest="proposal_command", required=True)
    proposal_list = proposal_sub.add_parser("list", help="列出提案")
    proposal_list.add_argument(
        "--status", choices=[str(s) for s in VirtualChangeStatus], default=None
    )
    proposal_list.add_argument("--task", default=None, help="按任务 UUID 过滤")
    proposal_show = proposal_sub.add_parser("show", help="查看单条提案（含 diff）")
    proposal_show.add_argument("id", help="提案 UUID")
    proposal_diff = proposal_sub.add_parser("diff", help="查看单条提案的差异（头部带动作标记）")
    proposal_diff.add_argument("id", help="提案 UUID")
    rollback = proposal_sub.add_parser("rollback", help="回滚已落盘的改动（P1-2）")
    rollback.add_argument("change_id", nargs="?", default=None, help="单条回滚的提案 UUID")
    rollback.add_argument("--batch", default=None, help="整批回滚的 Apply 批次 UUID")
    rollback.add_argument("--last", action="store_true", help="回滚最近一次成功 Apply")
    recovery = proposal_sub.add_parser("recovery", help="崩溃恢复挂起项的人工决策")
    recovery_sub = recovery.add_subparsers(dest="recovery_command", required=True)
    recovery_sub.add_parser("list", help="列出待人工决策的崩溃恢复项（三版本对照）")
    recovery_resolve = recovery_sub.add_parser("resolve", help="对一条挂起项做决策")
    recovery_resolve.add_argument("change_id", help="提案 UUID")
    decision = recovery_resolve.add_mutually_exclusive_group(required=True)
    decision.add_argument("--cover", action="store_true", help="用备份覆盖，还原为改动前原文")
    decision.add_argument("--keep", action="store_true", help="保持磁盘现状，提案作废")

    agents = sub.add_parser("agents", help="本机 CLI Agent 的发现与接入")
    agents_sub = agents.add_subparsers(dest="agents_command", required=True)
    agents_sub.add_parser("scan", help="扫描本机可接入的 CLI Agent（PATH → 版本 → 认证 → 能力）")
    agents_sub.add_parser("list", help="列出已发现的 Agent 及其状态")
    connect = agents_sub.add_parser("connect", help="接入一个 Agent（推进到 READY）")
    connect.add_argument("agent", nargs="?", default=None, help="Agent 名，如 opencode")
    connect.add_argument("--all", action="store_true", help="接入全部已安装的 Agent")
    remove = agents_sub.add_parser("remove", help="移除一个 Agent 的接入记录")
    remove.add_argument("agent", help="Agent 名，如 opencode")

    logs = sub.add_parser("logs", help="读取服务日志文件尾部")
    logs.add_argument("--lines", type=int, default=200, help="读取的行数，默认 200")
    logs.add_argument("--file", default=None, help="日志文件路径；缺省读 FLUX_LOG_FILE")

    return parser


# --- 入口 ---


def main(argv: list[str] | None = None, *, settings: Settings | None = None) -> int:
    """解析并执行一条 CLI 命令，返回进程退出码。

    `settings` 仅供测试注入；生产路径读仓库根 .env（与 API 进程同一份配置）。
    """
    args = build_parser().parse_args(argv)
    resolved = settings or get_settings()
    try:
        data, code = asyncio.run(_run(args, resolved))
    except AIOSError as exc:
        _emit(
            {
                "ok": False,
                "error": {"code": exc.code, "message": exc.message, "details": exc.details},
            }
        )
        return 1
    except SQLAlchemyError as exc:
        # 数据库不可达或结构未就绪（未迁移）是最常见的运维现场，给一条能照做的提示，
        # 而不是把整段带 SQL 的堆栈丢给运维。
        _emit(
            {
                "ok": False,
                "error": {
                    "code": "database_error",
                    "message": str(exc).splitlines()[0],
                    "details": {"hint": "数据库不可用或结构未就绪；确认连接串并运行 make migrate"},
                },
            }
        )
        return 1
    except Exception as exc:  # 顶层兜底：任何未预期异常都转成一段可读 JSON，不留裸 traceback
        _emit({"ok": False, "error": {"code": "internal_error", "message": str(exc)}})
        return 1
    _emit({"ok": code == 0, "data": data})
    return code


async def _run(args: argparse.Namespace, settings: Settings) -> tuple[dict[str, Any], int]:
    # logs 只读文件，不需要装配容器（也就不碰数据库）
    if args.command == "logs":
        return _cmd_logs(args), 0

    container = Container(settings)
    try:
        if args.command == "doctor":
            return await _cmd_doctor(container)
        if args.command == "status":
            return await _cmd_status(container), 0
        if args.command == "tools":
            if args.tools_command == "list":
                return _cmd_tools_list(), 0
            return await _cmd_tools_call(container, args), 0
        if args.command == "task":
            if args.task_command == "list":
                return await _cmd_task_list(container, args), 0
            return await _cmd_task_show(container, args), 0
        if args.command == "proposal":
            if args.proposal_command == "list":
                return await _cmd_proposal_list(container, args), 0
            if args.proposal_command == "recovery":
                if args.recovery_command == "list":
                    return await _cmd_proposal_recovery_list(container), 0
                return await _cmd_proposal_recovery_resolve(container, args), 0
            if args.proposal_command == "diff":
                return await _cmd_proposal_diff(container, args), 0
            if args.proposal_command == "rollback":
                return await _cmd_proposal_rollback(container, args), 0
            return await _cmd_proposal_show(container, args), 0
        if args.command == "agents":
            if args.agents_command == "scan":
                return await _cmd_agents_scan(container), 0
            if args.agents_command == "list":
                return await _cmd_agents_list(container), 0
            if args.agents_command == "connect":
                return await _cmd_agents_connect(container, args), 0
            return await _cmd_agents_remove(container, args), 0
    finally:
        await container.dispose()
    raise BadRequestError(f"未知命令：{args.command}")


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


# --- doctor ---


def _check(name: str, ok: bool, level: str, detail: Any) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "level": level, "detail": detail}


async def _cmd_doctor(container: Container) -> tuple[dict[str, Any], int]:
    settings = container.settings
    checks: list[dict[str, Any]] = []

    db_url = _redact_url(settings.database_url)
    db_ok = await check_database(container.engine)
    checks.append(_check("database", db_ok, "error", db_url if db_ok else f"无法连接：{db_url}"))

    alembic = await _alembic_version(container)
    checks.append(
        _check(
            "migrations",
            alembic is not None,
            "warn",
            f"alembic_version={alembic}"
            if alembic
            else "未找到 alembic_version（可能未执行 make migrate）",
        )
    )

    checks.append(_workspace_check(settings.workspace_root))

    checks.append(
        _check(
            "dsh",
            True,
            "info",
            f"dsh_enabled={settings.dsh_enabled}",
        )
    )

    forbidden = sorted(FORBIDDEN_TOOL_NAMES & set(TOOLS))
    tools_detail = (
        f"{len(ALL_TOOLS)} 个工具，硬禁令工具缺席"
        if not forbidden
        else f"工具面混入硬禁令：{forbidden}"
    )
    checks.append(_check("tools", not forbidden, "error", tools_detail))

    checks.append(await _agents_check(container, db_ok))

    overall_ok = all(check["ok"] or check["level"] != "error" for check in checks)
    data = {
        "version": VERSION,
        "env": settings.env,
        "database_url": _redact_url(settings.database_url),
        "checks": checks,
    }
    return data, (0 if overall_ok else 1)


def _workspace_check(workspace_root: str | None) -> dict[str, Any]:
    if not workspace_root:
        return _check("workspace_root", False, "warn", "未配置（Apply 落盘与文件读取不可用）")
    path = Path(workspace_root)
    if not path.is_dir():
        return _check("workspace_root", False, "warn", f"目录不存在：{path}")
    return _check("workspace_root", True, "info", str(path))


async def _agents_check(container: Container, db_ok: bool) -> dict[str, Any]:
    if not db_ok:
        return _check("agents", False, "warn", "跳过：数据库不可用")
    try:
        await container.agents.load_from_db()
    except Exception as exc:  # 表未建（未迁移）时给出提示，而不是让 doctor 直接崩
        return _check("agents", False, "warn", f"读取注册表失败：{exc}")
    return _check("agents", True, "info", f"{container.agents.count()} 个 Agent 档案")


async def _alembic_version(container: Container) -> str | None:
    try:
        async with container.engine.connect() as conn:
            result = await conn.execute(text("SELECT version_num FROM alembic_version"))
            return result.scalar_one_or_none()
    except Exception:
        return None


# --- status ---


async def _cmd_status(container: Container) -> dict[str, Any]:
    if not await check_database(container.engine):
        raise ConfigurationError(
            "数据库不可用，无法读取状态",
            details={"database_url": _redact_url(container.settings.database_url)},
        )
    async with container.session_factory() as session:
        tasks = _counts(
            await session.execute(select(Task.status, func.count(Task.id)).group_by(Task.status))
        )
        proposals = _counts(
            await session.execute(
                select(VirtualChange.status, func.count(VirtualChange.id)).group_by(
                    VirtualChange.status
                )
            )
        )
        runs = _counts(
            await session.execute(
                select(AgentRun.status, func.count(AgentRun.id)).group_by(AgentRun.status)
            )
        )
    non_terminal = await container.run_repo.list_non_terminal()
    return {
        "database": "ok",
        "tasks": _bucket(tasks),
        "proposals": _bucket(proposals),
        "runs": _bucket(runs),
        "non_terminal_runs": [run.to_dict() for run in non_terminal],
    }


def _counts(result: Any) -> dict[str, int]:
    return {str(status): int(count) for status, count in result.all()}


def _bucket(by_status: dict[str, int]) -> dict[str, Any]:
    return {"total": sum(by_status.values()), "by_status": by_status}


# --- tools ---


def _cmd_tools_list() -> dict[str, Any]:
    return {
        "count": len(ALL_TOOLS),
        "forbidden": sorted(FORBIDDEN_TOOL_NAMES),
        "tools": [
            {
                "name": tool.name,
                "title": tool.title,
                "capability": str(tool.capability),
                "approval_capable": tool.approval_capable,
                "input_schema": tool.input_schema,
            }
            for tool in ALL_TOOLS
        ],
    }


async def _cmd_tools_call(container: Container, args: argparse.Namespace) -> dict[str, Any]:
    spec = TOOLS.get(args.name)
    if spec is None:
        raise NotFoundError(f"工具 {args.name} 不存在", details={"known": sorted(TOOLS)})

    # Agent 档案的权威在 agents 表：CLI 进程没跑过 API 的 lifespan，必须先把注册表读进内存，
    # 否则 authenticate() 回查不到档案会 fail-closed（这正是 P3-16 的设计，不是 bug）。
    await container.agents.load_from_db()

    token = args.token or os.environ.get("FLUX_CLI_TOKEN")
    identity = await container.agent_tokens.authenticate(token)
    if not identity.has(spec.capability):
        raise PermissionDeniedError(
            f"令牌身份缺少能力 {spec.capability}",
            details={
                "agent_id": identity.agent_id,
                "required": str(spec.capability),
                "granted": sorted(str(scope) for scope in identity.scopes),
            },
        )

    params = _parse_params(args.params)
    ctx = ToolContext(container=container, identity=identity, tool_name=spec.name)
    result = await spec.handler(ctx, params)
    return {"tool": spec.name, "agent_id": identity.agent_id, "result": result}


def _parse_params(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise BadRequestError(f"--params 不是合法 JSON：{exc}") from exc
    if not isinstance(value, dict):
        raise BadRequestError("--params 必须是 JSON 对象")
    return value


# --- task ---


async def _cmd_task_list(container: Container, args: argparse.Namespace) -> dict[str, Any]:
    status = TaskStatus(args.status) if args.status else None
    tasks = await container.task_repo.list(status=status, limit=args.limit)
    return {"count": len(tasks), "tasks": [task.to_dict() for task in tasks]}


async def _cmd_task_show(container: Container, args: argparse.Namespace) -> dict[str, Any]:
    task = await container.task_repo.get(_uuid(args.id, "任务"))
    data = task.to_dict()
    if args.messages > 0:
        messages, has_more = await container.task_repo.list_messages(task.id, limit=args.messages)
        data["messages"] = [message.to_dict() for message in messages]
        data["messages_has_more"] = has_more
    return data


# --- proposal ---


async def _cmd_proposal_list(container: Container, args: argparse.Namespace) -> dict[str, Any]:
    status = VirtualChangeStatus(args.status) if args.status else None
    task_id = _uuid(args.task, "任务") if args.task else None
    changes = await container.proposal_repo.list(status=status, task_id=task_id)
    return {"count": len(changes), "proposals": [_proposal_summary(change) for change in changes]}


async def _cmd_proposal_show(container: Container, args: argparse.Namespace) -> dict[str, Any]:
    change = await container.workspace.get(args.id)
    detail = change.to_dict()
    if change.status == VirtualChangeStatus.APPLYING.value:
        detail["hint"] = APPLYING_HINT
    return detail


#: 动作标记：git 风格的 A/M/D，delete 头部即 `D <path>`（P1-1）
_KIND_LETTERS = {"create": "A", "modify": "M", "delete": "D"}


async def _cmd_proposal_diff(container: Container, args: argparse.Namespace) -> dict[str, Any]:
    """输出单条提案的差异；头部带一行动作标记（新增 A / 修改 M / 删除 D + 路径）。"""
    change = await container.workspace.get(args.id)
    kind = change.kind or "modify"
    return {
        "id": str(change.id),
        "file_path": change.file_path,
        "kind": kind,
        "status": change.status,
        "header": f"{_KIND_LETTERS.get(kind, 'M')} {change.file_path}",
        "diff": change.diff or "",
        "added_lines": change.added_lines,
        "removed_lines": change.removed_lines,
        "hunks": change.hunks,
    }


async def _cmd_proposal_rollback(container: Container, args: argparse.Namespace) -> dict[str, Any]:
    """回滚已落盘的改动：单条（change_id）/ 整批（--batch）/ 最近一次（--last）。"""
    if args.batch:
        batch = await container.workspace.rollback_batch(args.batch)
        return {
            "batch_id": str(batch.id),
            "status": batch.status,
            "change_ids": list(batch.change_ids),
        }
    if args.last:
        candidates = await container.workspace.list_recent_batches(recent=True, limit=1)
        if not candidates:
            raise NotFoundError("没有可回滚的 Apply 批次（最近没有 applied 批）")
        batch = await container.workspace.rollback_batch(candidates[0].id)
        return {
            "batch_id": str(batch.id),
            "status": batch.status,
            "change_ids": list(batch.change_ids),
        }
    if not args.change_id:
        raise BadRequestError("请给出 change_id，或用 --batch <id> / --last 指定要回滚的批次")
    change = await container.workspace.rollback_change(args.change_id)
    return {
        "change_id": str(change.id),
        "file_path": change.file_path,
        "kind": change.kind,
        "status": change.status,
    }


async def _cmd_proposal_recovery_list(container: Container) -> dict[str, Any]:
    """列出待人工决策的崩溃恢复项：每项含三版本（备份原文 / 磁盘现状 / 提案内容）。"""
    items = await container.workspace.list_pending_recovery_items()
    return {"count": len(items), "items": [item.to_dict() for item in items]}


async def _cmd_proposal_recovery_resolve(
    container: Container, args: argparse.Namespace
) -> dict[str, Any]:
    """对一条挂起项做决策：--cover 用备份覆盖还原；--keep 保持现状、提案作废。"""
    resolution = RecoveryResolution.COVER if args.cover else RecoveryResolution.KEEP
    change = await container.workspace.resolve_recovery(args.change_id, resolution)
    return {"change_id": str(change.id), "status": change.status, "resolution": resolution.value}


#: P0-1：applying 意味着上一轮 Apply 中断（进程被杀），恢复会在服务启动或下次 Apply 时自动发生
APPLYING_HINT = (
    "该提案的上一轮 Apply 被中断：服务启动或下次 Apply 时会自动对账"
    "（文件回改动前、提案回 accepted），无需人工处理"
)


def _proposal_summary(change: VirtualChange) -> dict[str, Any]:
    summary = {
        "id": str(change.id),
        "file_path": change.file_path,
        "kind": change.kind,
        "status": change.status,
        "task_id": str(change.task_id) if change.task_id else None,
        "group_id": str(change.group_id) if change.group_id else None,
        "agent_source": change.agent_source,
        "added_lines": change.added_lines,
        "removed_lines": change.removed_lines,
        "hunks": change.hunks,
        "summary": change.summary,
        "created_at": change.created_at.isoformat() if change.created_at else None,
    }
    if change.status == VirtualChangeStatus.APPLYING.value:
        summary["hint"] = APPLYING_HINT
    return summary


# --- agents（发现 / 接入）---


async def _cmd_agents_scan(container: Container) -> dict[str, Any]:
    rows = await container.installations.scan()
    return {"count": len(rows), "agents": [row.to_dict() for row in rows]}


async def _cmd_agents_list(container: Container) -> dict[str, Any]:
    rows = await container.installations.list()
    return {"count": len(rows), "agents": [row.to_dict() for row in rows]}


async def _cmd_agents_connect(container: Container, args: argparse.Namespace) -> dict[str, Any]:
    if args.all:
        rows = await container.installations.connect_all()
        return {"count": len(rows), "agents": [row.to_dict() for row in rows]}
    if not args.agent:
        raise BadRequestError("请指定 Agent 名，或用 --all 接入全部已安装的 Agent")
    row = await container.installations.connect(args.agent)
    return {"agent": row.to_dict()}


async def _cmd_agents_remove(container: Container, args: argparse.Namespace) -> dict[str, Any]:
    await container.installations.remove(args.agent)
    return {"removed": args.agent}


# --- logs ---


def _cmd_logs(args: argparse.Namespace) -> dict[str, Any]:
    path = args.file or os.environ.get("FLUX_LOG_FILE")
    if not path:
        candidate = REPO_ROOT / "logs" / "flux.log"
        path = str(candidate) if candidate.exists() else None
    if not path:
        raise ConfigurationError(
            "未配置日志文件",
            details={
                "hint": "Flux 默认把日志写到 stdout，由 systemd / docker 收集。"
                "请用 --file 或 FLUX_LOG_FILE 指定日志文件，或改用 docker compose logs backend。"
            },
        )
    log_path = Path(path).expanduser()
    if not log_path.is_file():
        raise NotFoundError(f"日志文件不存在：{log_path}", details={"file": str(log_path)})
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    tail = lines[-max(1, args.lines) :]
    return {"file": str(log_path), "count": len(tail), "lines": tail}


# --- 工具函数 ---


def _uuid(value: str, label: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError) as exc:
        raise BadRequestError(f"{label} ID 非法：{value}") from exc


def _redact_url(url: str) -> str:
    """隐去连接串里的密码，避免日志/输出泄露凭据。"""
    if "://" not in url or "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, host = rest.rsplit("@", 1)
    user = creds.split(":", 1)[0]
    if ":" not in creds:
        return url
    return f"{scheme}://{user}:***@{host}"
