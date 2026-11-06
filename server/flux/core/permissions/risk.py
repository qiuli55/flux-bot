"""终端命令风险分类（2026-10-25 三模式审批闸门的数据源）。

三条硬规则：

1. **fail-closed**：没见过的二进制、含命令替换/管道的复合命令，一律按高风险处理——
   分类器不认识它，就意味着平台不为它的副作用背书；
2. **受保护优先**：命令里只要出现密钥文件、受保护配置或系统服务操作，直接升级为
   protected，优先级高于 high（任何模式都要用户逐次点头）；
3. **低风险白名单是封闭集合**：只有明确列出的只读命令/测试命令自动放行，
   其余全部走审批——宁可多问一次，也不默认放行。

本模块只做纯字符串判定，不碰数据库、不执行命令；命令全文也不落日志（只记首词与规则）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

LEVEL_LOW = "low"
LEVEL_HIGH = "high"
LEVEL_PROTECTED = "protected"

#: 只读/测试类命令（首词匹配）。`find` 另判 -delete/-exec；`git` / `npm` 等按子命令判定。
LOW_RISK_COMMANDS: frozenset[str] = frozenset(
    {
        "ls",
        "pwd",
        "cat",
        "head",
        "tail",
        "grep",
        "rg",
        "wc",
        "stat",
        "du",
        "df",
        "tree",
        "file",
        "which",
        "whereis",
        "echo",
        "printf",
        "date",
        "whoami",
        "uname",
        "hostname",
        "id",
        "ps",
        "sleep",
        "true",
        "false",
        "make",
        "pytest",
        "ruff",
        "eslint",
        "tsc",
        "prettier",
        "black",
        "mypy",
        "cargo",
        "go",
        "gradle",
        "gradlew",
        "mvn",
    }
)

#: 测试/构建类 python 调用（`python -m pytest`、`python3 -m ruff` 等）
_PYTHON_RE = re.compile(r"^python[0-9.]*$")
_PYTHON_LOW_MODULES: frozenset[str] = frozenset(
    {"pytest", "ruff", "mypy", "unittest", "compileall"}
)

#: 高风险首词：破坏性文件操作、网络、包管理、容器、进程与系统控制
HIGH_RISK_COMMANDS: frozenset[str] = frozenset(
    {
        "rm",
        "rmdir",
        "mv",
        "cp",
        "ln",
        "touch",
        "mkdir",
        "dd",
        "truncate",
        "shred",
        "chmod",
        "chown",
        "chgrp",
        "mkfs",
        "mount",
        "umount",
        "tee",
        "kill",
        "killall",
        "pkill",
        "xargs",
        "curl",
        "wget",
        "ssh",
        "scp",
        "sftp",
        "rsync",
        "nc",
        "netcat",
        "docker",
        "kubectl",
        "terraform",
        "helm",
        "apt",
        "apt-get",
        "yum",
        "dnf",
        "apk",
        "brew",
        "pip",
        "pip3",
        "npm",
        "pnpm",
        "yarn",
        "npx",
        "uv",
        "poetry",
        "bash",
        "sh",
        "zsh",
        "eval",
        "exec",
        "source",
        "sudo",
        "su",
        "git",
        "sed",
        "awk",
        "find",
        "systemctl",
        "service",
        "reboot",
        "shutdown",
        "poweroff",
        "halt",
        "init",
        "crontab",
        "at",
        "useradd",
        "userdel",
        "usermod",
        "passwd",
    }
)

#: 受保护路径/对象（命令里出现即 protected，任何模式都要审批）
_PROTECTED_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(^|[\s'\"=:])/root/\.ssh(/|\s|$)"), "/root/.ssh"),
    (re.compile(r"(^|[\s'\"=:/])id_(?:rsa|dsa|ecdsa|ed25519)(?:\b|_)"), "SSH 私钥"),
    (re.compile(r"\.(?:pem|key|p12|pfx|keystore|jks)(?:\s|$|['\"])"), "证书/私钥文件"),
    (re.compile(r"/opt/flux/server/flux-server\.env"), "Flux 服务端配置文件"),
    (re.compile(r"(^|[\s'\"=:/])/etc/systemd(/|\s|$)"), "/etc/systemd"),
    (re.compile(r"(^|[\s'\"=:/])/etc/nginx(/|\s|$)"), "/etc/nginx"),
    (re.compile(r"(^|[\s'\"=:/])/etc/shadow(\.|$|\s)"), "/etc/shadow"),
    (re.compile(r"(^|[\s'\"=:/])/etc/sudoers(\.|$|\s)"), "/etc/sudoers"),
)

#: 受保护服务操作（首词）
PROTECTED_COMMANDS: frozenset[str] = frozenset(
    {"systemctl", "service", "reboot", "shutdown", "poweroff", "halt", "init"}
)

#: `.env` 类文件（模板/示例后缀除外）
_ENV_TEMPLATE_SUFFIXES: frozenset[str] = frozenset(
    {"example", "sample", "template", "dist", "defaults"}
)
_TOKEN_RE = re.compile(r"[^\s'\"`]+")

#: 命令替换 / 进程替换：无法静态判定内容，按高风险
_SUBSTITUTION_RE = re.compile(r"\$\(|`|<\(|>\(")
#: 输出重定向到真实文件（写盘）；`>` 后紧跟 /dev/null 或 &1/&2 不算
_REDIRECT_RE = re.compile(r"(?<![0-9])>>?\s*(?P<target>[^\s;|&]+)")

_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*\s+")


@dataclass(frozen=True)
class RiskReport:
    """一次命令分类的结论：等级、命中规则与人类可读理由。"""

    level: str
    rule: str
    reason: str

    @property
    def requires_approval_in_normal(self) -> bool:
        return self.level in (LEVEL_HIGH, LEVEL_PROTECTED)

    @property
    def requires_approval_always(self) -> bool:
        return self.level == LEVEL_PROTECTED


def _first_word(segment: str) -> tuple[str, list[str]]:
    """取命令首词（跳过 `VAR=value` 前缀），返回 (首词, 余下参数)。"""
    text = segment.strip()
    while True:
        match = _ENV_ASSIGN_RE.match(text)
        if match is None:
            break
        text = text[match.end() :].strip()
    parts = text.split()
    if not parts:
        return "", []
    return parts[0], parts[1:]


def _basename(word: str) -> str:
    return PurePosixPath(word.replace("\\", "/")).name.lower()


def _protected_hit(command: str) -> str | None:
    """受保护路径/文件命中即返回标签；`.env` 按文件名精确判定，避免误伤 environment 等词。"""
    for pattern, label in _PROTECTED_PATTERNS:
        if pattern.search(command):
            return label
    for token in _TOKEN_RE.findall(command):
        name = _basename(token)
        if name == ".env":
            return ".env"
        if name.startswith(".env."):
            suffix = name.rsplit(".", 1)[-1]
            if suffix not in _ENV_TEMPLATE_SUFFIXES:
                return ".env"
    return None


def _redirect_targets(command: str) -> list[str]:
    targets: list[str] = []
    for match in _REDIRECT_RE.finditer(command):
        target = match.group("target").strip("'\"")
        if not target or target in ("/dev/null", "&1", "&2", "1", "2"):
            continue
        targets.append(target)
    return targets


def _classify_segment(segment: str) -> tuple[str, str, str] | None:
    """单个命令段的风险；返回 (level, rule, reason)，None = 低风险。"""
    word, args = _first_word(segment)
    if not word:
        return None
    name = _basename(word)

    if name in PROTECTED_COMMANDS or "--daemon-reload" in args or "daemon-reload" in args:
        return (LEVEL_PROTECTED, "protected_service", f"系统服务操作 {name} 会改动服务状态")

    if name in HIGH_RISK_COMMANDS:
        # 少数命令有明确的只读用法，单独放行，其余保持高风险
        if name == "git":
            return _classify_git(args)
        if name in ("npm", "pnpm", "yarn"):
            return _classify_node_package(name, args)
        if name in ("sed",) and args[:1] and args[0].startswith("-n"):
            return None
        if name == "awk":
            return (LEVEL_HIGH, "awk", "awk 可执行任意语句并写文件")
        if name == "find":
            if "-delete" in args or "-exec" in args:
                return (LEVEL_HIGH, "find_mutation", "find 的 -delete/-exec 会改文件或执行命令")
            return None
        if name in ("cp", "ln", "touch", "mkdir"):
            return (LEVEL_HIGH, name, f"{name} 会写工作区文件")
        return (LEVEL_HIGH, name, f"{name} 属于高风险命令")

    if name in LOW_RISK_COMMANDS:
        return _classify_low(name, args)

    if _PYTHON_RE.match(name):
        if len(args) >= 2 and args[0] == "-m" and args[1] in _PYTHON_LOW_MODULES:
            return None
        return (LEVEL_HIGH, "python", "python 可直接执行任意代码")

    return (LEVEL_HIGH, "unknown_command", f"未识别的命令 {name or word}，按高风险处理")


def _classify_low(name: str, args: list[str]) -> tuple[str, str, str] | None:
    if name == "cargo":
        return (
            None
            if args[:1] and args[0] in ("test", "build", "check", "clippy", "fmt")
            else (
                LEVEL_HIGH,
                "cargo",
                "cargo 仅 test/build/check/clippy/fmt 自动放行",
            )
        )
    if name == "go":
        return (
            None
            if args[:1] and args[0] in ("test", "build", "vet")
            else (
                LEVEL_HIGH,
                "go",
                "go 仅 test/build/vet 自动放行",
            )
        )
    if name in ("gradle", "gradlew"):
        return (
            None
            if any(arg in ("build", "test", "assemble", "check") for arg in args)
            else (
                LEVEL_HIGH,
                "gradle",
                "gradle 仅 build/test/assemble/check 自动放行",
            )
        )
    if name == "mvn":
        return (
            None
            if args[:1] and args[0] in ("test", "verify", "package")
            else (
                LEVEL_HIGH,
                "mvn",
                "mvn 仅 test/verify/package 自动放行",
            )
        )
    if name == "make":
        # make 可以执行任意 recipe；只有不带目标（默认测试目标）或明确测试目标才放行
        if any(arg in ("install", "deploy", "release", "publish") for arg in args):
            return (LEVEL_HIGH, "make", "make install/deploy 会改系统或发布")
        return None
    return None


def _classify_node_package(name: str, args: list[str]) -> tuple[str, str, str] | None:
    """npm/pnpm/yarn：只放行 test / run test / run build / run lint，安装与发布高风险。"""
    if not args:
        return (LEVEL_HIGH, name, f"{name} 缺少子命令，按高风险处理")
    if args[0] == "test":
        return None
    if args[0] == "run" and len(args) >= 2 and args[1] in ("test", "build", "lint", "typecheck"):
        return None
    return (LEVEL_HIGH, name, f"{name} {args[0]} 会安装/发布依赖或改工作区")


def _classify_git(args: list[str]) -> tuple[str, str, str] | None:
    """git 的只读子命令放行；提交/推送/重置/清理/切分支一律高风险。"""
    if not args:
        return (LEVEL_HIGH, "git", "git 缺少子命令，按高风险处理")
    sub = args[0]
    if sub in ("status", "diff", "log", "show", "blame", "describe", "ls-files", "rev-parse"):
        return None
    if sub == "remote":
        if len(args) == 1 or args[1] in ("-v", "show", "get-url"):
            return None
        return (LEVEL_HIGH, "git_remote", "修改 git remote 会改变推送目标")
    if sub == "branch":
        if len(args) == 1 or all(arg.startswith("-") for arg in args[1:]):
            return None
        return (LEVEL_HIGH, "git_branch", "新建/删除分支会改动仓库状态")
    if sub == "tag" and (len(args) == 1 or args[1] in ("-l", "--list")):
        return None
    return (LEVEL_HIGH, "git_mutation", f"git {sub} 会改动仓库历史或工作区")


def classify_command(command: str) -> RiskReport:
    """把一条 shell 命令分类为 low / high / protected。"""
    raw = (command or "").strip()
    if not raw:
        return RiskReport(LEVEL_HIGH, "empty_command", "空命令，按高风险处理")

    protected = _protected_hit(raw)
    if protected is not None:
        return RiskReport(
            LEVEL_PROTECTED,
            "protected_path",
            f"命令涉及受保护对象（{protected}），任何模式都需要你逐次确认",
        )

    if _SUBSTITUTION_RE.search(raw):
        return RiskReport(
            LEVEL_HIGH,
            "command_substitution",
            "命令含命令替换/进程替换，实际执行内容无法静态判定",
        )

    targets = _redirect_targets(raw)
    if targets:
        return RiskReport(
            LEVEL_HIGH,
            "redirect",
            f"命令会写文件（重定向到 {targets[0]}），属于工作区改动",
        )

    segments = [part for part in re.split(r"\|\||&&|;|\||\n", raw) if part.strip()]
    if not segments:
        return RiskReport(LEVEL_HIGH, "empty_command", "空命令，按高风险处理")

    highest: RiskReport | None = None
    for segment in segments:
        outcome = _classify_segment(segment)
        if outcome is None:
            continue
        level, rule, reason = outcome
        report = RiskReport(level, rule, reason)
        if level == LEVEL_PROTECTED:
            return report
        if highest is None:
            highest = report
    return highest or RiskReport(LEVEL_LOW, "low_risk", "只读/测试类命令，自动放行")


__all__ = [
    "HIGH_RISK_COMMANDS",
    "LEVEL_HIGH",
    "LEVEL_LOW",
    "LEVEL_PROTECTED",
    "LOW_RISK_COMMANDS",
    "PROTECTED_COMMANDS",
    "RiskReport",
    "classify_command",
]
