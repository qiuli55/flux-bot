"""能力导入 API（批次③ §5）：扫描本机 Agent / Skill / Connector → 标准转换 → 注册表。

- 只接受"要导入哪一个"，不接受任意路径或 spec——扫描源是代码常量，防止把接口
  变成任意文件探测/读取器；
- blocked 的候选出现在扫描报告里，但导入被 422 拒绝（理由只回字段路径 + 标签，不回值）；
- 重复导入不静默覆盖：内容变化且未给决策时 409，details 给出字段级差异与
  keep / replace 两个选项。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from flux.api.deps import get_container
from flux.api.response import ok
from flux.container import Container
from flux.enums import CapabilityKind
from flux.schemas.api import CapabilityImportRequest

router = APIRouter(prefix="/capabilities", tags=["capabilities"])


@router.get("")
async def list_capabilities(
    kind: CapabilityKind | None = Query(
        default=None, description="按种类过滤：agent / skill / connector；缺省返回全部"
    ),
    container: Container = Depends(get_container),
) -> dict[str, object]:
    """已导入能力的注册表（同一 kind+name 只有一条）。"""
    records = await container.capability_import.list(kind=kind)
    return ok([record.to_dict() for record in records], metadata={"count": len(records)})


@router.post("/scan")
async def scan_capabilities(container: Container = Depends(get_container)) -> dict[str, object]:
    """扫描默认源并返回标准对象与安全结论；blocked 候选只能看、不能导入。"""
    report = await container.capability_import.scan()
    blocked = sum(1 for candidate in report.candidates if candidate.blocked)
    return ok(
        report.to_dict(),
        metadata={"candidates": len(report.candidates), "blocked": blocked},
    )


@router.post("/import")
async def import_capability(
    payload: CapabilityImportRequest, container: Container = Depends(get_container)
) -> dict[str, object]:
    """导入一个扫描候选。outcome ∈ imported / unchanged / kept / replaced。"""
    outcome = await container.capability_import.import_capability(
        payload.kind, payload.name, decision=payload.decision
    )
    return ok(outcome.to_dict(), metadata={"outcome": outcome.outcome})
