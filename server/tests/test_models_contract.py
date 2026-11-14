"""数据模型契约测试：代码里的表必须与主规格 §11.2 完全一致。"""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import sys
from pathlib import Path

from sqlalchemy import inspect

from flux.db.session import create_engine
from flux.models import Base

BACKEND_ROOT = Path(__file__).resolve().parents[1]

# 主规格 §11.2 表清单（裁决 A13 已把 agent_logs 并入 agent_execution_logs）
SPEC_TABLES = {
    "users",
    "provider_accounts",
    "organizations",
    "organization_members",
    "agents",
    "agent_skills",
    "agent_execution_logs",
    "projects",
    "project_memory",
    "tasks",
    "virtual_changes",
    "connectors",
    "connector_logs",
    # 账号侧连接器框架（迁移 a1c4e7f92b30）：通用安装元数据 + api_key 凭据密文。
    # GitHub 的 OAuth 令牌仍复用 github_connections，本表不重复存令牌。
    "connector_installations",
    # 外部连接器：本地账号 ↔ GitHub 身份绑定（迁移 c8e4a1f72b93）。
    # 令牌以密钥库信封加密落库，接口与日志只暴露身份与 scopes。
    "github_connections",
    "usage_records",
    # §11.2 之外的新增表：官方套餐额度快照（计费 P0）。
    # 官方接口只给套餐总量、没有任务归因，快照负责把百分比钉在时间轴上用于对账与闸门判定。
    "usage_snapshots",
    # §11.2 之外的新增表：MCP 能力面的鉴权凭据（目标架构 §3.2）。
    # 它不是业务实体，因此不入主规格表清单，但必须与其余表一样受本文件全部约束。
    "agent_tokens",
    # §11.2 之外的新增表：Solo 任务执行中心的对话消息（任务消息/步骤落库）。
    # 会话数据依附于任务，删除任务即级联清理，同样受本文件全部约束。
    "task_messages",
    # §11.2 之外的新增表：DSH Agent Run 的生命周期权威记录（修复方案 §2.2）。
    # Run 状态、心跳、进程归属（pid/pgid/owner_pid）落库，重启与多实例共享同库时可对账。
    "agent_runs",
    # §11.2 之外的新增表：本机 CLI Agent 的发现与接入状态（最终方案 §3.2 / §4）。
    # 与 agents 表（身份/权限）分离：这里只记"装没装、哪个版本、接没接入"。
    "agent_installations",
    # §11.2 之外的新增表：三层记忆的 User / Environment 层（批次② §4.2）。
    # Project 层复用 project_memory（Project Brain）；本表同样受本文件全部约束。
    "memories",
    # §11.2 之外的新增表：能力导入注册表（批次③ §5）。
    # 同一 (kind, name) 唯一，fingerprint 判定 unchanged / keep / replace。
    "imported_capabilities",
    # §11.2 之外的新增表：Apply 事务日志（P0-1 崩溃恢复；P1-2 整批回滚的分组依据）。
    # 一行 = 一次 apply_many，必须在任何磁盘操作之前落库，重启后据此对账。
    "apply_batches",
    # §11.2 之外的新增表：终端会话与终端事件（Agent Terminal Console §6 / §8）。
    # 会话是"观察 + 控制"的载体，事件按会话内单调 seq 落库以支持历史恢复与 SSE 重连。
    "terminal_sessions",
    "terminal_events",
    # FLUX_CONTEXT_MECHANISM §3.3 / §3.5 的上下文机制最小闭环。
    # context_steps 是压缩 / 检索 / 原子事实的最小单元（有意用自增整数主键，
    # 见 flux/models/context.py 的说明）；context_blobs 是内容寻址原文库
    # （sha256 去重 + zstd/lzma 无损压缩）。
    "context_steps",
    "context_blobs",
    # FLUX_CONTEXT_MECHANISM §7.2 的反向索引：文件 / 命令 / URL / 错误关键字 → step_id。
    # 正向 fetch 需要 id，而接管 agent 手里往往只有线索；没有本表回看通道等于没有入口。
    "context_step_tokens",
    # FLUX_CONTEXT_MECHANISM §4.4 的验证账本：一行 = 一次"某指纹在某类断言上被验证"。
    # 派生数据（记的是别人验证的结论），随会话级联删除；自增整数主键，理由见
    # flux/models/context.py 的 ContextVerification 说明。
    "context_verifications",
    # FLUX_CONTEXT_MECHANISM §10.1 的脱敏映射表：占位符 → 被换下来的原文，值单独加密。
    # 本表本身敏感，随会话级联删除（§8.2 / §13-5）。
    "context_redactions",
    # 密钥库：Agent 模型通道的 API Key（加密落库，明文不出接口）。与其余表一样受本文件约束。
    "api_keys",
    # 执行轨迹持久化：思考段 / 工具调用 / 错误行按 Run 落库，
    # 对话页的「任务耗时 · 概览 · 思考过程 · 命令输出」刷新与换设备后仍可回看。
    "run_trace_items",
    # 跨轮记忆：每轮结束平台生成的工作记忆卡，下一轮开工注入执行指令。
    "task_memory_cards",
    # 任务 ↔ 外部 Agent 会话绑定（opencode / codex 跨轮续聊）。
    "task_runtime_sessions",
    # 助理（Supervisor）编排层：外部事实 → 无状态判断 → 工单 → 派活 → 报告。
    # 执行仍归 tasks / agent_runs，这里只保存编排状态；13 张表同属一个迁移
    # （e1b7c4a9d2f6），因此一并纳入契约。
    "supervisor_settings",
    "supervisor_sources",
    "supervisor_policies",
    "supervisor_triggers",
    "supervisor_threads",
    "supervisor_evals",
    "supervisor_mandates",
    "supervisor_tickets",
    "supervisor_subscriptions",
    "supervisor_assignments",
    "supervisor_reports",
    "supervisor_digests",
    "supervisor_outbox",
    # §11.2 之外的新增表：助理知识库（RAG）。documents 是切块来源（对话 / docs / 项目记忆），
    # chunks 是检索单元（正文 + 词法 + 可选向量）；两者随来源替换同事务刷新。
    "knowledge_documents",
    "knowledge_chunks",
    # 微信接入（OpenClaw / 微信 ClawBot 只做传输层）：微信私聊 ↔ Flux 助理对话的映射，
    # 兼作入站消息幂等（last_message_id）。消息本身仍落 task_messages。
    "wechat_sessions",
    # 常驻 Bot（迁移 c6b3f8a1d027）：每次开工落一条带 bot_id 的任务。
    "bots",
    # 内部多 bot 群聊（2026-11-04）：群 / 成员 / epoch / 消息四张表同属一个迁移
    # （f6b2c9d4a81e）。成员是常驻 Bot（bots.id），群消息按群内 seq 单调递增。
    "chat_groups",
    "chat_group_members",
    "chat_group_epochs",
    "chat_group_messages",
    # 群聊成员状态（每个 bot 在每个群的 last_seen_seq / turns / 私聊摘要）与
    # 分层记忆（scope=group 本群 / scope=team 跨群），同属迁移 b7d1a9c4e320。
    "chat_group_member_states",
    "chat_group_memories",
    # Bot 分层长期记忆（迁移 d9e4a7c2b613）：摘要行 + 全文副本分块，
    # 摘要按 relevance（direct / indirect）分层，chunks 保留当时原文。
    "bot_memories",
    "bot_memory_chunks",
    # Bot 自动化（迁移 a7f3c9e1d284）：一个 Bot 多个例程（各自 cron / 指令），
    # 以及开工失败等主动通知（read_at 为空即未读）。
    "bot_routines",
    "bot_notifications",
    # Bot 对话轮次（迁移 e4f1a2b3c4d5）：一轮用户消息 → 助手回复的持久化状态，
    # 同时承担 client_turn_id 幂等与重启后的 interrupted 恢复。
    "bot_turns",
    # 跨 Bot 共享的用户记忆（迁移 c7e2a9b4d6f1）：画像键值 + 去重共享日志。
    # 与 bot_memories（每 Bot 记忆）和 memories（三层记忆）分开存储。
    "user_profile_entries",
    "user_journal_entries",
    # 用户保险库（迁移 d7a1f4c9b2e6）：站点登录 / 支付卡 / 联系人。
    # 密码、完整卡号、CVV 均以 SecretBox 信封密文落库；接口只回掩码。
    "vault_logins",
    "vault_cards",
    "vault_contacts",
    # 技能包库（迁移 a7c1e9d4b2f6）：可 CRUD 的技能正文 + 每 Bot 启用/排序分配。
    "skill_packages",
    "bot_skills",
    # 统一出站草稿（迁移 e2c9b7a4d1f3）：各频道待审批出站正文的持久化真相源。
    "outbound_drafts",
    # 全局审批规则（迁移 a9c4e7f1b2d6）：按 Flux 云用户配置，空表 = 零审批。
    "approval_rules",
    # Phase 3（迁移 b9f3c1d8a4e2）：IM 账号绑定 / 一次性绑定码，以及共享空间与成员。
    "channel_user_bindings",
    "channel_bind_codes",
    "spaces",
    "space_members",
    # 手机设备配对（迁移 f9d2e4a7b1c8）：devices 存长期设备令牌哈希，
    # pairing_codes 存一次性配对码哈希；两者都不落明文。
    "devices",
    "pairing_codes",
    # Bot 产出的可分享制品页面（迁移 b3d7e1a9c5f2）：页面当前状态 + 只追加的版本历史。
    # 公开链接按不可猜测的 share_token 读取，仅 published 页面可访问。
    "artifact_pages",
    "artifact_page_revisions",
    # §11.2 之外的新增表：Email 入站轮询游标（迁移 b7c2e4f9a1d3）。
    # 每个 (account_key, mailbox) 一行，记录已处理的最大 IMAP UID 与最近轮询状态；
    # 进程内去重集合重启即丢，持久化游标才能避免重复发布 email.received。
    "email_trigger_cursors",
}


def test_metadata_matches_spec_table_list() -> None:
    assert set(Base.metadata.tables) == SPEC_TABLES


def test_every_table_has_uuid_pk_and_timestamps() -> None:
    """§11.5：主键一律 UUID，每张表必须有 created_at / updated_at。"""
    for name, table in Base.metadata.tables.items():
        assert "id" in table.c, f"{name} 缺少主键 id"
        assert table.c["id"].primary_key, f"{name}.id 不是主键"
        for column in ("created_at", "updated_at"):
            assert column in table.c, f"{name} 缺少 {column}"


def test_schema_can_be_created_on_sqlite(tmp_path) -> None:
    """DDL 必须真的能建出来，而不只是元数据里列着。"""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'schema.db'}")

    async def _create() -> set[str]:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with engine.connect() as conn:
            return set(await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names()))

    try:
        created = asyncio.run(_create())
    finally:
        asyncio.run(engine.dispose())

    assert created >= SPEC_TABLES


def test_indexes_cover_spec_query_columns() -> None:
    """§11.4：project_id、task status、agent status、created_at 需要有索引。"""
    expectations = {
        # parent_task_id：Bot 后台子任务指回父对话任务，按父对话查/清理时需要索引
        # （迁移 b7d2e4f9c1a3 建了 ix_tasks_parent_task_id）。
        "tasks": ["project_id", "status", "parent_task_id"],
        "agents": ["status", "role"],
        "virtual_changes": ["project_id", "status"],
        "projects": ["organization_id"],
    }
    for table_name, columns in expectations.items():
        table = Base.metadata.tables[table_name]
        for column in columns:
            assert table.c[column].index, f"{table_name}.{column} 应有索引"


def test_tasks_table_covers_subtask_parent_column() -> None:
    """Bot 后台子任务：tasks.parent_task_id 指回父对话任务（迁移 b7d2e4f9c1a3）。

    普通任务为 NULL；父任务被清理时只置空关联（SET NULL），不级联删除子任务记录。
    """
    table = Base.metadata.tables["tasks"]
    column = table.c["parent_task_id"]
    assert column.nullable
    assert column.index
    assert {fk.target_fullname for fk in column.foreign_keys} == {"tasks.id"}
    assert {fk.ondelete for fk in column.foreign_keys} == {"SET NULL"}


def test_migrated_schema_matches_metadata(tmp_path) -> None:
    """把 Alembic 迁移跑出来的表结构与 Base.metadata 逐列对比。

    防止「模型加了列但忘了写迁移」这种本地测得过、线上炸掉的漂移：
    verify.sh 的迁移往返只验能不能跑，不验与模型是否一致。
    """
    db_path = tmp_path / "migrated.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(BACKEND_ROOT / "alembic.ini"),
            "-x",
            f"db_url={url}",
            "upgrade",
            "head",
        ],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    connection = sqlite3.connect(db_path)
    try:
        for table_name, table in Base.metadata.tables.items():
            rows = connection.execute(f'PRAGMA table_info("{table_name}")').fetchall()
            migrated_columns = {row[1] for row in rows}
            assert rows, f"迁移后的库里缺少元数据表 {table_name}"
            metadata_columns = {column.name for column in table.columns}
            assert migrated_columns == metadata_columns, (
                f"{table_name} 列不一致："
                f"仅元数据有 {metadata_columns - migrated_columns}，"
                f"仅迁移有 {migrated_columns - metadata_columns}"
            )
    finally:
        connection.close()
