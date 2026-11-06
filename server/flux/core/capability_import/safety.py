"""轻量安全检查：deny-by-default（批次③ §5）。

导入是把"外部内容"带进 Flux 的入口之一，所以这里的默认立场是**拒绝**，
只有明确可判安全的样本才放行到标准对象；每一条拦截只回标签与字段路径，
**绝不回显疑似值本身**——报告、日志、上下文里都不允许出现密钥原文。

覆盖四类：
- 密钥 / 令牌：复用记忆红线 `find_secret`（只回标签），再补 Bearer 明文与
  x-api-key 赋值两类导入面常见形态；占位符与环境变量引用（`${VAR}`、`<…>`）放行；
- 可疑命令：curl/wget 管道进 shell、base64 解码执行、rm -rf、sudo、eval、
  /dev/tcp 反向连接、nc -e、mkfifo；
- 路径逃逸：绝对路径、`..` 上跳、空路径；
- 完整性 / 协议：别处（Scanner 与 Service）负责"必填缺失、SKILL.md 不存在、
  NOT_INSTALLED 不可导入"，URL 协议检查在本模块。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from flux.core.memory.secrets import find_secret

# --- 结果 ---


@dataclass(frozen=True)
class Finding:
    """一条拦截/告警。`field` 是字段路径（如 `connectors.x.headers`），`reason` 是标签。"""

    field: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"field": self.field, "reason": self.reason}


@dataclass(frozen=True)
class SafetyVerdict:
    findings: tuple[Finding, ...] = ()

    @property
    def blocked(self) -> bool:
        return bool(self.findings)

    def to_dict(self) -> dict[str, object]:
        return {"blocked": self.blocked, "findings": [f.to_dict() for f in self.findings]}


# --- 密钥 / 令牌 ---

#: 记忆红线之外的导入面专有形态；捕获组最后一个为疑似值，只用于判断，不外传。
_EXTRA_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Bearer 明文令牌", re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._~+/=-]{20,})")),
    (
        "x-api-key / x-auth-token 带赋值",
        re.compile(r"(?i)\b(x-api-key|x-auth-token)\b\s*[:=]\s*[\"']?([A-Za-z0-9+/=_\-.]{8,})"),
    ),
)

#: 占位符特征：文档 / 示例里"这里填你的 key"不该被拦。
_PLACEHOLDER_MARKERS = (
    "<",
    ">",
    "${",
    "{{",
    "your",
    "example",
    "sample",
    "placeholder",
    "changeme",
    "xxxx",
    "****",
    "...",
)

_ENV_REFERENCE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Za-z_][A-Za-z0-9_]*")
_ALNUM_RUN = re.compile(r"[A-Za-z0-9]{8,}")


def looks_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


def is_safe_reference(value: str) -> bool:
    """值是"环境变量引用 + 占位文字"时才安全（如 `Bearer ${TOKEN}`）。

    若去引用、去占位后还残留 8 位以上字母数字，视为夹带明文，不给放行。
    """
    if not _ENV_REFERENCE.search(value):
        return False
    residue = _ENV_REFERENCE.sub(" ", value).lower()
    for marker in _PLACEHOLDER_MARKERS:
        residue = residue.replace(marker, " ")
    return _ALNUM_RUN.search(residue) is None


def check_text(text: str, *, field: str) -> list[Finding]:
    """扫描一段文本里的疑似凭证。返回标签与字段路径，不回显匹配内容。"""
    findings: list[Finding] = []
    label = find_secret(text)
    if label is not None:
        findings.append(Finding(field, f"疑似密钥 / 令牌（{label}）"))
    for extra_label, pattern in _EXTRA_SECRET_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        captured = match.group(match.lastindex) if match.lastindex else match.group(0)
        if looks_placeholder(captured):
            continue
        findings.append(Finding(field, f"疑似令牌（{extra_label}）"))
    return _dedupe(findings)


# --- 可疑命令 ---

_SUSPICIOUS_COMMAND_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "curl/wget 管道进 shell",
        re.compile(
            r"(?i)\b(?:curl|wget)\b[^|;&<>]*\|\s*(?:sudo\s+)?(?:bash|sh|zsh|dash|ksh|fish)\b"
        ),
    ),
    (
        "base64 解码后执行",
        re.compile(r"(?i)\bbase64\b[^|;&]*\|\s*(?:sudo\s+)?(?:bash|sh|zsh|dash|ksh|fish)\b"),
    ),
    ("递归强删", re.compile(r"(?i)\brm\s+(?:-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r)\b")),
    ("sudo 提权", re.compile(r"(?i)(?:^|[;&|]|\s)sudo\b")),
    ("eval 动态执行", re.compile(r"(?i)\beval\b")),
    ("/dev/tcp 反向连接", re.compile(r"/dev/tcp/")),
    ("nc -e 反弹 shell", re.compile(r"(?i)\bnc\b[^|;&]*\s-e\b")),
    ("mkfifo 命名管道", re.compile(r"(?i)\bmkfifo\b")),
)


def check_command(command: Sequence[str], *, field: str) -> list[Finding]:
    joined = " ".join(command)
    findings: list[Finding] = []
    for label, pattern in _SUSPICIOUS_COMMAND_PATTERNS:
        if pattern.search(joined):
            findings.append(Finding(field, f"可疑命令模式：{label}"))
    return _dedupe(findings)


# --- 路径 ---

_ABSOLUTE_PATH = re.compile(r"^(?:/|~|[A-Za-z]:[\\/])")


def check_relative_path(value: str, *, field: str) -> list[Finding]:
    """声明的相对路径不允许逃出所在目录。"""
    findings: list[Finding] = []
    if not value or not value.strip():
        return [Finding(field, "路径为空")]
    if "\x00" in value:
        findings.append(Finding(field, "路径含空字节"))
    if _ABSOLUTE_PATH.match(value):
        findings.append(Finding(field, "绝对路径逃逸"))
    if ".." in re.split(r"[\\/]", value):
        findings.append(Finding(field, "相对路径逃逸（..）"))
    return _dedupe(findings)


# --- URL ---


def check_url(value: str, *, field: str) -> list[Finding]:
    try:
        parts = urlsplit(value)
    except ValueError:
        return [Finding(field, "URL 无法解析")]
    if parts.scheme not in ("http", "https"):
        return [Finding(field, f"URL 协议必须是 http(s)（实际 {parts.scheme or '空'}）")]
    if not parts.netloc:
        return [Finding(field, "URL 缺少主机名")]
    return []


def _dedupe(findings: list[Finding]) -> list[Finding]:
    seen: set[tuple[str, str]] = set()
    unique: list[Finding] = []
    for finding in findings:
        key = (finding.field, finding.reason)
        if key in seen:
            continue
        seen.add(key)
        unique.append(finding)
    return unique


__all__ = [
    "Finding",
    "SafetyVerdict",
    "check_command",
    "check_relative_path",
    "check_text",
    "check_url",
    "is_safe_reference",
    "looks_placeholder",
]
