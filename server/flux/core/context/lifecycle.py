"""会话级联删除（FLUX_CONTEXT_MECHANISM §8.2 / 不变量 I7）。

I2 说"系统永不主动丢弃"，**唯一的例外**是用户删掉整个会话 —— 删就删干净，
漏一张表就是事故。§8.2 的清单落到本仓库的实际位置如下（这张对照表就是"删全了没"的自查依据）：

| §8.2 清单项 | 本仓库落点 |
| --- | --- |
| 段 / turn 表 | 无独立表：`turn_id` 只是 `context_steps` / `context_step_tokens` 上的分区键 |
| step 事件表 | `context_steps`（按 `turn_id` / `run_id` 删） |
| 目录行（若物化） | 未物化：`catalog_line` 是 step 的即时投影，随 step 一起消失 |
| 反向索引（五类） | `context_step_tokens`（五类只是 `kind` 的取值，同表同删） |
| 压缩产物 | `context_blobs`（原文 + 编码都在这一行） |
| 验证账本（§4.4 派生） | `context_verifications`（按 `turn_id` / `run_id` 删） |
| 快照 / 派生视图缓存 | 无：目录行与打包结果都不落盘，没有缓存要清 |
| 脱敏映射表 | `context_redactions`（按 `turn_id` / `run_id` 删；它存的正是秘密，最易漏） |

**为什么不能直接 drop 分区**：§8.2 的实现建议是"按会话分区、级联删除变一次 drop"。
`context_blobs` 却必须**全局**去重（§8.4：同一份文件 / 日志只存一次），所以 blob 的删除
天然跨分区 —— 无条件按 turn 删会把别的 turn 还在引用的原文删掉。故删除分两步：
先删 step 与反向索引，再对本次涉及的 `sha256` 做一次引用复查，只在**没有任何 step 再引用**
时才删 blob 行（等价于全局 CAS 的引用计数 GC）。分区键（`turn_id` / `run_id`）都建了索引，
第一步的扫删因此不是全表扫。

删除必须**幂等**（I7）：删不存在的 turn / run 不报错，返回全 0。
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from flux.models.context import (
    ContextBlob,
    ContextRedaction,
    ContextStep,
    ContextStepToken,
    ContextVerification,
)


class ContextLifecycle:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def delete_turn(self, turn_id: str) -> dict[str, int]:
        """删掉一个 turn 的全部痕迹（§8.1：`turn_id` 是删除单元）。

        返回被删行数，供调用方核对与记日志；幂等，重复调用返回全 0。
        """
        return await self._cascade(
            token_filter=ContextStepToken.turn_id == turn_id,
            step_filter=ContextStep.turn_id == turn_id,
            verification_filter=ContextVerification.turn_id == turn_id,
            redaction_filter=ContextRedaction.turn_id == turn_id,
        )

    async def delete_run(self, run_id: str) -> dict[str, int]:
        """删掉一个 run 的全部痕迹（归属单元，通常等于"删掉这次会话"）。"""
        return await self._cascade(
            token_filter=ContextStepToken.run_id == run_id,
            step_filter=ContextStep.run_id == run_id,
            verification_filter=ContextVerification.run_id == run_id,
            redaction_filter=ContextRedaction.run_id == run_id,
        )

    async def _cascade(
        self,
        *,
        token_filter: ColumnElement[bool],
        step_filter: ColumnElement[bool],
        verification_filter: ColumnElement[bool],
        redaction_filter: ColumnElement[bool],
    ) -> dict[str, int]:
        """先删索引、账本与 step，再 GC 掉因此失去引用的 blob；全程一个事务。"""
        async with self._session_factory() as db:
            # 先把要删的 step 引用到的原文 hash 记下来：step 一删就问不出来源了
            candidates = set(
                await db.scalars(
                    select(ContextStep.result_sha256).where(
                        step_filter, ContextStep.result_sha256.is_not(None)
                    )
                )
            )
            tokens = (await db.execute(delete(ContextStepToken).where(token_filter))).rowcount
            verifications = (
                await db.execute(delete(ContextVerification).where(verification_filter))
            ).rowcount
            redactions = (
                await db.execute(delete(ContextRedaction).where(redaction_filter))
            ).rowcount
            steps = (await db.execute(delete(ContextStep).where(step_filter))).rowcount
            blobs = await self._gc_blobs(db, candidates)
            await db.commit()
            return {
                "steps": steps,
                "tokens": tokens,
                "verifications": verifications,
                "redactions": redactions,
                "blobs": blobs,
            }

    async def _gc_blobs(self, db: AsyncSession, shas: set[str | None]) -> int:
        """删除不再被任何 step 引用的 blob（去重后可能被别的 turn 共享，必须复查）。"""
        deleted = 0
        for sha in sorted(sha for sha in shas if sha):
            referenced = await db.scalar(
                select(func.count())
                .select_from(ContextStep)
                .where(ContextStep.result_sha256 == sha)
            )
            if referenced:
                continue
            deleted += (
                await db.execute(delete(ContextBlob).where(ContextBlob.sha256 == sha))
            ).rowcount
        return deleted
