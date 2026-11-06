"""助理（Supervisor）：13 张编排表

参考 DeepSeek Super-agent 的 Source / Trigger / Thread / Eval / Mandate / Ticket /
Assignment / Report / Outbox 分层，按 Flux 多项目、多平台场景重写。要点：

- 外部内容先落 `supervisor_triggers`（`dedupe_key` 唯一 = 幂等依据）；
- 判断层无状态，只往 `supervisor_evals` 追加结论，用于回放；
- 有状态的只有 thread / ticket / assignment / report；执行仍归既有 tasks；
- `supervisor_outbox` 与事件同事务入队，进程崩溃后按它重放。

`supervisor_digests` 属于后续阶段，但一并建表以减少迁移次数。

Revision ID: e1b7c4a9d2f6
Revises: d2f8a6c41e07
Create Date: 2026-11-02 23:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1b7c4a9d2f6"
down_revision: str | Sequence[str] | None = "d2f8a6c41e07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "supervisor_settings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("singleton", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("accept_mode", sa.String(length=16), nullable=False, server_default="observe"),
        sa.Column("model_provider", sa.String(length=32), nullable=True),
        sa.Column("model_name", sa.String(length=128), nullable=True),
        sa.Column("max_tickets_per_thread", sa.Integer(), nullable=False, server_default="6"),
        sa.Column("max_ticket_depth", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("max_concurrent_runs", sa.Integer(), nullable=False, server_default="2"),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("singleton"),
    )

    op.create_table(
        "supervisor_sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("config", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
    )

    op.create_table(
        "supervisor_policies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("source_key", sa.String(length=64), nullable=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("trigger_types", sa.JSON(), nullable=False),
        sa.Column("accept_mode", sa.String(length=16), nullable=True),
        sa.Column("rules", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_supervisor_policies_project_id"), "supervisor_policies", ["project_id"]
    )
    op.create_index(
        op.f("ix_supervisor_policies_source_key"), "supervisor_policies", ["source_key"]
    )

    op.create_table(
        "supervisor_triggers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_key", sa.String(length=64), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("dedupe_key", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.String(length=255), nullable=False),
        sa.Column("actor_platform", sa.String(length=32), nullable=False),
        sa.Column("thread_key", sa.String(length=255), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("raw", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="received"),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key"),
    )
    op.create_index(
        op.f("ix_supervisor_triggers_source_key"), "supervisor_triggers", ["source_key"]
    )
    op.create_index(
        op.f("ix_supervisor_triggers_external_id"), "supervisor_triggers", ["external_id"]
    )
    op.create_index(
        op.f("ix_supervisor_triggers_project_id"), "supervisor_triggers", ["project_id"]
    )
    op.create_index(op.f("ix_supervisor_triggers_status"), "supervisor_triggers", ["status"])

    op.create_table(
        "supervisor_threads",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("thread_key", sa.String(length=255), nullable=False),
        sa.Column("source_key", sa.String(length=64), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("thread_key"),
    )
    op.create_index(op.f("ix_supervisor_threads_source_key"), "supervisor_threads", ["source_key"])
    op.create_index(op.f("ix_supervisor_threads_project_id"), "supervisor_threads", ["project_id"])
    op.create_index(
        op.f("ix_supervisor_threads_last_activity_at"),
        "supervisor_threads",
        ["last_activity_at"],
    )

    op.create_table(
        "supervisor_evals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("trigger_id", sa.Uuid(), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("risk", sa.JSON(), nullable=False),
        sa.Column("model_meta", sa.JSON(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["trigger_id"], ["supervisor_triggers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_supervisor_evals_trigger_id"), "supervisor_evals", ["trigger_id"])
    op.create_index(op.f("ix_supervisor_evals_decision"), "supervisor_evals", ["decision"])

    op.create_table(
        "supervisor_mandates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("thread_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.String(length=255), nullable=False),
        sa.Column("actor_platform", sa.String(length=32), nullable=False),
        sa.Column("strength", sa.String(length=16), nullable=False),
        sa.Column("scope", sa.JSON(), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["thread_id"], ["supervisor_threads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_supervisor_mandates_thread_id"), "supervisor_mandates", ["thread_id"])

    op.create_table(
        "supervisor_tickets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("thread_id", sa.Uuid(), nullable=False),
        sa.Column("parent_ticket_id", sa.Uuid(), nullable=True),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("plan_key", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("depends_on", sa.JSON(), nullable=False),
        sa.Column("degraded", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("sequence", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["thread_id"], ["supervisor_threads.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["parent_ticket_id"], ["supervisor_tickets.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id"),
    )
    op.create_index(op.f("ix_supervisor_tickets_thread_id"), "supervisor_tickets", ["thread_id"])
    op.create_index(op.f("ix_supervisor_tickets_project_id"), "supervisor_tickets", ["project_id"])

    op.create_table(
        "supervisor_subscriptions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ticket_id", sa.Uuid(), nullable=False),
        sa.Column("source_key", sa.String(length=64), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False, server_default="terminal"),
        *_timestamps(),
        sa.ForeignKeyConstraint(["ticket_id"], ["supervisor_tickets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_supervisor_subscriptions_ticket_id"), "supervisor_subscriptions", ["ticket_id"]
    )

    op.create_table(
        "supervisor_assignments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ticket_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=True),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="dispatched"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["ticket_id"], ["supervisor_tickets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_supervisor_assignments_ticket_id"), "supervisor_assignments", ["ticket_id"]
    )
    op.create_index(op.f("ix_supervisor_assignments_run_id"), "supervisor_assignments", ["run_id"])

    op.create_table(
        "supervisor_reports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ticket_id", sa.Uuid(), nullable=False),
        sa.Column("assignment_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["ticket_id"], ["supervisor_tickets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["assignment_id"], ["supervisor_assignments.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_supervisor_reports_ticket_id"), "supervisor_reports", ["ticket_id"])
    op.create_index(op.f("ix_supervisor_reports_status"), "supervisor_reports", ["status"])

    op.create_table(
        "supervisor_digests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("thread_id", sa.Uuid(), nullable=True),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("decisions", sa.JSON(), nullable=False),
        sa.Column("ticket_ids", sa.JSON(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["thread_id"], ["supervisor_threads.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_supervisor_digests_thread_id"), "supervisor_digests", ["thread_id"])
    op.create_index(op.f("ix_supervisor_digests_project_id"), "supervisor_digests", ["project_id"])

    op.create_table(
        "supervisor_outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("topic", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_supervisor_outbox_topic"), "supervisor_outbox", ["topic"])
    op.create_index(op.f("ix_supervisor_outbox_status"), "supervisor_outbox", ["status"])
    op.create_index(
        op.f("ix_supervisor_outbox_available_at"), "supervisor_outbox", ["available_at"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_supervisor_outbox_available_at"), table_name="supervisor_outbox")
    op.drop_index(op.f("ix_supervisor_outbox_status"), table_name="supervisor_outbox")
    op.drop_index(op.f("ix_supervisor_outbox_topic"), table_name="supervisor_outbox")
    op.drop_table("supervisor_outbox")

    op.drop_index(op.f("ix_supervisor_digests_project_id"), table_name="supervisor_digests")
    op.drop_index(op.f("ix_supervisor_digests_thread_id"), table_name="supervisor_digests")
    op.drop_table("supervisor_digests")

    op.drop_index(op.f("ix_supervisor_reports_status"), table_name="supervisor_reports")
    op.drop_index(op.f("ix_supervisor_reports_ticket_id"), table_name="supervisor_reports")
    op.drop_table("supervisor_reports")

    op.drop_index(op.f("ix_supervisor_assignments_run_id"), table_name="supervisor_assignments")
    op.drop_index(op.f("ix_supervisor_assignments_ticket_id"), table_name="supervisor_assignments")
    op.drop_table("supervisor_assignments")

    op.drop_index(
        op.f("ix_supervisor_subscriptions_ticket_id"), table_name="supervisor_subscriptions"
    )
    op.drop_table("supervisor_subscriptions")

    op.drop_index(op.f("ix_supervisor_tickets_project_id"), table_name="supervisor_tickets")
    op.drop_index(op.f("ix_supervisor_tickets_thread_id"), table_name="supervisor_tickets")
    op.drop_table("supervisor_tickets")

    op.drop_index(op.f("ix_supervisor_mandates_thread_id"), table_name="supervisor_mandates")
    op.drop_table("supervisor_mandates")

    op.drop_index(op.f("ix_supervisor_evals_decision"), table_name="supervisor_evals")
    op.drop_index(op.f("ix_supervisor_evals_trigger_id"), table_name="supervisor_evals")
    op.drop_table("supervisor_evals")

    op.drop_index(op.f("ix_supervisor_threads_last_activity_at"), table_name="supervisor_threads")
    op.drop_index(op.f("ix_supervisor_threads_project_id"), table_name="supervisor_threads")
    op.drop_index(op.f("ix_supervisor_threads_source_key"), table_name="supervisor_threads")
    op.drop_table("supervisor_threads")

    op.drop_index(op.f("ix_supervisor_triggers_status"), table_name="supervisor_triggers")
    op.drop_index(op.f("ix_supervisor_triggers_project_id"), table_name="supervisor_triggers")
    op.drop_index(op.f("ix_supervisor_triggers_external_id"), table_name="supervisor_triggers")
    op.drop_index(op.f("ix_supervisor_triggers_source_key"), table_name="supervisor_triggers")
    op.drop_table("supervisor_triggers")

    op.drop_index(op.f("ix_supervisor_policies_source_key"), table_name="supervisor_policies")
    op.drop_index(op.f("ix_supervisor_policies_project_id"), table_name="supervisor_policies")
    op.drop_table("supervisor_policies")

    op.drop_table("supervisor_sources")

    op.drop_table("supervisor_settings")
