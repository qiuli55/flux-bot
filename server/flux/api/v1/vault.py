"""用户保险库 API（站点登录 / 支付卡 / 联系人）。

与 `/api/v1/keys`（模型供应商密钥）分开挂载：这里存的是用户自己的账号信息。
所有响应默认掩码——密码不回、卡号只回 last4；只有登录密码有一个显式
`POST /vault/logins/{id}/reveal`，返回一次并记审计日志（不含密码）。支付卡
不提供任何返回 PAN / CVV 的接口。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from flux.api.deps import get_container
from flux.api.response import ok
from flux.container import Container
from flux.schemas.api import (
    VaultCardCreateRequest,
    VaultCardUpdateRequest,
    VaultContactCreateRequest,
    VaultContactUpdateRequest,
    VaultLoginCreateRequest,
    VaultLoginUpdateRequest,
)

router = APIRouter(prefix="/vault", tags=["vault"])


# --- 站点登录 ---


@router.get("/logins")
async def list_logins(container: Container = Depends(get_container)) -> dict[str, object]:
    """列出登录条目；响应不含密码或密文。"""
    rows = await container.vault.list_logins()
    items = [row.to_dict() for row in rows]
    return ok(items, metadata={"count": len(items)})


@router.post("/logins")
async def create_login(
    payload: VaultLoginCreateRequest, container: Container = Depends(get_container)
) -> dict[str, object]:
    """新增登录条目；密码加密落库，响应只回展示字段。"""
    row = await container.vault.create_login(
        site=payload.site,
        username=payload.username,
        password=payload.password,
        notes=payload.notes,
    )
    return ok(row.to_dict())


@router.get("/logins/{login_id}")
async def get_login(
    login_id: str, container: Container = Depends(get_container)
) -> dict[str, object]:
    row = await container.vault.get_login(login_id)
    return ok(row.to_dict())


@router.patch("/logins/{login_id}")
async def update_login(
    login_id: str,
    payload: VaultLoginUpdateRequest,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """局部更新；未出现 / null 的字段不变，密码留空也表示不改。"""
    data = {
        key: value
        for key, value in payload.model_dump(exclude_unset=True).items()
        if value is not None
    }
    row = await container.vault.update_login(login_id, **data)
    return ok(row.to_dict())


@router.delete("/logins/{login_id}")
async def delete_login(
    login_id: str, container: Container = Depends(get_container)
) -> dict[str, object]:
    await container.vault.delete_login(login_id)
    return ok({"deleted": True, "login_id": login_id})


@router.post("/logins/{login_id}/reveal")
async def reveal_login(
    login_id: str, container: Container = Depends(get_container)
) -> dict[str, object]:
    """显式取回一次密码明文；审计日志只记条目 id。"""
    password = await container.vault.reveal_login(login_id)
    return ok({"password": password})


# --- 支付卡 ---


@router.get("/cards")
async def list_cards(container: Container = Depends(get_container)) -> dict[str, object]:
    """列出支付卡；只回 last4 / 品牌 / 有效期，绝不回 PAN 或 CVV。"""
    rows = await container.vault.list_cards()
    items = [row.to_dict() for row in rows]
    return ok(items, metadata={"count": len(items)})


@router.post("/cards")
async def create_card(
    payload: VaultCardCreateRequest, container: Container = Depends(get_container)
) -> dict[str, object]:
    """新增支付卡；完整卡号 / CVV 只用于生成密文，明文立即丢弃。"""
    row = await container.vault.create_card(
        label=payload.label,
        brand=payload.brand,
        pan=payload.pan,
        cvv=payload.cvv,
        last4=payload.last4,
        exp_month=payload.exp_month,
        exp_year=payload.exp_year,
        notes=payload.notes,
    )
    return ok(row.to_dict())


@router.get("/cards/{card_id}")
async def get_card(
    card_id: str, container: Container = Depends(get_container)
) -> dict[str, object]:
    row = await container.vault.get_card(card_id)
    return ok(row.to_dict())


@router.patch("/cards/{card_id}")
async def update_card(
    card_id: str,
    payload: VaultCardUpdateRequest,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """局部更新；未出现 / null 的字段不变。不存在 reveal 接口。"""
    data = {
        key: value
        for key, value in payload.model_dump(exclude_unset=True).items()
        if value is not None
    }
    row = await container.vault.update_card(card_id, **data)
    return ok(row.to_dict())


@router.delete("/cards/{card_id}")
async def delete_card(
    card_id: str, container: Container = Depends(get_container)
) -> dict[str, object]:
    await container.vault.delete_card(card_id)
    return ok({"deleted": True, "card_id": card_id})


# --- 联系人 ---


@router.get("/contacts")
async def list_contacts(container: Container = Depends(get_container)) -> dict[str, object]:
    rows = await container.vault.list_contacts()
    items = [row.to_dict() for row in rows]
    return ok(items, metadata={"count": len(items)})


@router.post("/contacts")
async def create_contact(
    payload: VaultContactCreateRequest, container: Container = Depends(get_container)
) -> dict[str, object]:
    row = await container.vault.create_contact(
        name=payload.name,
        phones=payload.phones,
        emails=payload.emails,
        notes=payload.notes,
    )
    return ok(row.to_dict())


@router.get("/contacts/{contact_id}")
async def get_contact(
    contact_id: str, container: Container = Depends(get_container)
) -> dict[str, object]:
    row = await container.vault.get_contact(contact_id)
    return ok(row.to_dict())


@router.patch("/contacts/{contact_id}")
async def update_contact(
    contact_id: str,
    payload: VaultContactUpdateRequest,
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """局部更新；未出现 / null 的字段不变。"""
    data = {
        key: value
        for key, value in payload.model_dump(exclude_unset=True).items()
        if value is not None
    }
    row = await container.vault.update_contact(contact_id, **data)
    return ok(row.to_dict())


@router.delete("/contacts/{contact_id}")
async def delete_contact(
    contact_id: str, container: Container = Depends(get_container)
) -> dict[str, object]:
    await container.vault.delete_contact(contact_id)
    return ok({"deleted": True, "contact_id": contact_id})


__all__ = ["router"]
