"""Bot IM 频道状态（只读）。

与频道回调路由不同，这个接口挂在 `require_rest_auth` 之下：它暴露的是频道配置
完整性、收发时间与近期错误，属于运维信息，不能给未登录的外部请求看。

安全边界：只返回环境变量**名字**与布尔值，绝不返回变量值；错误串已在
`ChannelActivityRegistry` / 出站草稿落库前压平截断，不含消息正文或密文。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from flux.api.deps import get_container
from flux.api.response import ok
from flux.container import Container
from flux.core.channels.status import collect_channel_statuses

router = APIRouter(prefix="/channels", tags=["channels"])


@router.get("/status", summary="Bot IM 频道配置与收发状态（只读）")
async def channels_status(container: Container = Depends(get_container)) -> dict[str, object]:
    statuses = await collect_channel_statuses(
        container.settings,
        container.channel_activity,
        container.outbound_drafts,
    )
    return ok(
        statuses,
        metadata={
            "count": len(statuses),
            # 接收时间与入站错误是进程内登记，重启后清空；发送记录来自 outbound_drafts。
            "receive_history": "process-local",
        },
    )


__all__ = ["router"]
