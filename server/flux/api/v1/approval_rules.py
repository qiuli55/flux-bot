"""全局审批规则 API（App 设置页使用）。

产品默认是**零审批**：这里不提供任何「默认自动批准」入口，只让登录用户管理
自己的规则。规则按 Flux 云用户隔离，对该用户名下所有 Bot 生效；响应里始终
带 `created_by_kind` / `created_by_bot_id`，前端因此能显示「这条规则是谁加的」。

跨用户读取 / 修改 / 删除一律按不存在处理（404），不泄露 id 是否存在。
用户与 Bot 都可以新增规则；Bot 修改 / 删除前必须在对话里挂审批卡二次确认，
确认通过后才会调用本 API 的更新 / 删除逻辑。本 API 需要用户登录身份。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from flux.api.account_scope import account_key_for_request
from flux.api.deps import get_container
from flux.api.response import ok
from flux.container import Container
from flux.schemas.api import ApprovalRuleCreateRequest, ApprovalRuleUpdateRequest

router = APIRouter(prefix="/approval-rules", tags=["approval-rules"])


@router.get("")
async def list_approval_rules(
    request: Request,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """列出当前用户的全部规则（含已停用项，按创建时间升序）。"""
    cloud_user_id = account_key_for_request(request, container.settings)
    rows = await container.approval_rules.list(cloud_user_id, enabled_only=False)
    return ok([row.to_dict() for row in rows], metadata={"count": len(rows)})


@router.post("")
async def create_approval_rule(
    payload: ApprovalRuleCreateRequest,
    request: Request,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """新增一条用户规则；created_by_kind 固定为 user。"""
    cloud_user_id = account_key_for_request(request, container.settings)
    row = await container.approval_rules.create(
        cloud_user_id=cloud_user_id,
        rule_type=payload.type,
        params=payload.params,
        description=payload.description,
        enabled=payload.enabled,
        created_by_kind="user",
    )
    return ok(row.to_dict())


@router.get("/{rule_id}")
async def get_approval_rule(
    rule_id: str,
    request: Request,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """查看自己的一条规则；别人的规则返回 404。"""
    cloud_user_id = account_key_for_request(request, container.settings)
    row = await container.approval_rules.get(rule_id, cloud_user_id=cloud_user_id)
    return ok(row.to_dict())


@router.patch("/{rule_id}")
async def update_approval_rule(
    rule_id: str,
    payload: ApprovalRuleUpdateRequest,
    request: Request,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """修改自己的规则（说明 / 参数 / 启停 / 类型）；参数会重新校验。"""
    cloud_user_id = account_key_for_request(request, container.settings)
    data = payload.model_dump(exclude_unset=True)
    row = await container.approval_rules.update(
        rule_id,
        cloud_user_id=cloud_user_id,
        rule_type=data.get("type"),
        params=data.get("params"),
        description=data.get("description"),
        enabled=data.get("enabled"),
    )
    return ok(row.to_dict())


@router.delete("/{rule_id}")
async def delete_approval_rule(
    rule_id: str,
    request: Request,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """删除自己的规则；别人的规则按不存在处理（404）。"""
    cloud_user_id = account_key_for_request(request, container.settings)
    deleted = await container.approval_rules.delete(rule_id, cloud_user_id=cloud_user_id)
    return ok({"deleted": True, "rule_id": deleted["id"]})


__all__ = ["router"]
