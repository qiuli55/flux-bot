"""外部连接器 API（账户页「连接器」分组的后端）。

两类能力共用 `/connectors` 前缀：
- `execute`：Agent 经注册表执行已登记连接器（主规格 §12.7，历史能力）；
- `github/*`：用户把自己的 GitHub 账号接到当前本地账号上（设备码 OAuth）；
- `catalog` / `{id}/install` / `{id}`：账号侧连接器框架（目录、安装、卸载）。

账号隔离：Phase 2 起账号标识就是登录人（云 JWT 的 `sub`，或
`FLUX_OWNER_CLOUD_USER_ID` 主用户），由 `account_key_for_request` 统一解析；
客户端自报的 `X-Flux-Account` 不再作为身份依据。分不清是谁时 fail-closed
（403 `cloud_login_required`），不会写 `'default'` 公共记录。

注意：`execute` 走的是 Agent 执行侧注册表（`Container.connectors`），而
catalog / install / uninstall 走账号侧框架（`Container.connector_framework`），
两者刻意分开，互不改变对方的契约。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from flux.api.account_scope import account_key_for_request
from flux.api.deps import get_container
from flux.api.response import ok
from flux.container import Container
from flux.enums import Capability
from flux.schemas.api import (
    ConnectorExecuteRequest,
    ConnectorInstallRequest,
    EmailSendRequest,
)

router = APIRouter(prefix="/connectors", tags=["connectors"])


def _account_key(request: Request, container: Container) -> str:
    """账号标识 = 当前登录人；不再读客户端自报的 X-Flux-Account。"""
    return account_key_for_request(request, container.settings)


@router.get("")
async def list_connectors(
    request: Request, container: Container = Depends(get_container)
) -> dict[str, object]:
    """连接器列表。以后新增的外部连接器都从这里挂，App 只渲染这一份清单。"""
    account_key = _account_key(request, container)
    return ok(await container.connector_framework.list_states(account_key))


@router.get("/catalog")
async def list_connector_catalog(
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """可安装连接器目录（静态定义，不含任何账号数据）。"""
    return ok(await container.connector_framework.list_catalog())


@router.post("/github/login/start")
async def start_github_login(
    request: Request, container: Container = Depends(get_container)
) -> dict[str, object]:
    account_key = _account_key(request, container)
    return ok(await container.github.start_login(account_key))


@router.get("/github/login/{flow_id}")
async def poll_github_login(
    flow_id: str, request: Request, container: Container = Depends(get_container)
) -> dict[str, object]:
    account_key = _account_key(request, container)
    return ok(await container.github.poll_login(flow_id, account_key))


@router.delete("/github")
async def disconnect_github(
    request: Request, container: Container = Depends(get_container)
) -> dict[str, object]:
    account_key = _account_key(request, container)
    return ok(await container.github.disconnect(account_key))


# --- Email（IMAP 收信 / SMTP 发信）----------------------------------------
# 这些动作都要求当前账号已安装 email 连接器：未安装 404，凭据解密失败 503，
# IMAP/SMTP 上游失败 502。响应里永远不出现密码；正文只在单封邮件接口返回。


@router.get("/email/messages")
async def list_email_messages(
    request: Request,
    mailbox: str = Query(default="INBOX", max_length=255),
    limit: int = Query(default=20, ge=1, le=50),
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """列出邮件头（不含正文），最新的在前。"""
    account_key = _account_key(request, container)
    messages = await container.email.list_messages(account_key, mailbox=mailbox, limit=limit)
    return ok({"messages": messages, "mailbox": mailbox, "count": len(messages)})


@router.get("/email/messages/{message_id}")
async def get_email_message(
    message_id: str,
    request: Request,
    mailbox: str = Query(default="INBOX", max_length=255),
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """读取一封邮件；优先 text/plain，只有 HTML 时返回 HTML 正文。"""
    account_key = _account_key(request, container)
    message = await container.email.get_message(account_key, message_id, mailbox=mailbox)
    return ok(message)


@router.post("/email/send")
async def send_email_message(
    payload: EmailSendRequest,
    request: Request,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """通过 SMTP 发送一封邮件（不会自动发送，也不会创建草稿）。"""
    account_key = _account_key(request, container)
    result = await container.email.send_message(
        account_key,
        to=payload.to,
        cc=payload.cc,
        subject=payload.subject,
        body=payload.body,
        content_type=payload.content_type,
    )
    return ok(result)


@router.post("/email/test")
async def test_email_connection(
    request: Request,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """分别验证 IMAP / SMTP 连接与登录，不发送真实邮件。"""
    account_key = _account_key(request, container)
    return ok(await container.email.test_connection(account_key))


@router.post("/email/poll")
async def poll_email_messages(
    request: Request,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """立即跑一轮邮箱轮询（供测试与未来的「立即检查」按钮）。

    只发布新邮件对应的 email.received 事件，不发送任何邮件；响应里不含正文与密码。
    后台轮询由 main.py lifespan 按 FLUX_EMAIL_TRIGGER_* 配置自动起停。
    """
    account_key = _account_key(request, container)
    return ok(await container.email_trigger.poll_account(account_key))


@router.post("/{connector_id}/install")
async def install_connector(
    connector_id: str,
    payload: ConnectorInstallRequest,
    request: Request,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """安装一个账号侧连接器；凭据只进加密存储，响应里不出现明文。"""
    account_key = _account_key(request, container)
    state = await container.connector_framework.install(
        account_key,
        connector_id,
        auth_type=payload.auth_type,
        api_key=payload.api_key,
        scopes=payload.scopes,
        settings=payload.settings,
    )
    return ok(state)


@router.delete("/{connector_id}")
async def uninstall_connector(
    connector_id: str,
    request: Request,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """卸载连接器；只删除当前账号的安装与凭据（`/github` 是它的兼容别名）。"""
    account_key = _account_key(request, container)
    return ok(await container.connector_framework.uninstall(account_key, connector_id))


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
