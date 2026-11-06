"""Scanner：把本机真实形态转换成 Flux 标准对象（批次③ §5）。

只读发现，绝不修改用户环境：

- Skill：`SKILL.md` 目录（root 本身是 Skill，或 root 下每个子目录是 Skill 两种布局）；
- Connector：`opencode.json`（mcp 段）、`.mcp.json`（mcpServers 段）、
  插件 `connector.json`（connectors 段）；env / header 只清洗出**键名与 ${VAR} 引用**，
  明文值一律不进入标准对象；
- Agent：安装事实来自 `InstallationService.scan()`（真源在 agent_runtime），
  这里只做只读投影——扫描命令本身就是 installation 的 scan。

扫描报告（`CapabilityScanReport`）同时给出候选与来源两段：可疑样本以 blocked 状态
出现在报告里，但 `spec` 已达可解析的部分同样经严格解析，保证"看起来合法"与
"可导入"是两件事——被拦的原因只在 `findings`（字段路径 + 标签），报告里没有值。
"""

from __future__ import annotations

import glob
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from flux.core.agent_runtime.installation import InstallationService
from flux.core.capability_import.safety import (
    Finding,
    check_command,
    check_relative_path,
    check_text,
    check_url,
    is_safe_reference,
)
from flux.core.capability_import.standards import FluxAgent, FluxConnector, FluxSkill, FluxSpec
from flux.enums import AgentInstallStatus, CapabilityKind
from flux.errors import ValidationError
from flux.models.agent_installation import AgentInstallation

#: 默认扫描源（代码常量，不加 settings；REST 不接受任意路径，防文件探测）。
#: 相对路径按启动目录解析；`*` 通配由 glob 展开。
DEFAULT_SKILL_ROOTS: tuple[str, ...] = (
    "~/.trae-cn/builtin_skills",
    "~/.trae-cn/plugins/*/*/skills",
    "~/.claude/skills",
    "skills",
)
DEFAULT_CONNECTOR_FILES: tuple[str, ...] = (
    "~/.config/opencode/opencode.json",
    "~/.mcp.json",
    "~/.trae-cn/plugins/*/*/connector.json",
)

ScanOutcome = tuple[list["CapabilityCandidate"], list["ScanSourceReport"]]


@dataclass(frozen=True)
class ScanSourceReport:
    """一个扫描源的结论（path / kind / 状态 / 候选数）。"""

    path: str
    kind: str
    status: str  # scanned | missing | unreadable | invalid
    candidates: int

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "kind": self.kind,
            "status": self.status,
            "candidates": self.candidates,
        }


@dataclass(frozen=True)
class CapabilityCandidate:
    """一个待导入候选：标准对象 + 安全结论。blocked 的候选不可导入。"""

    kind: CapabilityKind
    name: str
    source: str
    spec: FluxSpec | None
    findings: tuple[Finding, ...] = ()

    @property
    def blocked(self) -> bool:
        return bool(self.findings)

    @property
    def status(self) -> str:
        return "blocked" if self.blocked else "ok"

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "name": self.name,
            "source": self.source,
            "status": self.status,
            "spec": self.spec.to_dict() if self.spec is not None else None,
            "findings": [finding.to_dict() for finding in self.findings],
        }


@dataclass(frozen=True)
class CapabilityScanReport:
    candidates: tuple[CapabilityCandidate, ...]
    sources: tuple[ScanSourceReport, ...]

    def find(self, kind: CapabilityKind, name: str) -> CapabilityCandidate | None:
        """按 kind+name 定位候选；同名多个来源时优先可导入的那个。"""
        matches = [
            candidate
            for candidate in self.candidates
            if candidate.kind is kind and candidate.name == name
        ]
        if not matches:
            return None
        for candidate in matches:
            if not candidate.blocked:
                return candidate
        return matches[0]

    def to_dict(self) -> dict[str, object]:
        blocked = sum(1 for candidate in self.candidates if candidate.blocked)
        return {
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "sources": [source.to_dict() for source in self.sources],
            "summary": {
                "total": len(self.candidates),
                "ok": len(self.candidates) - blocked,
                "blocked": blocked,
            },
        }


# --- Skill ---


class SkillScanner:
    def __init__(
        self, roots: Sequence[str] = DEFAULT_SKILL_ROOTS, *, base_dir: Path | None = None
    ) -> None:
        self._roots = tuple(roots)
        self._base_dir = base_dir or Path.cwd()

    def scan(self) -> ScanOutcome:
        candidates: list[CapabilityCandidate] = []
        sources: list[ScanSourceReport] = []
        for pattern in self._roots:
            roots = self._expand(pattern)
            if not roots:
                sources.append(ScanSourceReport(pattern, "skill", "missing", 0))
                continue
            for root in roots:
                found = self._scan_root(root)
                candidates.extend(found)
                sources.append(ScanSourceReport(str(root), "skill", "scanned", len(found)))
        candidates.sort(key=lambda candidate: (candidate.name, candidate.source))
        return candidates, sources

    def _expand(self, pattern: str) -> list[Path]:
        if pattern.startswith("~"):
            expanded = os.path.expanduser(pattern)
        elif os.path.isabs(pattern):
            expanded = pattern
        else:
            expanded = str(self._base_dir / pattern)
        return [Path(match) for match in sorted(glob.glob(expanded)) if Path(match).is_dir()]

    def _scan_root(self, root: Path) -> list[CapabilityCandidate]:
        if (root / "SKILL.md").is_file():
            return [self._skill_candidate(root)]
        found: list[CapabilityCandidate] = []
        for child in sorted(root.iterdir()):
            if child.is_dir() and (child / "SKILL.md").is_file():
                found.append(self._skill_candidate(child))
        return found

    def _skill_candidate(self, skill_dir: Path) -> CapabilityCandidate:
        entry_path = skill_dir / "SKILL.md"
        try:
            text = entry_path.read_text(encoding="utf-8")
        except OSError:
            return CapabilityCandidate(
                kind=CapabilityKind.SKILL,
                name=skill_dir.name,
                source=str(skill_dir),
                spec=None,
                findings=(Finding(f"skills.{skill_dir.name}.entry", "SKILL.md 不可读"),),
            )

        frontmatter, parse_error = _parse_frontmatter(text)
        raw_name = frontmatter.get("name")
        name = (
            raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else skill_dir.name
        )

        findings: list[Finding] = check_text(text, field=f"skills.{name}.SKILL.md")
        if parse_error is not None:
            findings.append(Finding(f"skills.{name}.SKILL.md", parse_error))
        if not isinstance(raw_name, str) or not raw_name.strip():
            findings.append(Finding(f"skills.{name}.name", "缺少必填字段 name"))

        metadata = frontmatter.get("metadata")
        requires = metadata.get("requires") if isinstance(metadata, Mapping) else None
        spec_dict: dict[str, Any] = {
            "name": name,
            "source": str(skill_dir),
            "entry": "SKILL.md",
            "description": _as_text(frontmatter.get("description")),
            "requires_bins": _str_list(
                requires.get("bins") if isinstance(requires, Mapping) else None
            ),
            "requires_skills": _str_list(
                requires.get("skills") if isinstance(requires, Mapping) else None
            ),
            "permissions": _str_list(
                metadata.get("permissions") if isinstance(metadata, Mapping) else None
            ),
            "network": bool(metadata.get("network")) if isinstance(metadata, Mapping) else False,
            "files": _str_list(metadata.get("files") if isinstance(metadata, Mapping) else None),
        }
        version = frontmatter.get("version")
        if isinstance(version, str):
            spec_dict["version"] = version

        spec: FluxSkill | None = None
        try:
            spec = FluxSkill.from_dict(spec_dict)
        except ValidationError as exc:
            findings.append(Finding(f"skills.{name}", f"标准对象非法：{exc.message}"))

        declared_files = spec_dict["files"]
        for index, declared in enumerate(declared_files):
            findings.extend(check_relative_path(declared, field=f"skills.{name}.files[{index}]"))

        return CapabilityCandidate(
            kind=CapabilityKind.SKILL,
            name=name,
            source=str(skill_dir),
            spec=spec if not findings else None,
            findings=tuple(findings),
        )


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str | None]:
    """解析 SKILL.md 的 YAML frontmatter。

    错误信息固定成标签，不携带 YAML 解析器的原始输出——它可能回显疑似内容。
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, "缺少 frontmatter（SKILL.md 必须以 --- 开头）"
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None:
        return {}, "frontmatter 未闭合"
    try:
        loaded = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError:
        return {}, "frontmatter YAML 解析失败"
    if not isinstance(loaded, Mapping):
        return {}, "frontmatter 必须是对象"
    return dict(loaded), None


# --- Connector ---


class ConnectorScanner:
    def __init__(
        self, files: Sequence[str] = DEFAULT_CONNECTOR_FILES, *, base_dir: Path | None = None
    ) -> None:
        self._files = tuple(files)
        self._base_dir = base_dir or Path.cwd()

    def scan(self) -> ScanOutcome:
        candidates: list[CapabilityCandidate] = []
        sources: list[ScanSourceReport] = []
        for pattern in self._files:
            paths = self._expand(pattern)
            if not paths:
                sources.append(ScanSourceReport(pattern, "connector", "missing", 0))
                continue
            for path in paths:
                found, status = self._scan_file(path)
                candidates.extend(found)
                sources.append(ScanSourceReport(str(path), "connector", status, len(found)))
        candidates.sort(key=lambda candidate: (candidate.name, candidate.source))
        return candidates, sources

    def _expand(self, pattern: str) -> list[Path]:
        if pattern.startswith("~"):
            expanded = os.path.expanduser(pattern)
        elif os.path.isabs(pattern):
            expanded = pattern
        else:
            expanded = str(self._base_dir / pattern)
        return [Path(match) for match in sorted(glob.glob(expanded)) if Path(match).is_file()]

    def _scan_file(self, path: Path) -> tuple[list[CapabilityCandidate], str]:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return [], "unreadable"
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return [], "invalid"
        section = _servers_section(_flavor_for(path), payload)
        if section is None:
            return [], "invalid"
        candidates = [
            self._candidate(_flavor_for(path), str(name), entry, source=path)
            for name, entry in sorted(section.items())
        ]
        return candidates, "scanned"

    def _candidate(
        self, flavor: str, name: str, entry: Any, *, source: Path
    ) -> CapabilityCandidate:
        field = f"connectors.{name}"
        if not isinstance(entry, Mapping):
            return CapabilityCandidate(
                kind=CapabilityKind.CONNECTOR,
                name=name,
                source=str(source),
                spec=None,
                findings=(Finding(field, "Connector 声明必须是对象"),),
            )

        findings: list[Finding] = []
        # 整段扫描兜底：覆盖我们没显式清洗的任意字段（例如顶层 "apiKey"）
        findings.extend(check_text(json.dumps(entry, ensure_ascii=False, default=str), field=field))

        transport = _transport(entry)
        command = _command(entry)
        url = entry.get("url")
        if isinstance(url, str):
            findings.extend(check_url(url, field=f"{field}.url"))
        elif url is not None:
            findings.append(Finding(f"{field}.url", "url 必须是字符串"))
        if command:
            findings.extend(check_command(command, field=f"{field}.command"))

        env_findings, env_keys, env_refs = _scrub_values(
            _str_map(entry.get("env") or entry.get("environment")), field=f"{field}.env"
        )
        header_findings, header_keys, header_refs = _scrub_values(
            _str_map(entry.get("headers")), field=f"{field}.headers"
        )
        findings.extend(env_findings)
        findings.extend(header_findings)

        configuration: dict[str, Any] = {
            "env_keys": env_keys,
            "header_keys": header_keys,
            "env_refs": env_refs,
            "header_refs": header_refs,
        }
        declared_type = entry.get("type")
        if isinstance(declared_type, str):
            configuration["declared_type"] = declared_type
        auth_policy = entry.get("auth_policy")
        if isinstance(auth_policy, str):
            configuration["auth_policy"] = auth_policy

        spec_dict: dict[str, Any] = {
            "name": name,
            "source": str(source),
            "transport": transport,
            "description": _as_text(entry.get("description")),
            "command": list(command) if command else None,
            "url": url if isinstance(url, str) else None,
            "actions": _str_list(entry.get("actions")),
            "required_permissions": _str_list(
                entry.get("required_permissions") or entry.get("permissions")
            ),
            "network": bool(entry.get("network", transport == "remote")),
            "configuration": configuration,
        }
        version = entry.get("version")
        if isinstance(version, str):
            spec_dict["version"] = version

        spec: FluxConnector | None = None
        try:
            spec = FluxConnector.from_dict(spec_dict)
        except ValidationError as exc:
            findings.append(Finding(field, f"标准对象非法：{exc.message}"))

        return CapabilityCandidate(
            kind=CapabilityKind.CONNECTOR,
            name=name,
            source=str(source),
            spec=spec if not findings else None,
            findings=tuple(findings),
        )


def _flavor_for(path: Path) -> str:
    if path.name == "opencode.json":
        return "opencode"
    if path.name == "connector.json":
        return "plugin"
    return "mcp"


def _servers_section(flavor: str, payload: Any) -> Mapping[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    key = {"opencode": "mcp", "mcp": "mcpServers", "plugin": "connectors"}[flavor]
    section = payload.get(key)
    return section if isinstance(section, Mapping) else None


def _transport(entry: Mapping[str, Any]) -> str:
    declared = entry.get("type")
    if isinstance(declared, str):
        lowered = declared.lower()
        if lowered in ("remote", "http", "sse", "streamable-http", "streamable_http"):
            return "remote"
        if lowered in ("local", "stdio", "command", "process"):
            return "stdio"
        if lowered in ("oauth", "declared", "plugin"):
            return "declared"
    if entry.get("url"):
        return "remote"
    if entry.get("command"):
        return "stdio"
    return "declared"


def _command(entry: Mapping[str, Any]) -> tuple[str, ...] | None:
    raw = entry.get("command")
    parts: list[str] = []
    if isinstance(raw, str):
        parts = [raw]
    elif isinstance(raw, (list, tuple)) and all(isinstance(item, str) for item in raw):
        parts = list(raw)
    else:
        return None
    args = entry.get("args")
    if isinstance(args, (list, tuple)) and all(isinstance(item, str) for item in args):
        parts += list(args)
    return tuple(parts) if parts else None


def _scrub_values(
    raw: Mapping[str, str], *, field: str
) -> tuple[list[Finding], list[str], dict[str, str]]:
    """清洗一组 env / header：只留键名与安全的 ${VAR} 引用，明文值不保留。"""
    findings: list[Finding] = []
    keys = sorted(raw)
    refs: dict[str, str] = {}
    for key, value in raw.items():
        findings.extend(check_text(value, field=f"{field}.{key}"))
        if is_safe_reference(value):
            refs[key] = value
    return findings, keys, refs


def _str_map(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items() if isinstance(item, str)}


def _str_list(value: Any) -> list[str]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, str)]


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


# --- Agent ---


class AgentScanner:
    """把 AgentInstallation 行只读投影成 FluxAgent；扫描即触发一次安装事实刷新。"""

    def __init__(self, installations: InstallationService) -> None:
        self._installations = installations

    async def scan(self) -> ScanOutcome:
        rows = await self._installations.scan()
        candidates = sorted(
            (self._candidate(row) for row in rows), key=lambda candidate: candidate.name
        )
        sources = [ScanSourceReport("agent_installations", "agent", "scanned", len(candidates))]
        return candidates, sources

    def _candidate(self, row: AgentInstallation) -> CapabilityCandidate:
        name = row.name
        findings: list[Finding] = []
        if row.status == AgentInstallStatus.NOT_INSTALLED.value:
            findings.append(
                Finding(f"agents.{name}.install_status", "Agent 未安装（NOT_INSTALLED），不可导入")
            )
        if not row.executable:
            findings.append(Finding(f"agents.{name}.executable", "缺少可执行文件"))
        spec_dict: dict[str, Any] = {
            "name": name,
            "source": row.source or "path",
            "executable": row.executable or "",
            "install_status": row.status,
            "path": row.path,
            "capabilities": list(row.capabilities or []),
            "auth_status": row.auth_status or "unknown",
        }
        if row.version:
            spec_dict["version"] = row.version
        spec = None
        try:
            spec = FluxAgent.from_dict(spec_dict)
        except ValidationError as exc:
            findings.append(Finding(f"agents.{name}", f"标准对象非法：{exc.message}"))
        return CapabilityCandidate(
            kind=CapabilityKind.AGENT,
            name=name,
            source=row.executable or row.name,
            spec=spec if not findings else None,
            findings=tuple(findings),
        )


__all__ = [
    "AgentScanner",
    "CapabilityCandidate",
    "CapabilityScanReport",
    "ConnectorScanner",
    "DEFAULT_CONNECTOR_FILES",
    "DEFAULT_SKILL_ROOTS",
    "ScanSourceReport",
    "SkillScanner",
]
