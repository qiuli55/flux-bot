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

from flux.config import Settings
from flux.core.memory.policies import (
    ENVIRONMENT_MEMORY_SEED,
    MAX_CONTENT_CHARS,
    MEMORY_POLICIES,
    RECALL_NOTE,
    MemoryPolicy,
)
from flux.core.memory.repository import MemoryRepository
from flux.core.memory.secrets import ensure_no_secret
from flux.core.user_scope import normalize_cloud_user_id, owns
from flux.enums import MemoryLayer
from flux.errors import BadRequestError, NotFoundError, PermissionDeniedError, ValidationError
from flux.models.memory import MemoryEntry

#: 无法确定"这是谁"时的稳定错误码（与 devices / keys 保持一致）。
CLOUD_LOGIN_REQUIRED = "cloud_login_required"

#: 注入共享空间记忆的字符预算（与共享用户记忆的注入预算同量级）。
SHARED_RENDER_MAX_CHARS = 2000

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


def _fits(used: int, line: str, limit: int) -> bool:
    """预算判断：不写回 used，由调用方决定是否采用。"""
    return used + len(line) + 1 <= limit


class MemoryService:
    def __init__(
        self,
        repository: MemoryRepository,
        *,
        brain: ProjectBrain | None = None,
        policies: dict[MemoryLayer, MemoryPolicy] | None = None,
        environment_seed: tuple[tuple[str, str], ...] | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._repository = repository
        self._brain = brain
        #: 用于把「没显式传人」的调用（MCP / 云端同步 / 内部任务）回退到主用户。
        self._settings = settings
        # 按层覆盖默认策略（测试与部署都可以只改某一层）
        self._policies: dict[MemoryLayer, MemoryPolicy] = {**MEMORY_POLICIES, **(policies or {})}
        self._environment_seed = (
            ENVIRONMENT_MEMORY_SEED if environment_seed is None else environment_seed
        )
        # 账号云同步（可选）：本机写入后推到云端；云端快照回填本地缓存。
        # 未装配时行为与原来完全一致（纯本地三层记忆）。
        self._cloud: Any = None
        # 共享空间服务（可选）：Phase 3 装配。未装配时 shared 层为空，其余行为不变。
        self._spaces: Any = None

    def attach_cloud(self, sync: Any) -> None:
        """接线账号级记忆同步；同步失败不能影响本地读写，错误由同步组件自行记账。"""
        self._cloud = sync

    def attach_spaces(self, spaces: Any) -> None:
        """接线共享空间服务；未装配时 Shared 层按空处理（不泄露任何空间内容）。"""
        self._spaces = spaces

    # --- User Memory（用户确认后写入；可查可删）---

    async def write_user(
        self,
        *,
        content: str,
        source: str = "user",
        meta: dict | None = None,
        cloud_user_id: str | None = None,
    ) -> MemoryEntry:
        """写一条 User Memory。入口只有用户侧（REST），Agent 没有直写通道。

        必须能确定归属人（请求 principal / 主用户配置）；分不清就拒绝，
        绝不把记忆写到一个"公共"池子里。
        """
        owner = self._require_user(cloud_user_id)
        text = self._validate(content, where="User Memory")
        await self._sweep_expired(MemoryLayer.USER, owner)
        entry = await self._repository.insert(
            layer=MemoryLayer.USER,
            content=text,
            source=source.strip() or "user",
            meta=meta,
            expires_at=self._deadline(MemoryLayer.USER),
            cloud_user_id=owner,
        )
        await self._repository.prune_layer(
            MemoryLayer.USER,
            keep=self._policies[MemoryLayer.USER].max_entries,
            cloud_user_id=owner,
            owner_cloud_user_id=self._owner(),
        )
        if self._can_sync(entry):
            # 推到账号云并回填 cloud_id；云端不可达时保留本地条目，等待下一次同步补推
            pushed = await self._cloud.push_entry(entry)
            if pushed is not None:
                entry = pushed
        return entry

    async def update_user(
        self,
        entry_id: str | uuid.UUID,
        *,
        content: str,
        cloud_user_id: str | None = None,
    ) -> MemoryEntry:
        """修改一条 User Memory（手动编辑）；同步失败时退回待补推，避免下轮被云端旧值覆盖。"""
        owner = self._require_user(cloud_user_id)
        entry = await self._owned_entry(entry_id, owner)
        if entry.layer != MemoryLayer.USER.value:
            raise PermissionDeniedError(
                "只有 User Memory 可修改；Environment / Project 记忆由平台或项目流程维护",
                details={"memory_id": str(entry_id), "layer": entry.layer},
            )
        text = self._validate(content, where="User Memory")
        updated = await self._repository.update_content(entry.id, content=text)
        cloud_id = str((updated.meta or {}).get("cloud_id") or "")
        if self._can_sync(updated) and cloud_id:
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

    # --- Shared Memory（共享空间；只有空间成员可读写）---

    async def write_shared(
        self,
        space_id: str | uuid.UUID,
        *,
        content: str,
        cloud_user_id: str | None,
        source: str = "user",
        meta: dict | None = None,
    ) -> MemoryEntry:
        """写一条 Shared 记忆；调用方必须是该空间成员。

        `cloud_user_id` 记录作者，但访问权只看空间成员关系——不按作者过滤。
        """
        spaces = self._require_spaces()
        user = self._resolve_user(cloud_user_id)
        if user is None:
            raise PermissionDeniedError(
                "该操作需要 Flux 账号登录（或由部署方设置 FLUX_OWNER_CLOUD_USER_ID）",
                code=CLOUD_LOGIN_REQUIRED,
            )
        space = await spaces.require_member(space_id, user)
        text = self._validate(content, where="Shared Memory")
        await self._sweep_expired_shared(space.id)
        entry = await self._repository.insert(
            layer=MemoryLayer.SHARED,
            content=text,
            source=source.strip() or "user",
            meta=meta,
            expires_at=self._deadline(MemoryLayer.SHARED),
            cloud_user_id=user,
            space_id=space.id,
        )
        await self._repository.prune_shared(
            space.id, keep=self._policies[MemoryLayer.SHARED].max_entries
        )
        return entry

    async def list_shared(
        self,
        space_id: str | uuid.UUID,
        *,
        cloud_user_id: str | None,
    ) -> list[MemoryEntry]:
        """列出某个空间里未过期的 Shared 记忆；非成员按空间不存在处理。"""
        spaces = self._require_spaces()
        user = self._resolve_user(cloud_user_id)
        if user is None:
            return []
        space = await spaces.require_member(space_id, user)
        rows = await self._repository.list_shared(space_id=space.id)
        return [entry for entry in rows if not _is_expired(entry)]

    async def delete_shared(
        self,
        space_id: str | uuid.UUID,
        entry_id: str | uuid.UUID,
        *,
        cloud_user_id: str | None,
    ) -> None:
        """删除一条 Shared 记忆；非成员或条目不属于该空间一律按不存在处理。"""
        spaces = self._require_spaces()
        user = self._resolve_user(cloud_user_id)
        if user is None:
            raise NotFoundError(f"记忆 {entry_id} 不存在", details={"memory_id": str(entry_id)})
        space = await spaces.require_member(space_id, user)
        entry = await self._repository.get(entry_id)
        if entry.layer != MemoryLayer.SHARED.value or entry.space_id != space.id:
            raise NotFoundError(f"记忆 {entry_id} 不存在", details={"memory_id": str(entry_id)})
        await self._repository.delete(entry_id)

    async def shared_entries(
        self, cloud_user_id: str | None
    ) -> list[tuple[Any, list[MemoryEntry]]]:
        """当前用户所有空间里的 Shared 记忆，按空间分组（只含非空空间）。"""
        spaces = self._spaces
        user = normalize_cloud_user_id(cloud_user_id)
        if spaces is None or user is None:
            return []
        memberships = await spaces.memberships(user)
        if not memberships:
            return []
        by_space: dict[uuid.UUID, list[MemoryEntry]] = {}
        rows = await self._repository.list_shared_in_spaces(
            [space.id for space, _role in memberships]
        )
        for entry in rows:
            if entry.space_id is None or _is_expired(entry):
                continue
            by_space.setdefault(entry.space_id, []).append(entry)
        return [
            (space, by_space[space.id]) for space, _role in memberships if by_space.get(space.id)
        ]

    async def render_shared_block(self, cloud_user_id: str | None) -> str:
        """把当前用户所有共享空间的记忆压成一段上下文（预算内，失败降级空串）。

        显式传入优先；内部调用（历史任务 NULL）回退到主用户，与 User 层同一口径。
        """
        try:
            groups = await self.shared_entries(self._resolve_user(cloud_user_id))
        except Exception:  # noqa: BLE001 - 共享记忆只是增强，不能挡住对话/开工
            return ""
        if not groups:
            return ""
        lines: list[str] = []
        used = 0
        for space, entries in groups:
            header = f"共享空间「{space.name}」记忆（仅成员可见）："
            if not _fits(used, header, SHARED_RENDER_MAX_CHARS):
                break
            used += len(header) + 1
            lines.append(header)
            for entry in entries:
                text = " ".join((entry.content or "").split())
                if not text:
                    continue
                line = f"- {text}"
                if not _fits(used, line, SHARED_RENDER_MAX_CHARS):
                    break
                used += len(line) + 1
                lines.append(line)
        return "\n".join(lines) + "\n" if lines else ""

    # --- 查询 / 删除 ---

    async def list_entries(
        self,
        *,
        layer: MemoryLayer | None = None,
        cloud_user_id: str | None = None,
    ) -> list[MemoryEntry]:
        """列出未过期条目；layer 缺省返回 Environment + User 两层。

        Environment 永远全局可见；User 层只返回当前人的条目。没有可解析的人时
        User 层为空（不是"大家都看得到"），避免把别人的记忆泄露出去。
        """
        if layer is MemoryLayer.PROJECT:
            raise ValidationError(
                "Project Memory 请走项目接口：GET /projects/{project_id}/memory",
                details={"layer": MemoryLayer.PROJECT.value},
            )
        owner = self._resolve_user(cloud_user_id)
        if layer is MemoryLayer.ENVIRONMENT:
            return await self.ensure_environment()
        if layer is MemoryLayer.USER:
            return await self._live(MemoryLayer.USER, owner)
        if layer is MemoryLayer.SHARED:
            return [
                entry for _space, entries in await self.shared_entries(owner) for entry in entries
            ]
        return await self.ensure_environment() + await self._live(MemoryLayer.USER, owner)

    async def delete(self, entry_id: str | uuid.UUID, *, cloud_user_id: str | None = None) -> None:
        """删除一条 User Memory；Environment / Project 层只能由各自的维护方处理。

        别人的条目按"不存在"处理（404），不泄露 id 是否存在。
        """
        owner = self._require_user(cloud_user_id)
        entry = await self._owned_entry(entry_id, owner)
        if entry.layer != MemoryLayer.USER.value:
            raise PermissionDeniedError(
                "只有 User Memory 可删除；Environment / Project 记忆由平台或项目流程维护",
                details={"memory_id": str(entry_id), "layer": entry.layer},
            )
        cloud_id = (entry.meta or {}).get("cloud_id")
        if self._can_sync(entry) and cloud_id:
            # 先删云端再删本地：云端失败时本地仍保留（可重试），不会出现"云端有、本地没了"
            await self._cloud.delete_remote(str(cloud_id))
        await self._repository.delete(entry_id)

    async def update_entry_meta(
        self,
        entry_id: str | uuid.UUID,
        meta: dict,
        *,
        cloud_user_id: str | None = None,
    ) -> MemoryEntry:
        """更新一条 User 记忆的 meta（云端同步回填 cloud_id 用）。"""
        owner = self._require_user(cloud_user_id)
        await self._owned_entry(entry_id, owner)
        return await self._repository.update_meta(entry_id, meta=meta)

    async def apply_cloud_snapshot(
        self, items: list[dict[str, Any]], *, cloud_user_id: str | None = None
    ) -> dict[str, int]:
        """把账号云快照合并进本地 User 缓存（多设备一致性）。

        只处理当前/主用户自己的 User 层条目，绝不把别人在这台机器上的记忆
        推上云或从云端覆盖掉。
        """
        owner = self._resolve_user(cloud_user_id)
        if owner is None:
            # 不知道这是谁：不写、不删、不覆盖任何人的记忆。
            return {"inserted": 0, "updated": 0, "removed": 0}
        local = await self._live(MemoryLayer.USER, owner)
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
                    cloud_user_id=owner,
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
            MemoryLayer.USER,
            keep=self._policies[MemoryLayer.USER].max_entries,
            cloud_user_id=owner,
            owner_cloud_user_id=self._owner(),
        )
        return {"inserted": inserted, "updated": updated, "removed": removed}

    async def recall(
        self,
        *,
        project_id: str | uuid.UUID | None = None,
        cloud_user_id: str | None = None,
    ) -> dict[str, Any]:
        """三层统一只读出口：Environment → User → Project（仅当前项目）。

        Environment 是平台规则，任何登录用户都能读到；User 层只含当前人的条目。
        """
        layers: list[dict[str, Any]] = []
        blocks: list[str] = []
        owner = self._resolve_user(cloud_user_id)

        for layer in (MemoryLayer.ENVIRONMENT, MemoryLayer.USER):
            entries = (
                await self.ensure_environment()
                if layer is MemoryLayer.ENVIRONMENT
                else await self._live(MemoryLayer.USER, owner)
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

        # 共享空间：只拼入当前用户是成员的空间；非成员完全看不到（包括空间名）。
        for space, entries in await self.shared_entries(owner):
            layers.append(
                {
                    "layer": MemoryLayer.SHARED.value,
                    "scope": "space",
                    "priority": 1,
                    "space_id": str(space.id),
                    "space_name": space.name,
                    "entries": [_entry_view(entry) for entry in entries],
                }
            )
            body = "\n".join(f"- {entry.content}" for entry in entries)
            blocks.append(f"# 共享空间「{space.name}」记忆（仅成员可见）\n{body}")

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

    # --- 按人隔离 ---

    def _owner(self) -> str | None:
        return normalize_cloud_user_id(getattr(self._settings, "owner_cloud_user_id", None))

    def _resolve_user(self, cloud_user_id: str | None) -> str | None:
        """显式传入优先；内部调用（MCP / 云端同步）回退到配置的主用户。"""
        return normalize_cloud_user_id(cloud_user_id) or self._owner()

    def _require_user(self, cloud_user_id: str | None) -> str:
        owner = self._resolve_user(cloud_user_id)
        if owner is None:
            raise PermissionDeniedError(
                "该操作需要 Flux 账号登录（或由部署方设置 FLUX_OWNER_CLOUD_USER_ID）",
                code=CLOUD_LOGIN_REQUIRED,
                details={
                    "hint": "请先登录账号云；本机 CLI / 主用户可配置 FLUX_OWNER_CLOUD_USER_ID"
                },
            )
        return owner

    def _can_sync(self, entry: MemoryEntry) -> bool:
        """账号云同步用的是主用户的设备凭据，只能操作主用户自己的条目。

        别人的 User 记忆留在本机，绝不推上主人的云账号，也不从云端覆盖。
        """
        if self._cloud is None:
            return False
        owner = self._owner()
        return owner is not None and owns(entry.cloud_user_id, owner, owner)

    async def _owned_entry(self, entry_id: str | uuid.UUID, owner: str) -> MemoryEntry:
        """取一条属于 owner 的 User 记忆；别人的条目按不存在处理（404）。"""
        entry = await self._repository.get(entry_id)
        if entry.layer == MemoryLayer.USER.value and not owns(
            entry.cloud_user_id, owner, self._owner()
        ):
            raise NotFoundError(f"记忆 {entry_id} 不存在", details={"memory_id": str(entry_id)})
        return entry

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

    def _require_spaces(self) -> Any:
        if self._spaces is None:
            # 未装配共享空间能力时，Shared 层等同于「不存在任何空间」。
            raise NotFoundError("共享空间不存在", details={"space_id": ""})
        return self._spaces

    async def _sweep_expired_shared(self, space_id: uuid.UUID) -> int:
        rows = await self._repository.list_shared(space_id=space_id)
        stale = [entry.id for entry in rows if _is_expired(entry)]
        if not stale:
            return 0
        return await self._repository.delete_many(stale)

    async def _live(self, layer: MemoryLayer, owner: str | None = None) -> list[MemoryEntry]:
        if layer is MemoryLayer.USER:
            if owner is None:
                # 分不清是谁：User 层宁可为空，也不能返回所有人的记忆。
                return []
            rows = await self._repository.list_layer(
                layer, cloud_user_id=owner, owner_cloud_user_id=self._owner()
            )
        else:
            rows = await self._repository.list_layer(layer)
        return [entry for entry in rows if not _is_expired(entry)]

    async def _sweep_expired(self, layer: MemoryLayer, owner: str | None = None) -> int:
        """清理机制：写过期条目即顺手删除，库不因 TTL 无界累积。"""
        if layer is MemoryLayer.USER:
            if owner is None:
                return 0
            rows = await self._repository.list_layer(
                layer, cloud_user_id=owner, owner_cloud_user_id=self._owner()
            )
        else:
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
