"""导入编排（批次③ §5）：扫描 → 安全检查 → 冲突决策 → 注册表。

原则：

- **API 不能注入任意 spec**：导入时在服务端重新扫描、按 kind+name 定位候选，
  调用方只能报"要导入哪一个"，不能递一份内容进来；
- **不静默覆盖**：同 (kind, name) 已存在且 fingerprint 变化时，没有显式决策就抛
  409（details 里给出字段级差异与可选决策 keep / replace），事务性地保持现状；
- **blocked 不可导入**：安全检查结论为 blocked 的候选直接 422 拒绝，理由只回
  字段路径 + 标签，不回值。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from flux.core.capability_import.repository import ImportedCapabilityRepository
from flux.core.capability_import.scanners import (
    AgentScanner,
    CapabilityCandidate,
    CapabilityScanReport,
    ConnectorScanner,
    ScanSourceReport,
    SkillScanner,
)
from flux.core.capability_import.standards import fingerprint as spec_fingerprint
from flux.core.event.bus import EventBus, Events
from flux.enums import CapabilityKind
from flux.errors import ConflictError, NotFoundError, ValidationError
from flux.models.imported_capability import ImportedCapability

#: 冲突时的两种决策（显式）：keep 保留现有记录；replace 用扫描到的新版本替换
ImportDecision = Literal["keep", "replace"]

_DECISIONS: tuple[str, ...] = ("keep", "replace")


@dataclass(frozen=True)
class ImportOutcome:
    """一次导入的结果：outcome ∈ imported / unchanged / kept / replaced。"""

    outcome: str
    record: ImportedCapability

    def to_dict(self) -> dict[str, Any]:
        return {"outcome": self.outcome, **self.record.to_dict()}


class CapabilityImportService:
    def __init__(
        self,
        repository: ImportedCapabilityRepository,
        *,
        skills: SkillScanner | None = None,
        connectors: ConnectorScanner | None = None,
        agents: AgentScanner | None = None,
        bus: EventBus | None = None,
    ) -> None:
        self._repository = repository
        self._skills = skills or SkillScanner()
        self._connectors = connectors or ConnectorScanner()
        self._agents = agents
        self._bus = bus

    async def scan(self) -> CapabilityScanReport:
        candidates: list[CapabilityCandidate] = []
        sources: list[ScanSourceReport] = []
        for scanner in (self._skills, self._connectors):
            found, reports = scanner.scan()
            candidates.extend(found)
            sources.extend(reports)
        if self._agents is not None:
            # Agent 的扫描会先刷新一次 installation 事实（真源在 agent_runtime）
            found, reports = await self._agents.scan()
            candidates.extend(found)
            sources.extend(reports)
        return CapabilityScanReport(candidates=tuple(candidates), sources=tuple(sources))

    async def list(self, kind: CapabilityKind | None = None) -> list[ImportedCapability]:
        return await self._repository.list(kind)

    async def import_capability(
        self,
        kind: CapabilityKind,
        name: str,
        *,
        decision: str | None = None,
    ) -> ImportOutcome:
        report = await self.scan()
        candidate = report.find(kind, name)
        if candidate is None:
            raise NotFoundError(
                f"扫描不到该能力：{kind.value}/{name}",
                details={
                    "kind": kind.value,
                    "name": name,
                    "hint": "先调用扫描接口查看可导入清单",
                },
            )
        if candidate.blocked:
            raise ValidationError(
                f"能力未通过安全检查，拒绝导入：{kind.value}/{name}",
                details={
                    "kind": kind.value,
                    "name": name,
                    "status": "blocked",
                    "findings": [finding.to_dict() for finding in candidate.findings],
                },
            )
        if candidate.spec is None:  # pragma: no cover - blocked 判定已覆盖该分支
            raise ValidationError(
                f"扫描候选不是合法的 Flux 标准对象：{kind.value}/{name}",
                details={"kind": kind.value, "name": name},
            )

        spec = candidate.spec.to_dict()
        incoming = spec_fingerprint(candidate.spec)
        existing = await self._repository.get(kind, name)

        if existing is None:
            record = await self._repository.insert(
                kind=kind,
                name=name,
                spec=spec,
                fingerprint=incoming,
                source=candidate.source,
                version=_optional_version(spec),
            )
            await self._publish(Events.CAPABILITY_IMPORTED, record)
            return ImportOutcome("imported", record)

        if existing.fingerprint == incoming:
            return ImportOutcome("unchanged", existing)

        if decision is None:
            raise ConflictError(
                f"已导入的能力发生变化：{kind.value}/{name}，需要显式决策",
                details={
                    "kind": kind.value,
                    "name": name,
                    "existing": _brief(existing),
                    "incoming": {
                        "source": candidate.source,
                        "version": _optional_version(spec),
                        "fingerprint": incoming,
                    },
                    "differences": _differences(existing.spec or {}, spec),
                    "decisions": list(_DECISIONS),
                },
            )
        if decision == "keep":
            # 保留现有记录：库里不动，给调用方一个明确的"我知道变了、但不换"结果
            return ImportOutcome("kept", existing)
        if decision != "replace":
            raise ValidationError(
                f"未知决策：{decision}（只能是 {' / '.join(_DECISIONS)}）",
                details={"decision": decision, "decisions": list(_DECISIONS)},
            )

        record = await self._repository.update(
            existing,
            spec=spec,
            fingerprint=incoming,
            source=candidate.source,
            version=_optional_version(spec),
        )
        await self._publish(Events.CAPABILITY_REPLACED, record)
        return ImportOutcome("replaced", record)

    async def _publish(self, event: str, record: ImportedCapability) -> None:
        if self._bus is None:
            return
        await self._bus.publish(
            event,
            {
                "kind": record.kind,
                "name": record.name,
                "source": record.source,
                "version": record.version,
                "fingerprint": record.fingerprint,
            },
        )


def _optional_version(spec: dict[str, Any]) -> str | None:
    version = spec.get("version")
    return version if isinstance(version, str) else None


def _brief(record: ImportedCapability) -> dict[str, Any]:
    return {
        "source": record.source,
        "version": record.version,
        "fingerprint": record.fingerprint,
        "updated_at": record.updated_at.isoformat() if record.updated_at is not None else None,
    }


def _differences(existing: dict[str, Any], incoming: dict[str, Any]) -> list[dict[str, Any]]:
    fields = sorted(set(existing) | set(incoming))
    return [
        {"field": field, "existing": existing.get(field), "incoming": incoming.get(field)}
        for field in fields
        if existing.get(field) != incoming.get(field)
    ]


__all__ = ["CapabilityImportService", "ImportDecision", "ImportOutcome"]
