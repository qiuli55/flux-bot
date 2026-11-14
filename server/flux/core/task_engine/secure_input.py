"""安全输入卡片的落库逻辑（REST 提交与组件动作回调共用）。

密钥明文只出现在请求里：这里把它交给密钥库加密保存，`task_messages` 只写一条
不含明文的「已安全存储」回执。每张卡片最多提交一次。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from flux.core.task_engine.repository import MessageDraft
from flux.enums import TaskMessageRole
from flux.errors import ConflictError, NotFoundError, ValidationError
from flux.logging import get_logger
from flux.models.task import Task, TaskMessage

if TYPE_CHECKING:  # pragma: no cover - 避免核心层与容器互相导入
    from flux.container import Container

logger = get_logger(__name__)


async def store_secure_input(
    container: Container,
    *,
    task: Task,
    card: TaskMessage,
    secret: str,
) -> dict[str, Any]:
    """校验卡片、去重、写密钥库与回执；返回 {"key": ..., "message": ...}。

    调用方负责确认任务归属与状态（REST 层用 get_owned / 终态检查）。
    """
    if card.kind != "secure_input":
        raise NotFoundError(
            "安全输入卡片不存在或不属于该任务",
            details={"task_id": str(task.id), "message_id": str(card.id)},
        )
    spec = (card.payload or {}).get("secure_input")
    if not isinstance(spec, dict) or not spec:
        raise ValidationError("卡片缺少密钥描述，无法保存", details={"message_id": str(card.id)})

    receipts = await container.task_repo.messages_of_kind(task.id, "secure_input_result")
    if any((item.payload or {}).get("message_id") == str(card.id) for item in receipts):
        raise ConflictError(
            "这张卡片的密钥已保存过，不会再写入一次",
            details={"message_id": str(card.id)},
        )

    value = secret.strip()
    if not value:
        raise ValidationError("密钥不能为空", details={"field": "value"})

    name = str(spec.get("name") or spec.get("title") or "API key")[:128]
    fields = {
        "provider": str(spec.get("provider") or ""),
        "base_url": str(spec.get("base_url") or ""),
        "models": [str(item) for item in spec.get("models") or []],
        "wire_api": str(spec.get("wire_api") or ""),
    }
    # 同名密钥视为「重新填一次」：覆盖凭据与用途，而不是报 409 让用户去密钥库手动合并。
    existing = next(
        (row for row in await container.key_vault.list_entries() if row.name == name), None
    )
    if existing is None:
        key = await container.key_vault.create(name=name, api_key=value, **fields)
    else:
        key = await container.key_vault.update(existing.id, api_key=value, **fields)

    stored = (
        await container.task_repo.add_messages(
            task.id,
            [
                MessageDraft(
                    role=TaskMessageRole.USER.value,
                    kind="secure_input_result",
                    content=f"已通过安全输入框保存密钥「{key.name}」，密钥不会出现在对话里。",
                    payload={
                        "message_id": str(card.id),
                        "key_id": str(key.id),
                        "name": key.name,
                        "provider": key.provider,
                        "key_hint": key.key_hint,
                        "status": "stored",
                    },
                )
            ],
        )
    )[0]
    await container.task_stream.publish(task.id, "user_message", {"message": stored.to_dict()})
    logger.info(
        "安全输入已保存 task=%s message=%s key=%s provider=%s",
        task.id,
        card.id,
        key.id,
        key.provider,
    )
    return {"key": key, "message": stored}


__all__ = ["store_secure_input"]
