"""Agent API（主规格 §12.3）：Agent 档案的注册与查询。

Flux 不执行 Agent（目标架构 §1）——这里只登记 agent 的身份与权限边界；
执行由各 agent 自己的运行时完成，能力（上下文 / 工具 / Skill）经 Flux MCP 面获得。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from flux.api.deps import get_container
from flux.api.response import ok
from flux.container import Container
from flux.core.agent_runtime.manager import AgentHandle, AgentSpec
from flux.core.agent_runtime.model_catalog import (
    KeyEntry,
    default_model_for_runtime,
    models_for_runtime,
    vault_models_for_runtime,
)
from flux.core.ide_completion import ActiveAgentSelection
from flux.errors import BadRequestError
from flux.schemas.api import ActiveAgentRequest, AgentCreateRequest, AgentTokenIssueRequest

router = APIRouter(prefix="/agents", tags=["agents"])


def _agent_payload(
    handle: AgentHandle,
    container: Container,
    key_entries: tuple[KeyEntry, ...] = (),
) -> dict[str, object]:
    """档案响应：附上该 Agent runtime 的可用原生模型与密钥库模型。"""
    data = handle.to_dict()
    spec = dict(data.get("spec") or {})
    runtime = str(spec.get("runtime") or "")
    spec["models"] = models_for_runtime(container.settings, runtime)
    spec["model"] = default_model_for_runtime(container.settings, runtime)
    spec["key_models"] = [
        option.to_dict() for option in vault_models_for_runtime(key_entries, runtime)
    ]
    data["spec"] = spec
    return data


@router.post("")
async def create_agent(
    payload: AgentCreateRequest, container: Container = Depends(get_container)
) -> dict[str, object]:
    spec = AgentSpec(
        name=payload.name,
        role=payload.role,
        description=payload.description,
        skills=tuple(payload.skills),
        tools=tuple(payload.tools),
        permissions=frozenset(payload.permissions),
        runtime=payload.runtime,
    )
    handle: AgentHandle = await container.agents.create(spec)
    return ok(handle.to_dict())


@router.get("")
async def list_agents(container: Container = Depends(get_container)) -> dict[str, object]:
    handles = container.agents.list()
    key_entries = tuple(await container.key_vault.catalog_entries())
    items = [_agent_payload(h, container, key_entries) for h in handles]
    return ok(items, metadata={"count": len(items)})


# --- 当前 Agent 选择（AI_COMPLETION_SPEC §5.3）---
# 注意：这两条必须声明在 `GET /{agent_id}` 之前，否则 "active" 会被当成 agent_id。
# 右栏选了什么，行内补全的 `follow-agent` 就用什么（agent + model + key）。


@router.get("/active")
async def get_active_agent(
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """当前生效的 Agent 选择，以及它会解析到的实际模型通道。"""

    return ok(await container.completion_resolver.describe_active())


@router.put("/active")
async def put_active_agent(
    payload: ActiveAgentRequest, container: Container = Depends(get_container)
) -> dict[str, object]:
    """保存右栏的 Agent / 模型选择（`agent_id` 留空表示清空选择）。"""

    selection = ActiveAgentSelection(
        agent_id=payload.agent_id, model=payload.model, key_id=payload.key_id
    )
    if selection.agent_id:
        # 档案不存在时 get() 抛 NotFoundError，先于落盘拦截
        container.agents.get(selection.agent_id)
    if selection.key_id:
        row = await container.key_vault.get(selection.key_id)
        if not row.enabled:
            raise BadRequestError(f"密钥 {row.name!r} 已停用", details={"key_id": str(row.id)})
        models = [str(item).strip() for item in (row.models or []) if str(item).strip()]
        if selection.model and models and selection.model not in models:
            raise BadRequestError(
                f"密钥 {row.name!r} 没有登记模型 {selection.model}",
                details={"key_id": str(row.id), "model": selection.model, "allowed": models},
            )
    container.completion_prefs.set_active_agent(selection)
    return ok(await container.completion_resolver.describe_active(), metadata={"saved": True})


@router.get("/{agent_id}")
async def get_agent(
    agent_id: str, container: Container = Depends(get_container)
) -> dict[str, object]:
    key_entries = tuple(await container.key_vault.catalog_entries())
    return ok(_agent_payload(container.agents.get(agent_id), container, key_entries))


# --- MCP 接入令牌（目标架构 §3.2）---
# 注意：M0 的 REST 面整体尚无用户鉴权（与 /api/v1/* 其它接口同一现状），
# 因此本组接口的信任前提是"后端只对内网/本机开放"。桌面端用户鉴权落地后，
# 签发令牌必须再收一道 owner 校验——这里不预先造一套假的权限检查。


@router.post("/{agent_id}/tokens")
async def issue_agent_token(
    agent_id: str,
    payload: AgentTokenIssueRequest,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """签发一枚接入令牌。明文只在本响应里出现一次，请立即写入 agent 侧配置。

    P3-16：路径里的 `agent_id` 必须是 Agent Registry 的 canonical UUID
    （先 `POST /agents` 建档拿到的那个 id）——名字只是 display_name，不能用来签令牌。
    """
    token, raw = await container.agent_tokens.issue(
        agent_id=agent_id, scopes=payload.scopes, label=payload.label
    )
    return ok(
        {**token.to_dict(), "token": raw},
        metadata={"notice": "明文令牌仅此一次返回，请立即写入 agent 配置；服务端只存哈希"},
    )


@router.get("/{agent_id}/tokens")
async def list_agent_tokens(
    agent_id: str, container: Container = Depends(get_container)
) -> dict[str, object]:
    """列出该 agent 的令牌（只有元信息，取不回明文）。"""
    tokens = await container.agent_tokens.list(agent_id=agent_id)
    return ok([token.to_dict() for token in tokens], metadata={"count": len(tokens)})


@router.delete("/{agent_id}/tokens/{token_id}")
async def revoke_agent_token(
    agent_id: str, token_id: str, container: Container = Depends(get_container)
) -> dict[str, object]:
    """撤销令牌：下一个 MCP 请求立即失效（鉴权不做缓存）。"""
    token = await container.agent_tokens.revoke(token_id)
    return ok(token.to_dict())


__all__ = ["router"]
