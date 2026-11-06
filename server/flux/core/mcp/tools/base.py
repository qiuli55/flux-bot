"""MCP 工具基元：工具定义、调用上下文、错误形状（目标架构 §3.3）。

一个工具 = 名字 + 描述 + 入参 schema + 所需能力 + 处理函数。
处理函数只做两件事：校验入参、调用 Flux 既有服务；不自己碰磁盘。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from flux.core.mcp.auth import AgentIdentity
from flux.enums import Capability
from flux.errors import ValidationError

if TYPE_CHECKING:  # pragma: no cover - 避免工具层与容器互相导入
    from flux.container import Container


@dataclass(frozen=True)
class ToolContext:
    """一次工具调用的上下文。identity 由服务端盖章，绝不来自请求体（§3.2）。"""

    container: Container
    identity: AgentIdentity
    tool_name: str


@dataclass(frozen=True)
class ToolSpec:
    name: str
    title: str
    description: str
    input_schema: dict[str, Any]
    capability: Capability
    handler: Callable[[ToolContext, dict[str, Any]], Awaitable[dict[str, Any]]]
    #: 命中保护名单时需要人工批准的写类工具（Phase 2 先 fail-closed 拒绝，
    #: 交互式审批桥随审查页一并落地）
    approval_capable: bool = False

    def to_mcp(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


def require_str(params: dict[str, Any], key: str, *, allow_empty: bool = False) -> str:
    value = params.get(key)
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValidationError(f"参数 {key} 必须是非空字符串", details={"param": key})
    return value.strip()


def optional_str(params: dict[str, Any], key: str) -> str | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"参数 {key} 必须是字符串", details={"param": key})
    return value.strip() or None


def reject_unknown(params: dict[str, Any], allowed: set[str]) -> None:
    """入参里出现未声明的字段一律报错——静默忽略会让"我传了但没生效"无从排查。

    报错里带上本工具允许的入参名：模型常凭记忆猜字段（`workdir` / `filename`），
    只报"未定义"的话它只能反复试错，既浪费调用又推高工具失败率。
    """
    unknown = sorted(set(params) - allowed)
    if unknown:
        allowed_text = "、".join(sorted(allowed)) or "（无）"
        raise ValidationError(
            f"存在未定义的入参：{'、'.join(unknown)}；该工具允许的入参是：{allowed_text}",
            details={"unknown": unknown, "allowed": sorted(allowed)},
        )
