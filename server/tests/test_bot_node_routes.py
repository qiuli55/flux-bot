"""Bot 节点的路由面：Bot 相关能力已挂载，IDE 专属能力仍然缺席。"""

from __future__ import annotations

from fastapi import FastAPI

from flux.bot_node.main import create_app
from flux.config import Settings


def _paths(app: FastAPI) -> set[str]:
    """已注册的 HTTP 路径。

    新版 FastAPI 的 include_router 是惰性的（app.routes 里是 _IncludedRouter 包装），
    直接读 route.path 拿不到子路由；OpenAPI 的 paths 是与实现无关的展开结果。
    """
    return set(app.openapi()["paths"])


def test_bot_relevant_routers_are_registered(settings: Settings) -> None:
    """子任务 / 外发草稿 / 技能 / 密钥库 / 日历 / 接入 / 计费都要在 Bot 节点可用。"""
    paths = _paths(create_app(settings))

    expected = {
        # Bot 子任务
        "/api/v1/bots/{bot_id}/subtasks",
        "/api/v1/bots/{bot_id}/subtasks/{subtask_id}",
        "/api/v1/bots/{bot_id}/subtasks/{subtask_id}/cancel",
        # 外发草稿
        "/api/v1/outbound-drafts",
        "/api/v1/outbound-drafts/{draft_id}",
        # 技能库 + 每 Bot 分配
        "/api/v1/skills",
        "/api/v1/bots/{bot_id}/skills",
        # 密钥库
        "/api/v1/vault/logins",
        "/api/v1/vault/cards",
        # 日历连接器
        "/api/v1/connectors/calendar/events",
        "/api/v1/connectors/calendar/test",
        # Agent 接入（UI / API 连接 claude-code 的入口）
        "/api/v1/installations",
        "/api/v1/installations/connect",
        # 计费
        "/api/v1/billing/usage",
        "/api/v1/billing/plans",
    }
    missing = expected - paths
    assert not missing, f"Bot 节点缺少路由：{sorted(missing)}"


def test_ide_only_routers_stay_absent(settings: Settings) -> None:
    """IDE / 桌面工作台路由仍不应出现在 Bot 节点。"""
    paths = _paths(create_app(settings))

    excluded_prefixes = (
        "/api/v1/projects",
        "/api/v1/git",
        "/api/v1/ide",
        "/api/v1/terminal",
        "/api/v1/workspace",
        "/api/v1/designs",
        "/api/v1/supervisor",
    )
    leaked = {path for path in paths if path.startswith(excluded_prefixes)}
    assert not leaked, f"IDE 专属路由不应出现在 Bot 节点：{sorted(leaked)}"
