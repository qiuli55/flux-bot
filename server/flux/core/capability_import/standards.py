"""Flux 标准对象（批次③）：Agent / Skill / Connector 的规范化形状。

Scanner 的产物必须是"合法的 Flux 标准对象"——本模块是唯一裁判：

- 严格解析：未知字段直接拒绝。外部形态（SKILL.md frontmatter、各家 MCP 配置）
  千变万化，静默丢弃字段等于静默改变语义，宁可报错让调用方显式适配；
- 只读冻结：解析后不可变，`fingerprint` 才有意义；
- 值域中立：这里只做结构与类型校验；敏感内容 / 可疑命令 / 路径逃逸的判定
  在 `flux.core.capability_import.safety`（deny-by-default）。两者分开，
  扫描报告才能说清"为什么被拦"而不是笼统的"对象非法"。

`fingerprint` 取 spec 的 canonical JSON（键排序 + 紧凑分隔符）的 SHA-256：
同一份能力在任何机器、任何字段顺序下指纹一致，重复导入判定不依赖对象身份。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from flux.enums import CapabilityKind
from flux.errors import ValidationError

#: Connector 的三种接入形态：
#: stdio——本机起子进程；remote——远端 HTTP 端点；declared——只有声明（如 OAuth 插件），
#: 由具体 Connector 实现决定怎么调用。
CONNECTOR_TRANSPORTS: tuple[str, ...] = ("stdio", "remote", "declared")


# --- 解析辅助（所有报错都带 kind/field，且绝不回显字段值）---


def _require_mapping(data: Any, *, kind: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise ValidationError(f"{kind} 必须是对象", details={"kind": kind})
    return data


def _reject_unknown(data: Mapping[str, Any], allowed: frozenset[str], *, kind: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValidationError(
            f"{kind} 含未知字段：{', '.join(unknown)}",
            details={"kind": kind, "unknown": unknown},
        )


def _required_text(data: Mapping[str, Any], key: str, *, kind: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(
            f"{kind} 缺少必填字段 {key}（非空字符串）",
            details={"kind": kind, "field": key},
        )
    return value


def _text_or_default(data: Mapping[str, Any], key: str, *, kind: str, default: str) -> str:
    if key not in data or data[key] is None:
        return default
    value = data[key]
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(
            f"{kind}.{key} 必须是非空字符串", details={"kind": kind, "field": key}
        )
    return value


def _optional_text(data: Mapping[str, Any], key: str, *, kind: str, default: str = "") -> str:
    value = data.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValidationError(f"{kind}.{key} 必须是字符串", details={"kind": kind, "field": key})
    return value


def _optional_text_or_none(data: Mapping[str, Any], key: str, *, kind: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{kind}.{key} 必须是字符串", details={"kind": kind, "field": key})
    return value


def _string_tuple(data: Mapping[str, Any], key: str, *, kind: str) -> tuple[str, ...]:
    value = data.get(key)
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ValidationError(
            f"{kind}.{key} 必须是字符串数组", details={"kind": kind, "field": key}
        )
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValidationError(
                f"{kind}.{key} 必须是字符串数组", details={"kind": kind, "field": key}
            )
        items.append(item)
    return tuple(items)


def _optional_string_tuple_or_none(
    data: Mapping[str, Any], key: str, *, kind: str
) -> tuple[str, ...] | None:
    if key not in data or data[key] is None:
        return None
    value = _string_tuple(data, key, kind=kind)
    if not value:
        raise ValidationError(f"{kind}.{key} 不能是空数组", details={"kind": kind, "field": key})
    return value


def _boolean(data: Mapping[str, Any], key: str, *, kind: str, default: bool = False) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ValidationError(f"{kind}.{key} 必须是布尔值", details={"kind": kind, "field": key})
    return value


def _plain_dict(data: Mapping[str, Any], key: str, *, kind: str) -> dict[str, Any]:
    value = data.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValidationError(f"{kind}.{key} 必须是对象", details={"kind": kind, "field": key})
    return dict(value)


# --- 标准对象 ---

_SKILL_FIELDS = frozenset(
    {
        "name",
        "source",
        "version",
        "description",
        "entry",
        "requires_bins",
        "requires_skills",
        "permissions",
        "network",
        "files",
    }
)


@dataclass(frozen=True)
class FluxSkill:
    """一个 Skill 的 Flux 标准形态（SKILL.md 目录）。

    `entry` 是相对 Skill 根目录的入口文件；`files` 是声明会读取/写入的相对路径。
    两者都只存声明，安全检查负责判定路径是否逃逸。
    """

    name: str
    source: str
    description: str = ""
    entry: str = "SKILL.md"
    version: str | None = None
    requires_bins: tuple[str, ...] = ()
    requires_skills: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    network: bool = False
    files: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: Any) -> FluxSkill:
        raw = _require_mapping(data, kind="FluxSkill")
        _reject_unknown(raw, _SKILL_FIELDS, kind="FluxSkill")
        return cls(
            name=_required_text(raw, "name", kind="FluxSkill"),
            source=_required_text(raw, "source", kind="FluxSkill"),
            description=_optional_text(raw, "description", kind="FluxSkill"),
            entry=_text_or_default(raw, "entry", kind="FluxSkill", default="SKILL.md"),
            version=_optional_text_or_none(raw, "version", kind="FluxSkill"),
            requires_bins=_string_tuple(raw, "requires_bins", kind="FluxSkill"),
            requires_skills=_string_tuple(raw, "requires_skills", kind="FluxSkill"),
            permissions=_string_tuple(raw, "permissions", kind="FluxSkill"),
            network=_boolean(raw, "network", kind="FluxSkill"),
            files=_string_tuple(raw, "files", kind="FluxSkill"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source": self.source,
            "version": self.version,
            "description": self.description,
            "entry": self.entry,
            "requires_bins": list(self.requires_bins),
            "requires_skills": list(self.requires_skills),
            "permissions": list(self.permissions),
            "network": self.network,
            "files": list(self.files),
        }


_CONNECTOR_FIELDS = frozenset(
    {
        "name",
        "source",
        "version",
        "description",
        "transport",
        "command",
        "url",
        "actions",
        "required_permissions",
        "network",
        "configuration",
    }
)


@dataclass(frozen=True)
class FluxConnector:
    """一个 MCP / 插件 Connector 的 Flux 标准形态。

    `configuration` 只允许放非敏感标量与 env/header 的**键名或 ${VAR} 引用**，
    绝不含明文值；清洗由 Scanner 负责，这里只保证是对象结构。
    """

    name: str
    source: str
    transport: str
    description: str = ""
    version: str | None = None
    command: tuple[str, ...] | None = None
    url: str | None = None
    actions: tuple[str, ...] = ()
    required_permissions: tuple[str, ...] = ()
    network: bool = False
    configuration: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Any) -> FluxConnector:
        raw = _require_mapping(data, kind="FluxConnector")
        _reject_unknown(raw, _CONNECTOR_FIELDS, kind="FluxConnector")
        transport = _required_text(raw, "transport", kind="FluxConnector")
        if transport not in CONNECTOR_TRANSPORTS:
            raise ValidationError(
                f"FluxConnector.transport 只能是 {' / '.join(CONNECTOR_TRANSPORTS)}",
                details={"kind": "FluxConnector", "field": "transport"},
            )
        command = _optional_string_tuple_or_none(raw, "command", kind="FluxConnector")
        url = _optional_text_or_none(raw, "url", kind="FluxConnector")
        if transport == "stdio" and command is None:
            raise ValidationError(
                "FluxConnector transport=stdio 必须给出 command",
                details={"kind": "FluxConnector", "field": "command"},
            )
        if transport == "remote" and not url:
            raise ValidationError(
                "FluxConnector transport=remote 必须给出 url",
                details={"kind": "FluxConnector", "field": "url"},
            )
        return cls(
            name=_required_text(raw, "name", kind="FluxConnector"),
            source=_required_text(raw, "source", kind="FluxConnector"),
            transport=transport,
            description=_optional_text(raw, "description", kind="FluxConnector"),
            version=_optional_text_or_none(raw, "version", kind="FluxConnector"),
            command=command,
            url=url,
            actions=_string_tuple(raw, "actions", kind="FluxConnector"),
            required_permissions=_string_tuple(raw, "required_permissions", kind="FluxConnector"),
            network=_boolean(raw, "network", kind="FluxConnector"),
            configuration=_plain_dict(raw, "configuration", kind="FluxConnector"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source": self.source,
            "version": self.version,
            "description": self.description,
            "transport": self.transport,
            "command": list(self.command) if self.command is not None else None,
            "url": self.url,
            "actions": list(self.actions),
            "required_permissions": list(self.required_permissions),
            "network": self.network,
            "configuration": dict(self.configuration),
        }


_AGENT_FIELDS = frozenset(
    {
        "name",
        "source",
        "version",
        "executable",
        "path",
        "capabilities",
        "auth_status",
        "install_status",
    }
)


@dataclass(frozen=True)
class FluxAgent:
    """一个本机 CLI Agent 的 Flux 标准形态（installation 事实的只读投影）。"""

    name: str
    source: str
    executable: str
    install_status: str
    version: str | None = None
    path: str | None = None
    capabilities: tuple[str, ...] = ()
    auth_status: str = "unknown"

    @classmethod
    def from_dict(cls, data: Any) -> FluxAgent:
        raw = _require_mapping(data, kind="FluxAgent")
        _reject_unknown(raw, _AGENT_FIELDS, kind="FluxAgent")
        return cls(
            name=_required_text(raw, "name", kind="FluxAgent"),
            source=_required_text(raw, "source", kind="FluxAgent"),
            executable=_required_text(raw, "executable", kind="FluxAgent"),
            install_status=_required_text(raw, "install_status", kind="FluxAgent"),
            version=_optional_text_or_none(raw, "version", kind="FluxAgent"),
            path=_optional_text_or_none(raw, "path", kind="FluxAgent"),
            capabilities=_string_tuple(raw, "capabilities", kind="FluxAgent"),
            auth_status=_optional_text(raw, "auth_status", kind="FluxAgent", default="unknown"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source": self.source,
            "version": self.version,
            "executable": self.executable,
            "path": self.path,
            "capabilities": list(self.capabilities),
            "auth_status": self.auth_status,
            "install_status": self.install_status,
        }


FluxSpec = FluxSkill | FluxConnector | FluxAgent

_SPEC_TYPES: dict[CapabilityKind, type[FluxSkill] | type[FluxConnector] | type[FluxAgent]] = {
    CapabilityKind.SKILL: FluxSkill,
    CapabilityKind.CONNECTOR: FluxConnector,
    CapabilityKind.AGENT: FluxAgent,
}


def parse_spec(kind: CapabilityKind, data: Any) -> FluxSpec:
    """按 kind 严格解析一个标准对象；未知字段 / 类型不符一律 ValidationError。"""
    spec_type = _SPEC_TYPES[kind]
    return spec_type.from_dict(data)


def fingerprint(spec: FluxSpec) -> str:
    """spec 的 canonical JSON 指纹：字段顺序无关，内容变即指纹变。"""
    canonical = json.dumps(
        spec.to_dict(), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "CONNECTOR_TRANSPORTS",
    "FluxAgent",
    "FluxConnector",
    "FluxSkill",
    "FluxSpec",
    "fingerprint",
    "parse_spec",
]
