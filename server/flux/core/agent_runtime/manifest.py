"""Agent Manifest：Agent 档案的声明与校验（主规格 §6.2；实施计划 §3.1）。

Manifest 只描述档案的静态身份与权限边界（角色 / 权限组 / 可见范围），
不含模型、不含 system prompt——它们归 Agent 自己（目标架构 §1）；
也**绝不含 API Key**（主规格 §14.3）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from flux.enums import AgentRole, Capability
from flux.errors import NotFoundError, ValidationError

MANIFEST_DIR = Path(__file__).resolve().parent / "manifests"

#: 第一批内置 Agent（主规格 §19.7：Architect / DevOps 待核心闭环稳定后再加）
BUILTIN_MANIFEST_FILES: tuple[str, ...] = (
    "tech_lead.yaml",
    "developer.yaml",
    "reviewer.yaml",
    "tester.yaml",
)

_ALLOWED_KEYS = frozenset({"name", "role", "description", "skills", "tools", "permissions"})
#: 明文出现在 Manifest 里即视为违规的键名片段（§14.3 密钥不外泄）
_FORBIDDEN_KEY_MARKERS = ("key", "secret", "token", "password", "credential")


@dataclass(frozen=True)
class AgentManifest:
    """一个 Agent 档案的声明式配置（§6.2：name / role / skills / tools / permissions）。"""

    name: str
    role: AgentRole
    description: str = ""
    skills: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    permissions: frozenset[Capability] = field(default_factory=frozenset)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": str(self.role),
            "description": self.description,
            "skills": list(self.skills),
            "tools": list(self.tools),
            "permissions": sorted(str(p) for p in self.permissions),
        }

    # --- 解析与校验 ---

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, source: str = "<dict>") -> AgentManifest:
        if not isinstance(data, Mapping):
            raise _invalid(source, "Manifest 顶层必须是映射（YAML mapping）")
        _reject_secret_keys(data, source=source)
        unknown = sorted(set(data) - _ALLOWED_KEYS)
        if unknown:
            raise _invalid(
                source,
                f"存在未知字段：{', '.join(unknown)}",
                {"allowed": sorted(_ALLOWED_KEYS)},
            )
        return cls(
            name=_require_str(data, "name", source),
            role=_parse_role(data.get("role"), source),
            description=_optional_str(data, "description", source),
            skills=_str_tuple(data, "skills", source),
            tools=_str_tuple(data, "tools", source),
            permissions=_parse_permissions(data.get("permissions"), source),
        )

    @classmethod
    def from_yaml(cls, text: str, *, source: str = "<yaml>") -> AgentManifest:
        try:
            loaded = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise _invalid(source, f"YAML 解析失败：{exc}") from exc
        if loaded is None:
            raise _invalid(source, "Manifest 内容为空")
        return cls.from_dict(loaded, source=source)

    @classmethod
    def load(cls, path: str | Path) -> AgentManifest:
        manifest_path = Path(path)
        if not manifest_path.is_file():
            raise NotFoundError(
                f"Agent Manifest 不存在：{manifest_path}",
                details={"path": str(manifest_path)},
            )
        return cls.from_yaml(manifest_path.read_text(encoding="utf-8"), source=str(manifest_path))


def load_manifests(directory: str | Path = MANIFEST_DIR) -> dict[str, AgentManifest]:
    """加载目录下全部 *.yaml，按 manifest.name 建索引。"""
    manifests: dict[str, AgentManifest] = {}
    for path in sorted(Path(directory).glob("*.yaml")):
        manifest = AgentManifest.load(path)
        if manifest.name in manifests:
            raise _invalid(str(path), f"Agent 名称重复：{manifest.name}")
        manifests[manifest.name] = manifest
    return manifests


def builtin_manifests() -> dict[str, AgentManifest]:
    """仓库内置的 4 份档案（Tech Lead / Developer / Reviewer / Tester）。"""
    present = {path.name for path in MANIFEST_DIR.glob("*.yaml")}
    missing = [name for name in BUILTIN_MANIFEST_FILES if name not in present]
    if missing:
        raise NotFoundError(f"内置 Agent Manifest 缺失：{', '.join(missing)}")
    return load_manifests(MANIFEST_DIR)


# --- 内部校验工具 ---


def _invalid(source: str, message: str, details: Any = None) -> ValidationError:
    return ValidationError(f"Agent Manifest 非法（{source}）：{message}", details=details)


def _reject_secret_keys(value: Any, *, source: str, path: str = "") -> None:
    """递归拒绝任何疑似密钥字段（§14.3：API Key 不得写进 Manifest）。"""
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            if any(marker in key_text.lower() for marker in _FORBIDDEN_KEY_MARKERS):
                raise _invalid(source, f"Manifest 不得包含密钥字段：{path}{key_text}")
            _reject_secret_keys(child, source=source, path=f"{path}{key_text}.")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, child in enumerate(value):
            _reject_secret_keys(child, source=source, path=f"{path}[{index}].")


def _require_str(data: Mapping[str, Any], key: str, source: str) -> str:
    return _require_str_value(data.get(key), key, source)


def _require_str_value(raw: Any, label: str, source: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise _invalid(source, f"{label} 必须是非空字符串")
    return raw.strip()


def _optional_str(data: Mapping[str, Any], key: str, source: str) -> str:
    raw = data.get(key)
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise _invalid(source, f"{key} 必须是字符串")
    return raw.strip()


def _str_tuple(data: Mapping[str, Any], key: str, source: str) -> tuple[str, ...]:
    raw = data.get(key)
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise _invalid(source, f"{key} 必须是字符串列表")
    values: list[str] = []
    for item in raw:
        values.append(_require_str_value(item, f"{key} 项", source))
    return tuple(values)


def _parse_role(raw: Any, source: str) -> AgentRole:
    text = _require_str_value(raw, "role", source)
    normalized = text.lower().replace(" ", "_").replace("-", "_")
    try:
        return AgentRole(normalized)
    except ValueError as exc:
        raise _invalid(
            source, f"未知角色：{text}", {"allowed": [str(role) for role in AgentRole]}
        ) from exc


def _parse_permissions(raw: Any, source: str) -> frozenset[Capability]:
    if raw is None:
        return frozenset()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise _invalid(source, "permissions 必须是字符串列表")
    permissions: set[Capability] = set()
    for item in raw:
        text = _require_str_value(item, "permissions 项", source)
        try:
            permissions.add(Capability(text.lower()))
        except ValueError as exc:
            raise _invalid(
                source,
                f"未知权限项：{text}",
                {"allowed": [str(cap) for cap in Capability]},
            ) from exc
    return frozenset(permissions)
