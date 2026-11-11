"""外部连接器 API（账户页「连接器」分组的后端）。

两类能力共用 `/connectors` 前缀：
- `execute`：Agent 经注册表执行已登记连接器（主规格 §12.7，历史能力）；
- `github/*`：用户把自己的 GitHub 账号接到当前本地账号上（设备码 OAuth）。

账号隔离：App 每个请求带 `X-Flux-Account: <本地账号 id>`，服务端校验格式后作为
`account_key`；连接记录按它唯一隔离。当前 App 的账号是本地账号，所以这是客户端声明；
将来接真实登录后换成令牌 subject，接口形状不变。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from flux.api.deps import get_container
from flux.api.response import ok
from flux.container import Container
from flux.core.github.service import ACCOUNT_HEADER, normalize_account_key
from flux.enums import Capability
from flux.schemas.api import ConnectorExecuteRequest

router = APIRouter(prefix="/connectors", tags=["connectors"])


def _account_key(request: Request) -> str:
    return normalize_account_key(request.headers.get(ACCOUNT_HEADER))


@router.get("")
async def list_connectors(
    request: Request, container: Container = Depends(get_container)
) -> dict[str, object]:
    """连接器列表。以后新增的外部连接器都从这里挂，App 只渲染这一份清单。"""
    account_key = _account_key(request)
    return ok([await container.github.public_state(account_key)])


@router.post("/github/login/start")
async def start_github_login(
    request: Request, container: Container = Depends(get_container)
) -> dict[str, object]:
    account_key = _account_key(request)
    return ok(await container.github.start_login(account_key))


@router.get("/github/login/{flow_id}")
async def poll_github_login(
    flow_id: str, request: Request, container: Container = Depends(get_container)
) -> dict[str, object]:
    account_key = _account_key(request)
    return ok(await container.github.poll_login(flow_id, account_key))


@router.delete("/github")
async def disconnect_github(
    request: Request, container: Container = Depends(get_container)
) -> dict[str, object]:
    account_key = _account_key(request)
    return ok(await container.github.disconnect(account_key))


@router.post("/execute")
async def execute_connector(
    payload: ConnectorExecuteRequest, container: Container = Depends(get_container)
) -> dict[str, object]:
    granted: frozenset[Capability] = frozenset()
    if payload.agent_id:
        # Agent 不存在会抛 NotFoundError（404）
        granted = container.agents.get(payload.agent_id).spec.permissions
    entry = await container.connectors.execute(
        payload.connector,
        payload.action,
        payload.parameters,
        agent_id=payload.agent_id,
        granted=granted,
    )
    return ok(entry)
