"""技能包格式：与 `capability_import` 的 FluxSkill / SKILL.md 约定对齐。

一个技能包就是一份 UTF-8 的 SKILL.md 等价文档：

```
---
name: disk-check
description: 巡检磁盘
version: "1.0"
entry: SKILL.md
requires_bins: [df]
permissions: [shell]
network: false
---
Markdown 正文，注入对话上下文的就是这一段。
```

- frontmatter 必须由 `---` 包围，字段接受 FluxSkill 集合；未知字段忽略
  （真实 SKILL.md 常带 license / allowed-tools 等展示性字段，不影响执行语义）；
- 也接受 JSON 形态（`{name, description, version, body, ...}`），由服务层用
  `build_package` 归一成同一个对象；
- 只做结构校验，不执行任何内容：正文大小有上限，`entry` / `files` 只允许
  相对路径（复用 capability_import.safety 的路径检查）。

本模块刻意不导入 `capability_import.scanners`（它依赖 agent_runtime），
frontmatter 解析在这里自包含，只复用标准对象与安全检查这两个纯模块。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import yaml

from flux.core.capability_import.safety import check_relative_path
from flux.core.capability_import.standards import FluxSkill
from flux.errors import ValidationError

#: 单个技能正文的字符上限（64 KiB）：技能是提示词，不是数据仓库。
MAX_BODY_CHARS = 64 * 1024
MAX_NAME_CHARS = 128
MAX_DESCRIPTION_CHARS = 4_000
MAX_VERSION_CHARS = 64
MAX_PATH_CHARS = 512
MAX_LIST_ITEMS = 64
MAX_LIST_ITEM_CHARS = 256

#: 技能包来源标记：与扫描导入的本地路径来源区分开。
SOURCE = "skill-library"

#: 名称按 ASCII slug 校验：不允许路径分隔符、空白与 Unicode 混淆字符。
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_PACKAGE_KEYS = frozenset(
    {
        "name",
        "description",
        "version",
        "body",
        "entry",
        "requires_bins",
        "requires_skills",
        "permissions",
        "network",
        "files",
    }
)


@dataclass(frozen=True)
class SkillPackageDocument:
    """一个已校验的技能包（元数据 + 指令正文）。"""

    name: str
    description: str
    version: str | None
    entry: str
    requires_bins: tuple[str, ...]
    requires_skills: tuple[str, ...]
    permissions: tuple[str, ...]
    network: bool
    files: tuple[str, ...]
    body: str

    def to_spec(self) -> dict[str, Any]:
        """归一成 FluxSkill 标准形状（source 固定为技能库）。"""
        return FluxSkill.from_dict(
            {
                "name": self.name,
                "source": SOURCE,
                "description": self.description,
                "version": self.version,
                "entry": self.entry,
                "requires_bins": list(self.requires_bins),
                "requires_skills": list(self.requires_skills),
                "permissions": list(self.permissions),
                "network": self.network,
                "files": list(self.files),
            }
        ).to_dict()

    def to_dict(self) -> dict[str, Any]:
        """API 形状：元数据扁平展开，正文单独放 body。"""
        return {**self.to_spec(), "body": self.body, "body_chars": len(self.body)}

    def to_document(self) -> str:
        """渲染回 SKILL.md 形态（导出 / 测试 round-trip 用）。"""
        frontmatter: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
        }
        if self.version is not None:
            frontmatter["version"] = self.version
        frontmatter["entry"] = self.entry
        if self.requires_bins:
            frontmatter["requires_bins"] = list(self.requires_bins)
        if self.requires_skills:
            frontmatter["requires_skills"] = list(self.requires_skills)
        if self.permissions:
            frontmatter["permissions"] = list(self.permissions)
        frontmatter["network"] = self.network
        if self.files:
            frontmatter["files"] = list(self.files)
        header = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
        return f"---\n{header}\n---\n\n{self.body}\n"


def _fail(message: str, *, field: str) -> ValidationError:
    return ValidationError(message, details={"field": field})


def _required_text(value: Any, *, field: str, max_chars: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _fail(f"技能缺少必填字段 {field}（非空字符串）", field=field)
    text = value.strip()
    if len(text) > max_chars:
        raise _fail(f"技能 {field} 超过 {max_chars} 字符上限", field=field)
    return text


def _optional_text(
    value: Any, *, field: str, max_chars: int, default: str | None = None
) -> str | None:
    if value is None:
        return default
    if not isinstance(value, str):
        raise _fail(f"技能 {field} 必须是字符串", field=field)
    text = value.strip()
    if len(text) > max_chars:
        raise _fail(f"技能 {field} 超过 {max_chars} 字符上限", field=field)
    return text


def _version(value: Any) -> str | None:
    # YAML 里 `version: 1.0` 会被解析成数字，按字符串处理更符合文档直觉。
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    text = _optional_text(value, field="version", max_chars=MAX_VERSION_CHARS)
    return text or None


def _boolean(value: Any, *, field: str) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise _fail(f"技能 {field} 必须是布尔值", field=field)
    return value


def _string_list(value: Any, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise _fail(f"技能 {field} 必须是字符串数组", field=field)
    items: list[str] = []
    for raw in value:
        if not isinstance(raw, str) or not raw.strip():
            raise _fail(f"技能 {field} 必须是非空字符串数组", field=field)
        item = raw.strip()
        if len(item) > MAX_LIST_ITEM_CHARS:
            raise _fail(f"技能 {field} 单项超过 {MAX_LIST_ITEM_CHARS} 字符上限", field=field)
        items.append(item)
    if len(items) > MAX_LIST_ITEMS:
        raise _fail(f"技能 {field} 最多 {MAX_LIST_ITEMS} 项", field=field)
    return tuple(items)


def _relative_path(value: Any, *, field: str, default: str) -> str:
    text = _optional_text(value, field=field, max_chars=MAX_PATH_CHARS, default=default)
    assert text is not None
    if not text:
        raise _fail(f"技能 {field} 不能为空", field=field)
    findings = check_relative_path(text, field=field)
    if findings:
        raise _fail(f"技能 {field} 路径非法：{findings[0].reason}", field=field)
    return text


def _relative_paths(value: Any, *, field: str) -> tuple[str, ...]:
    items = _string_list(value, field=field)
    for item in items:
        findings = check_relative_path(item, field=field)
        if findings:
            raise _fail(f"技能 {field} 路径非法：{findings[0].reason}", field=field)
    return items


def _clean_name(value: Any) -> str:
    name = _required_text(value, field="name", max_chars=MAX_NAME_CHARS)
    if not _NAME_RE.match(name):
        raise _fail(
            "技能 name 只能包含 ASCII 字母、数字、点、下划线与连字符，且以字母或数字开头",
            field="name",
        )
    return name


def _clean_body(value: Any) -> str:
    if not isinstance(value, str):
        raise _fail("技能缺少必填字段 body（Markdown 指令正文）", field="body")
    body = value.strip()
    if not body:
        raise _fail("技能 body 不能为空", field="body")
    if len(body) > MAX_BODY_CHARS:
        raise _fail(f"技能 body 超过 {MAX_BODY_CHARS} 字符上限", field="body")
    return body


def build_package(data: Mapping[str, Any]) -> SkillPackageDocument:
    """从 JSON 形态构建技能包；所有字段校验一次完成。"""
    if not isinstance(data, Mapping):
        raise _fail("技能包必须是对象", field="body")
    return SkillPackageDocument(
        name=_clean_name(data.get("name")),
        description=_optional_text(
            data.get("description"),
            field="description",
            max_chars=MAX_DESCRIPTION_CHARS,
            default="",
        )
        or "",
        version=_version(data.get("version")),
        entry=_relative_path(data.get("entry"), field="entry", default="SKILL.md"),
        requires_bins=_string_list(data.get("requires_bins"), field="requires_bins"),
        requires_skills=_string_list(data.get("requires_skills"), field="requires_skills"),
        permissions=_string_list(data.get("permissions"), field="permissions"),
        network=_boolean(data.get("network"), field="network"),
        files=_relative_paths(data.get("files"), field="files"),
        body=_clean_body(data.get("body")),
    )


def parse_package_document(text: str) -> SkillPackageDocument:
    """解析一份 SKILL.md 等价文档（YAML frontmatter + Markdown 正文）。"""
    if not isinstance(text, str):
        raise _fail("技能包内容必须是字符串", field="content")
    # 统一换行，避免 Windows 风格的 \r 混进正文
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if not lines or lines[0].strip() != "---":
        raise _fail("技能包缺少 frontmatter（必须以 --- 开头）", field="content")
    end: int | None = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None:
        raise _fail("技能包 frontmatter 未闭合", field="content")
    try:
        loaded = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as exc:
        # YAML 解析器的原始输出可能回显内容，错误信息只给标签
        raise _fail("技能包 frontmatter YAML 解析失败", field="content") from exc
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, Mapping):
        raise _fail("技能包 frontmatter 必须是对象", field="content")

    metadata = loaded.get("metadata")
    if not isinstance(metadata, Mapping):
        metadata = {}

    def flat_or_metadata(key: str, nested: Any = None) -> Any:
        # 平铺字段优先；同时兼容 scanners 使用的 metadata.requires.* 形态
        if loaded.get(key) is not None:
            return loaded[key]
        return metadata.get(key) if nested is None else nested

    requires = metadata.get("requires")
    requires_map = requires if isinstance(requires, Mapping) else {}

    data = {
        "name": loaded.get("name"),
        "description": loaded.get("description"),
        "version": loaded.get("version"),
        "entry": loaded.get("entry"),
        "requires_bins": flat_or_metadata("requires_bins", requires_map.get("bins")),
        "requires_skills": flat_or_metadata("requires_skills", requires_map.get("skills")),
        "permissions": flat_or_metadata("permissions"),
        "network": flat_or_metadata("network"),
        "files": flat_or_metadata("files"),
        "body": "\n".join(lines[end + 1 :]),
    }
    return build_package(data)


__all__ = [
    "MAX_BODY_CHARS",
    "MAX_DESCRIPTION_CHARS",
    "MAX_LIST_ITEMS",
    "MAX_NAME_CHARS",
    "MAX_PATH_CHARS",
    "MAX_VERSION_CHARS",
    "SOURCE",
    "SkillPackageDocument",
    "build_package",
    "parse_package_document",
]
