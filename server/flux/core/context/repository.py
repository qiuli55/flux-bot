"""context_steps 的读写入口（FLUX_CONTEXT_MECHANISM §3.3 / §7.2）。

step 是只追加的：这条日志只增不改，压缩、检索、原子事实都以它为准。
`seq_in_turn` 用"读改写"在 turn 内分配（先例见 TerminalRepository.append_event）；
并发安全由单进程装配保证——与 Apply 的进程内锁同一取舍。

`append` 同时写反向索引词条（§7.2），**在同一个事务里**：分成两步就会出现
"step 落了库、索引没落"的静默漂移，而反向索引正是"手里只有线索"时的唯一入口，坏了不易发现。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.core.context.index import extract_tokens, normalize_token
from flux.enums import ContextTokenKind, StepKind, StepStatus
from flux.errors import NotFoundError
from flux.models.context import ContextStep, ContextStepToken


class ContextStepRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def append(
        self,
        *,
        run_id: str,
        turn_id: str,
        kind: StepKind,
        status: StepStatus,
        agent_fp: str = "",
        tool: str | None = None,
        argv_digest: str | None = None,
        target_ref: str | None = None,
        exit_code: int | None = None,
        result_sha256: str | None = None,
        result_bytes: int | None = None,
        result_head: str | None = None,
        facts: list[dict[str, Any]] | None = None,
        unverified: bool = False,
    ) -> ContextStep:
        """追加一条 step，分配它在本 turn 内的序号，并落好反向索引词条。"""
        async with self._session_factory() as db:
            next_seq = await db.scalar(
                select(func.coalesce(func.max(ContextStep.seq_in_turn), -1) + 1).where(
                    ContextStep.turn_id == turn_id
                )
            )
            step = ContextStep(
                run_id=run_id,
                turn_id=turn_id,
                seq_in_turn=int(next_seq or 0),
                kind=kind.value,
                status=status.value,
                agent_fp=agent_fp,
                tool=tool,
                argv_digest=argv_digest,
                target_ref=target_ref,
                exit_code=exit_code,
                result_sha256=result_sha256,
                result_bytes=result_bytes,
                result_head=result_head,
                facts=facts,
                unverified=unverified,
            )
            db.add(step)
            # flush 拿到自增 step_id，词条才挂得上去；与 step 同一个事务提交，不会半途漂移
            await db.flush()
            for token_kind, token in extract_tokens(step):
                db.add(
                    ContextStepToken(
                        step_id=step.id,
                        run_id=run_id,
                        turn_id=turn_id,
                        kind=token_kind.value,
                        token=token,
                    )
                )
            await db.commit()
            return step

    async def get(self, step_id: int) -> ContextStep:
        async with self._session_factory() as db:
            step = await db.get(ContextStep, step_id)
            if step is None:
                raise NotFoundError(f"step {step_id} 不存在", details={"step_id": step_id})
            return step

    async def list_by_turn(self, turn_id: str) -> list[ContextStep]:
        """一个 turn 内的全部 step，按 `seq_in_turn` 排序（目录行的天然顺序）。"""
        statement = (
            select(ContextStep)
            .where(ContextStep.turn_id == turn_id)
            .order_by(ContextStep.seq_in_turn, ContextStep.id)
        )
        async with self._session_factory() as db:
            return list(await db.scalars(statement))

    async def list_by_run(self, run_id: str, *, limit: int = 500) -> list[ContextStep]:
        """一个 run 内的全部 step（归属单元），按发生顺序。"""
        statement = (
            select(ContextStep)
            .where(ContextStep.run_id == run_id)
            .order_by(ContextStep.id)
            .limit(limit)
        )
        async with self._session_factory() as db:
            return list(await db.scalars(statement))

    async def lookup(
        self,
        *,
        token: str | None = None,
        kind: ContextTokenKind | None = None,
        run_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
    ) -> list[int]:
        """反向索引（§7.2）：按线索找 `step_id`，返回**最近** `limit` 条、按时间升序。

        线索即 `token`（文件 / 命令 / URL / 错误关键字，按 `kind` 归一类）；
        不传 `token` 就是第五类「按时间窗」查询，直接扫 `context_steps.created_at`。

        取"最近 N 条"而非"最早 N 条"：接管 agent 问的多是"刚才那次"，且不取件时
        最新一条的信息量最大；返回时再翻成升序，方便直接喂给目录行渲染。

        只给 `step_id` 而不是整行 step：这一层是索引，取件是 `fetch` 的事（§7.1）。
        """
        statement = select(ContextStep.id).distinct()
        if token is not None:
            normalized = normalize_token(kind or ContextTokenKind.FILE, token)
            if not normalized:
                return []
            statement = statement.join(
                ContextStepToken, ContextStepToken.step_id == ContextStep.id
            ).where(ContextStepToken.token == normalized)
            if kind is not None:
                statement = statement.where(ContextStepToken.kind == kind.value)

        if run_id is not None:
            statement = statement.where(ContextStep.run_id == run_id)
        if since is not None:
            statement = statement.where(ContextStep.created_at >= since)
        if until is not None:
            statement = statement.where(ContextStep.created_at <= until)

        statement = statement.order_by(ContextStep.id.desc()).limit(limit)
        async with self._session_factory() as db:
            newest_first = list(await db.scalars(statement))
        return list(reversed(newest_first))

    async def tokens_of(self, step_id: int) -> list[ContextStepToken]:
        """取一条 step 的全部索引词条（排查"为什么查不到"时用）。"""
        statement = (
            select(ContextStepToken)
            .where(ContextStepToken.step_id == step_id)
            .order_by(ContextStepToken.kind, ContextStepToken.token)
        )
        async with self._session_factory() as db:
            return list(await db.scalars(statement))
