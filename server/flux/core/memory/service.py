"""三层记忆服务（批次② §4.2 / §4.3）。

- User Memory：跨 Workspace，只有用户确认的写入入口（REST），可查可删；
- Environment Memory：全局、平台代码维护（种子按 key 幂等 upsert），优先于项目上下文；
- Project Memory：底座是 Project Brain（`project_memory` 表），本服务只读，写入走
  `ProjectBrain.write` / 扫描沉淀。

写入必须是受控路径：**Agent 没有任何直写接口**（MCP 面只有只读的 `memory.recall`）。
所有写入先过密钥红线（`secrets.ensure_no_secret`），再按策略设 TTL、清过期、裁容量。

`recall()` 是三层的统一只读出口：先 Environment（平台规则优先），再 User（跨 Workspace），
最后 Project（仅当前项目）。跨 Workspace 可见的只有 Environment 与 User 两层。
"""

from __future__ import annotations

import uuid
from contextlib import suppress
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from flux.core.memory.policies import (
    ENVIRONMENT_MEMORY_SEED,
    MAX_CONTENT_CHARS,
    MEMORY_POLICIES,
    RECALL_NOTE,
    MemoryPolicy,
)
from flux.core.memory.repository import MemoryRepository
from flux.core.memory.secrets import ensure_no_secret
from flux.enums import MemoryLayer
from flux.errors import BadRequestError, PermissionDeniedError, ValidationError
from flux.models.memory import MemoryEntry

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注，避免与 project_brain 循环导入
    from flux.core.project_brain.service import ProjectBrain

_LAYER_VIEWS: dict[MemoryLayer, tuple[str, str, int]] = {
    # layer -> (scope, 上下文标题, 优先级)
    MemoryLayer.ENVIRONMENT: (
        "global",
        "Flux Environment Memory（平台运行规则，优先于普通项目上下文）",
        0,
    ),
    MemoryLayer.USER: ("cross_workspace", "User Memory（跨 Workspace）", 1),
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """把库里读出的时间归一到带时区的 UTC（SQLite 不保存时区，PostgreSQL 保留）。"""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _is_expired(entry: MemoryEntry) -> bool:
    if entry.expires_at is None:
        return False
    return _as_utc(entry.expires_at) <= _utcnow()


class MemoryService:
    def __init__(
        self,
        repository: MemoryRepository,
        *,
        brain: ProjectBrain | None = None,
        policies: dict[MemoryLayer, MemoryPolicy] | None = None,
        environment_seed: tuple[tuple[str, str], ...] | None = None,
    ) -> None:
        self._repository = repository
        self._brain = brain
        # 按层覆盖默认策略（测试与部署都可以只改某一层）
        self._policies: dict[MemoryLayer, MemoryPolicy] = {**MEMORY_POLICIES, **(policies or {})}
        self._environment_seed = (
            ENVIRONMENT_MEMORY_SEED if environment_seed is None else environment_seed
        )
        # 账号云同步（可选）：本机写入后推到云端；云端快照回填本地缓存。
        # 未装配时行为与原来完全一致（纯本地三层记忆）。
        self._cloud: Any = None

    def attach_cloud(self, sync: Any) -> None:
        """接线账号级记忆同步；同步失败不能影响本地读写，错误由同步组件自行记账。"""
        self._cloud = sync

    # --- User Memory（用户确认后写入；可查可删）---

    async def write_user(
        self,
        *,
        content: str,
        source: str = "user",
        meta: dict | None = None,
    ) -> MemoryEntry:
        """写一条 User Memory。入口只有用户侧（REST），Agent 没有直写通道。"""
        text = self._validate(content, where="User Memory")
        await self._sweep_expired(MemoryLayer.USER)
        entry = await self._repository.insert(
            layer=MemoryLayer.USER,
            content=text,
            source=source.strip() or "user",
            meta=meta,
            expires_at=self._deadline(MemoryLayer.USER),
        )
        await self._repository.prune_layer(
            MemoryLayer.USER, keep=self._policies[MemoryLayer.USER].max_entries
        )
        if self._cloud is not None:
            # 推到账号云并回填 cloud_id；云端不可达时保留本地条目，等待下一次同步补推
            pushed = await self._cloud.push_entry(entry)
            if pushed is not None:
                entry = pushed
        return entry

    async def update_user(self, entry_id: str | uuid.UUID, *, content: str) -> MemoryEntry:
        """修改一条 User Memory（手动编辑）；同步失败时退回待补推，避免下轮被云端旧值覆盖。"""
        entry = await self._repository.get(entry_id)
        if entry.layer != MemoryLayer.USER.value:
            raise PermissionDeniedError(
                "只有 User Memory 可修改；Environment / Project 记忆由平台或项目流程维护",
                details={"memory_id": str(entry_id), "layer": entry.layer},
            )
        text = self._validate(content, where="User Memory")
        updated = await self._repository.update_content(entry.id, content=text)
        cloud_id = str((updated.meta or {}).get("cloud_id") or "")
        if self._cloud is not None and cloud_id:
            ok = await self._cloud.update_remote(cloud_id, updated)
            if not ok:
                # 云端没改成功：去掉 cloud_id，下一次同步按「新条目」重推并清掉旧行
                meta = {k: v for k, v in (updated.meta or {}).items() if k != "cloud_id"}
                meta["stale_cloud_id"] = cloud_id
                updated = await self._repository.update_meta(updated.id, meta=meta)
        return updated

    # --- Environment Memory（平台维护：种子 upsert + 平台写入）---

    async def ensure_environment(self) -> list[MemoryEntry]:
        """按 key 幂等落平台种子（内容随平台版本刷新），返回未过期的 Environment 条目。"""
        rows = await self._repository.list_layer(MemoryLayer.ENVIRONMENT)
        by_key = {entry.key: entry for entry in rows if entry.key}
        changed = False
        for key, content in self._environment_seed:
            current = by_key.get(key)
            if current is None:
                # 并发首次落种子：另一路已写入，同一 (layer, key) 唯一约束兜底
                with suppress(BadRequestError):
                    await self._repository.insert(
                        layer=MemoryLayer.ENVIRONMENT, key=key, content=content, source="platform"
                    )
                changed = True
            elif current.content != content:
                await self._repository.update_content(current.id, content=content)
                changed = True
        if changed:
            rows = await self._repository.list_layer(MemoryLayer.ENVIRONMENT)
        return [entry for entry in rows if not _is_expired(entry)]

    async def write_environment(
        self,
        *,
        key: str,
        content: str,
        source: str = "platform",
        meta: dict | None = None,
    ) -> MemoryEntry:
        """平台维护入口（不暴露给 REST / MCP）：写一条带稳定 key 的运行规则。"""
        text = self._validate(content, where="Environment Memory")
        clean_key = (key or "").strip()
        if not clean_key:
            raise ValidationError("Environment 记忆必须带稳定 key", details={"param": "key"})
        await self._sweep_expired(MemoryLayer.ENVIRONMENT)
        entry = await self._repository.insert(
            layer=MemoryLayer.ENVIRONMENT,
            key=clean_key,
            content=text,
            source=source.strip() or "platform",
            meta=meta,
            expires_at=self._deadline(MemoryLayer.ENVIRONMENT),
        )
        await self._repository.prune_layer(
            MemoryLayer.ENVIRONMENT, keep=self._policies[MemoryLayer.ENVIRONMENT].max_entries
        )
        return entry

    # --- 查询 / 删除 ---

    async def list_entries(self, *, layer: MemoryLayer | None = None) -> list[MemoryEntry]:
        """列出未过期条目；layer 缺省返回 Environment + User 两层。"""
        if layer is MemoryLayer.PROJECT:
            raise ValidationError(
                "Project Memory 请走项目接口：GET /projects/{project_id}/memory",
                details={"layer": MemoryLayer.PROJECT.value},
            )
        if layer is MemoryLayer.ENVIRONMENT:
            return await self.ensure_environment()
        if layer is MemoryLayer.USER:
            return await self._live(MemoryLayer.USER)
        return await self.ensure_environment() + await self._live(MemoryLayer.USER)

    async def delete(self, entry_id: str | uuid.UUID) -> None:
        """删除一条 User Memory；Environment / Project 层只能由各自的维护方处理。"""
        entry = await self._repository.get(entry_id)
        if entry.layer != MemoryLayer.USER.value:
            raise PermissionDeniedError(
                "只有 User Memory 可删除；Environment / Project 记忆由平台或项目流程维护",
                details={"memory_id": str(entry_id), "layer": entry.layer},
            )
        cloud_id = (entry.meta or {}).get("cloud_id")
        if self._cloud is not None and cloud_id:
            # 先删云端再删本地：云端失败时本地仍保留（可重试），不会出现"云端有、本地没了"
            await self._cloud.delete_remote(str(cloud_id))
        await self._repository.delete(entry_id)

    async def update_entry_meta(self, entry_id: str | uuid.UUID, meta: dict) -> MemoryEntry:
        """更新一条 User 记忆的 meta（云端同步回填 cloud_id 用）。"""
        return await self._repository.update_meta(entry_id, meta=meta)

    async def apply_cloud_snapshot(self, items: list[dict[str, Any]]) -> dict[str, int]:
        """把账号云快照合并进本地 User 缓存（多设备一致性）。

        - 云端有、本地没有 → 插入（meta.cloud_id 标记来源）；
        - 两边都有且内容不同 → 以云端为准刷新本地内容；
        - 本地有 cloud_id 但云端已删除 → 本地一并删除。
        """
        local = await self._live(MemoryLayer.USER)
        by_cloud: dict[str, MemoryEntry] = {}
        for entry in local:
            cloud_id = str((entry.meta or {}).get("cloud_id") or "")
            if cloud_id:
                by_cloud[cloud_id] = entry

        cloud_ids: set[str] = set()
        inserted = updated = 0
        for item in items:
            cloud_id = str(item.get("id") or "")
            content = str(item.get("content") or "").strip()
            if not cloud_id or not content:
                continue
            cloud_ids.add(cloud_id)
            current = by_cloud.get(cloud_id)
            if current is None:
                await self._repository.insert(
                    layer=MemoryLayer.USER,
                    content=content,
                    source=str(item.get("source") or "auto"),
                    meta={
                        "cloud_id": cloud_id,
                        "origin_device": item.get("origin_device"),
                    },
                )
                inserted += 1
            elif current.content.strip() != content:
                await self._repository.update_content(current.id, content=content)
                updated += 1

        removed = 0
        stale = [entry.id for cloud_id, entry in by_cloud.items() if cloud_id not in cloud_ids]
        if stale:
            removed = await self._repository.delete_many(stale)
        await self._repository.prune_layer(
            MemoryLayer.USER, keep=self._policies[MemoryLayer.USER].max_entries
        )
        return {"inserted": inserted, "updated": updated, "removed": removed}

    async def recall(self, *, project_id: str | uuid.UUID | None = None) -> dict[str, Any]:
        """三层统一只读出口：Environment → User → Project（仅当前项目）。"""
        layers: list[dict[str, Any]] = []
        blocks: list[str] = []

        for layer in (MemoryLayer.ENVIRONMENT, MemoryLayer.USER):
            entries = (
                await self.ensure_environment()
                if layer is MemoryLayer.ENVIRONMENT
                else await self._live(MemoryLayer.USER)
            )
            if not entries:
                continue
            scope, title, priority = _LAYER_VIEWS[layer]
            layers.append(
                {
                    "layer": layer.value,
                    "scope": scope,
                    "priority": priority,
                    "entries": [_entry_view(entry) for entry in entries],
                }
            )
            body = "\n".join(f"- {entry.content}" for entry in entries)
            blocks.append(f"# {title}\n{body}")

        if project_id is not None:
            if self._brain is None:
                raise ValidationError("未接入 Project Brain，无法读取 Project Memory")
            # Project Brain 自己会校验项目存在（不存在即 NotFoundError）
            text = await self._brain.context(project_id)
            layers.append(
                {
                    "layer": MemoryLayer.PROJECT.value,
                    "scope": "workspace",
                    "priority": 2,
                    "project_id": str(project_id),
                    "text": text,
                }
            )
            blocks.append(f"# Project Memory（当前 Workspace）\n{text}")

        return {
            "project_id": str(project_id) if project_id is not None else None,
            "layers": layers,
            "content": "\n\n".join(blocks),
            "note": RECALL_NOTE,
        }

    # --- 内部 ---

    def _validate(self, content: str, *, where: str) -> str:
        text = (content or "").strip()
        if not text:
            raise ValidationError(f"{where}内容不能为空", details={"where": where})
        if len(text) > MAX_CONTENT_CHARS:
            raise ValidationError(
                f"{where}内容超过上限（{MAX_CONTENT_CHARS} 字符）",
                details={"where": where, "max_chars": MAX_CONTENT_CHARS},
            )
        ensure_no_secret(text, where=where)
        return text

    def _deadline(self, layer: MemoryLayer) -> datetime | None:
        ttl = self._policies[layer].ttl
        if ttl is None:
            return None
        return _utcnow() + ttl

    async def _live(self, layer: MemoryLayer) -> list[MemoryEntry]:
        rows = await self._repository.list_layer(layer)
        return [entry for entry in rows if not _is_expired(entry)]

    async def _sweep_expired(self, layer: MemoryLayer) -> int:
        """清理机制：写过期条目即顺手删除，库不因 TTL 无界累积。"""
        rows = await self._repository.list_layer(layer)
        stale = [entry.id for entry in rows if _is_expired(entry)]
        if not stale:
            return 0
        return await self._repository.delete_many(stale)


def _entry_view(entry: MemoryEntry) -> dict[str, Any]:
    """recall 上下文里的条目视图：只带内容与来源（元数据留给管理接口）。"""
    return {
        "id": str(entry.id),
        "content": entry.content,
        "source": entry.source,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
        "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
        "expires_at": entry.expires_at.isoformat() if entry.expires_at else None,
    }
