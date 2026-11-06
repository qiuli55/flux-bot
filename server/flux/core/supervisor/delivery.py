"""Supervisor 的报告投递：订阅直通。

`create_report` 与 outbox 入队同事务完成，worker 领取 `supervisor.report_ready`
后调用这里：查该工单的订阅 → 按投递策略过滤 → 渲染纯文本 → 交给平台通道。

三条原则：

- 只投递"平台已经确认的事实"（报告由 dispatcher 按任务真实终态写入），
  不让子 Agent 自报成功；
- 同一份报告由 outbox 的 dedupe_key 保证只投一次；
- 投递失败抛异常交给 outbox 退避重试，超过上限落 dead 并保留错误，绝不静默丢弃。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

from flux.core.supervisor.channels import SupervisorChannel
from flux.core.supervisor.repository import SupervisorRepository
from flux.enums import ReportStatus
from flux.logging import get_logger
from flux.models.supervisor import SupervisorReport, SupervisorTicket

logger = get_logger(__name__)

__all__ = ["DELIVERY_MODES", "SupervisorNotifier", "should_deliver"]

#: 允许的投递策略（订阅上落库的 mode 字段）
DELIVERY_MODES = frozenset({"all", "terminal", "needs_input"})

#: 终态报告：这些状态视为"这一单已经收尾"
TERMINAL_STATUSES = frozenset(
    {
        ReportStatus.COMPLETED.value,
        ReportStatus.FAILED.value,
        ReportStatus.BLOCKED.value,
        ReportStatus.CANCELLED.value,
        ReportStatus.TIMED_OUT.value,
    }
)

_STATUS_LABELS = {
    ReportStatus.COMPLETED.value: "已完成",
    ReportStatus.FAILED.value: "失败",
    ReportStatus.BLOCKED.value: "受阻",
    ReportStatus.NEEDS_INPUT.value: "等待你的决策",
    ReportStatus.CANCELLED.value: "已取消",
    ReportStatus.TIMED_OUT.value: "超时",
}


def should_deliver(mode: str, status: str) -> bool:
    """投递策略：all / terminal / needs_input；未知策略 fail-closed。"""
    if mode == "all":
        return True
    if mode == "terminal":
        return status in TERMINAL_STATUSES
    if mode == "needs_input":
        return status == ReportStatus.NEEDS_INPUT.value
    return False


def render_report_text(ticket: SupervisorTicket, report: SupervisorReport) -> str:
    """把结构化报告渲染成平台可读的纯文本（不含任何原始外部内容）。"""
    label = _STATUS_LABELS.get(report.status, report.status)
    summary = (report.summary or "").strip() or "（无摘要）"
    return f"【Flux 助理】{ticket.title}\n状态：{label}\n结论：{summary}\n工单：{ticket.id}"


class SupervisorNotifier:
    """按订阅把报告投递到各平台线程。"""

    def __init__(
        self,
        repo: SupervisorRepository,
        channels: Mapping[str, SupervisorChannel],
    ) -> None:
        self._repo = repo
        self._channels = dict(channels)

    async def deliver_report(self, report_id: uuid.UUID) -> int:
        """投递一份报告；返回实际发送的条数。"""
        report = await self._repo.get_report(report_id)
        subscriptions = await self._repo.list_subscriptions(report.ticket_id)
        if not subscriptions:
            return 0
        ticket = await self._repo.get_ticket(report.ticket_id)
        text = render_report_text(ticket, report)

        sent = 0
        errors: list[str] = []
        for subscription in subscriptions:
            if not should_deliver(subscription.mode, report.status):
                continue
            channel = self._channels.get(subscription.source_key)
            if channel is None:
                errors.append(f"通道未装配：{subscription.source_key}")
                continue
            try:
                await channel.send_text(subscription.external_id, text)
            except Exception as exc:  # noqa: BLE001 - 单条失败不影响其它订阅
                errors.append(f"{subscription.source_key}: {exc}")
                logger.warning(
                    "Supervisor 报告投递失败 report=%s subscription=%s: %s",
                    report.id,
                    subscription.id,
                    exc,
                )
            else:
                sent += 1
        if errors:
            # 全部失败才抛错交给 outbox 重试；部分成功时只记日志，避免已送达的线程
            # 在下一轮重试里再收到一遍。
            if sent == 0:
                raise RuntimeError("; ".join(errors)[:2000])
            logger.warning(
                "Supervisor 报告部分投递失败 report=%s 已发送=%s 错误=%s",
                report.id,
                sent,
                "; ".join(errors)[:1000],
            )
        return sent
