"""Agent 底座切换：存量 dsh runtime 回填为 claude-code

DSH 引擎已移除，Claude Code 成为唯一默认底座。旧库里 `agents.config.runtime` 可能仍是
"dsh"，升级后会被判为未知 runtime 而起不了 Run；这里在迁移期一次性改成 "claude-code"，
覆盖所有存量 Bot（新老 Bot 共用同一批内置 Agent 档案）。

Revision ID: a4c8d2e61f97
Revises: f4b7e1c9a260
Create Date: 2026-11-13 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4c8d2e61f97"
down_revision: str | Sequence[str] | None = "f4b7e1c9a260"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY_RUNTIMES = frozenset({"dsh"})
_TARGET_RUNTIME = "claude-code"


def upgrade() -> None:
    """把存量 Agent 的 dsh runtime 回填为 claude-code。"""
    bind = op.get_bind()
    agents = sa.table(
        "agents",
        sa.column("id", sa.Uuid()),
        sa.column("config", sa.JSON()),
    )
    rows = bind.execute(sa.select(agents.c.id, agents.c.config)).all()
    for agent_id, config in rows:
        if not isinstance(config, dict):
            continue
        if str(config.get("runtime") or "").strip() not in _LEGACY_RUNTIMES:
            continue
        config["runtime"] = _TARGET_RUNTIME
        bind.execute(agents.update().where(agents.c.id == agent_id).values(config=config))


def downgrade() -> None:
    """不回退：无法区分哪些 claude-code 原本是 dsh。"""
