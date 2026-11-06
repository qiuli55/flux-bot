"""补全上下文的三层聚合（AI_COMPLETION_SPEC §3.2 / §4.1）。

三层各司其职，缺一层都会让补全"看起来像个只会看当前行的玩具"：

- **L0 当前文件**：光标前 120 行 + 后 30 行；超过 3 万字符时保留靠近光标的尾部。
- **L1 邻接文件**：同目录下最多 8 个相关源文件，只取签名（import / 类 / 函数声明），
  总预算 1.2 万字符——让模型知道"这个项目长什么样"，但不把上下文窗口塞满。
- **L2 项目摘要**：`.flux/specs/*.md` 的规范摘要 + 技术栈 + 近期改动，总预算 2000 字符。

所有读取都是只读的；索引缺失（没配工作区、文件不存在、Git 不可用）一律退化为
空上下文而不是报错——补全请求不能因为"项目还没初始化"而失败。
"""

from __future__ import annotations

import asyncio
import hashlib
import posixpath
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flux.config import Settings
from flux.core.git_integration.client import GitClient, GitStatus
from flux.core.project_files.explorer import FileEntry, WorkspaceFileExplorer
from flux.core.project_scanner.scanner import ProjectScanner
from flux.logging import get_logger

logger = get_logger(__name__)

#: L0：光标前 / 后保留的行数
L0_BEFORE_LINES = 120
L0_AFTER_LINES = 30
#: L0：单文件上下文的最大字符数（超出保留靠近光标的尾部）
L0_MAX_CHARS = 30_000

#: L1：最多取多少个邻接文件、单文件签名上限、总预算
L1_MAX_FILES = 8
L1_PER_FILE_CHARS = 3_000
L1_MAX_CHARS = 12_000

#: L2：项目摘要总预算与各分块预算
L2_MAX_CHARS = 2_000
L2_SPEC_CHARS = 1_100
L2_STACK_CHARS = 450
L2_RECENT_CHARS = 450

#: 项目扫描 / Git 状态的缓存 TTL：扫描很贵（最多 2000 个文件），不能每次按键都跑
SCAN_TTL_SECONDS = 60.0
GIT_TTL_SECONDS = 5.0

#: 只把这些扩展名当作"邻接代码文件"；其余（图片 / 文档 / 锁文件）不读
CODE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py",
        ".pyi",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".vue",
        ".svelte",
        ".java",
        ".kt",
        ".kts",
        ".scala",
        ".go",
        ".rs",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".cs",
        ".swift",
        ".m",
        ".mm",
        ".rb",
        ".php",
        ".lua",
        ".dart",
        ".ex",
        ".exs",
        ".erl",
        ".hs",
        ".clj",
        ".sql",
        ".sh",
        ".bash",
        ".zsh",
        ".ps1",
        ".css",
        ".scss",
        ".less",
        ".html",
        ".htm",
        ".xml",
    }
)

#: 视为"声明行"的关键字：用于从邻接文件里抽签名（函数体 / 实现都丢掉）
_DECL_KEYWORDS: tuple[str, ...] = (
    "class",
    "interface",
    "type",
    "enum",
    "struct",
    "trait",
    "impl",
    "record",
    "protocol",
    "extension",
    "object",
    "module",
    "namespace",
    "def",
    "func",
    "fn",
    "function",
    "method",
    "const",
    "let",
    "var",
    "val",
)
_IMPORT_PREFIXES: tuple[str, ...] = (
    "import ",
    "from ",
    "package ",
    "using ",
    "namespace ",
    "include ",
    "require(",
    "export ",
    "#include",
)
#: 声明前的修饰词，抽签名时先剥掉再判断
_MODIFIERS: frozenset[str] = frozenset(
    {
        "export",
        "default",
        "declare",
        "abstract",
        "public",
        "private",
        "protected",
        "internal",
        "static",
        "final",
        "sealed",
        "readonly",
        "async",
        "await",
        "pub",
        "pub(crate)",
        "unsafe",
        "extern",
        "override",
        "virtual",
        "partial",
        "data",
    }
)
#: 抽签名时最多保留多少行
_SIGNATURE_MAX_LINES = 80


def _extension(path: str) -> str:
    return posixpath.splitext(path)[1].lower()


#: 只看文件开头这么多行里的 import：再往后基本都是实现，不是引用
_REFERENCE_SCAN_LINES = 400
#: 标识符提及只扫前 12 万字符，避免超大文件每次按键都做一遍全量正则
_MENTION_SCAN_CHARS = 120_000

_IMPORT_LINE = re.compile(
    r"^\s*(?:import|from|require|#include|include|use|using|package|require_relative)\b",
    re.IGNORECASE,
)
_REFERENCE_SPLIT = re.compile(r"[^0-9A-Za-z_]+")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")

#: import 语句里高频但无区分度的词（语言关键字 / 相对路径符号的残留）
_REFERENCE_NOISE: frozenset[str] = frozenset(
    {
        "import",
        "from",
        "require",
        "include",
        "using",
        "use",
        "package",
        "as",
        "def",
        "module",
        "true",
        "false",
        "none",
        "null",
        "nil",
        "self",
        "this",
        "std",
        "mod",
        "crate",
        "super",
        "public",
        "static",
    }
)


def _reference_tokens(text: str | None) -> set[str]:
    """从当前文件的 import / require 语句里抽出"它引用了哪些模块"。"""
    if not text:
        return set()
    tokens: set[str] = set()
    for line in text.splitlines()[:_REFERENCE_SCAN_LINES]:
        if not _IMPORT_LINE.match(line):
            continue
        for chunk in _REFERENCE_SPLIT.split(line):
            token = chunk.lower()
            if len(token) < 2 or token in _REFERENCE_NOISE or token.isdigit():
                continue
            tokens.add(token)
    return tokens


def _mention_tokens(text: str | None) -> set[str]:
    """当前文件正文出现过的标识符（含 PascalCase / snake_case 的模块名）。"""
    if not text:
        return set()
    return {match.group(0).lower() for match in _IDENTIFIER.finditer(text[:_MENTION_SCAN_CHARS])}


def _is_referenced(name: str, stem: str, references: set[str]) -> bool:
    if not references:
        return False
    return name.lower() in references or (bool(stem) and stem.lower() in references)


def _age_bucket(modified_at: str | None, now: datetime) -> int:
    """文件新鲜度分档：1 小时内 / 1 天内 / 1 周内 / 更旧 → 0/1/2/3。

    近期改过的文件更可能是"正在一起改的那一组"，按它排序比按路径长度更贴近直觉。
    """
    if not modified_at:
        return 3
    try:
        stamp = datetime.fromisoformat(str(modified_at).replace("Z", "+00:00"))
    except ValueError:
        return 3
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    age = (now - stamp).total_seconds()
    if age < 3_600:
        return 0
    if age < 86_400:
        return 1
    if age < 604_800:
        return 2
    return 3


def _signature_excerpt(content: str) -> str:
    """从源码里抽"骨架"：import / 声明行 / 装饰器，丢掉函数体与实现细节。"""
    kept: list[str] = []
    for raw_line in content.splitlines():
        if len(kept) >= _SIGNATURE_MAX_LINES:
            break
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith(("#[", "@", "///", "///", "//!", "#!")):
            kept.append(raw_line.rstrip())
            continue
        if any(stripped.startswith(prefix) for prefix in _IMPORT_PREFIXES):
            kept.append(raw_line.rstrip())
            continue
        # 逐词剥掉 public / static / export default 这类修饰词
        tokens = stripped.split()
        index = 0
        while index < len(tokens) and tokens[index] in _MODIFIERS:
            index += 1
        if index >= len(tokens):
            continue
        head = tokens[index].rstrip(":(")
        if head in _DECL_KEYWORDS:
            # 只保留声明行本身；带 `{` 的也保留，便于模型看出结构
            kept.append(raw_line.rstrip())
    return "\n".join(kept)


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit]


def _tail_truncate(text: str, limit: int) -> str:
    """保留尾部（用于 L0：光标附近的信息比文件开头更重要）。"""
    return text if len(text) <= limit else text[-limit:]


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    expires_at: float
    value: Any


class CompletionContextBuilder:
    """构建 L0 / L1 / L2 三层上下文（带进程内缓存）。"""

    def __init__(
        self,
        *,
        settings: Settings,
        files: WorkspaceFileExplorer,
        scanner: ProjectScanner,
        git_client: GitClient,
    ) -> None:
        self._settings = settings
        self._files = files
        self._scanner = scanner
        self._git_client = git_client
        self._lock = threading.Lock()
        self._scan_cache: dict[str, _CacheEntry] = {}
        self._git_cache: dict[str, _CacheEntry] = {}

    # --- 对外 ---

    async def build(
        self,
        *,
        project_id: str | None,
        path: str | None,
        caret: int | None,
        text: str | None,
        language_id: str | None = None,
    ) -> dict[str, Any]:
        root = self._workspace_root()
        # 当前文件只读一次：L0 切片与 L1 排序（import / 标识符提及）共用同一份原文
        source = text
        if source is None and path and root is not None:
            try:
                source = self._files.read(path=path, workspace_root=str(root)).content
            except Exception as exc:  # noqa: BLE001 - 读不到就退化为空上下文
                logger.debug("ide_completion.l0_read_failed path=%s error=%s", path, exc)
                source = None
        l0 = self._build_l0(path=path, caret=caret, text=source, language_id=language_id, root=root)
        l1 = self._build_l1(path=path, root=root, text=source)
        l2 = await self._build_l2(root=root)
        return {
            "l0": l0,
            "l1": l1,
            "l2": l2,
            "cache_key": self._cache_key(path=path, caret=caret, text=text, l1=l1, l2=l2),
            "project_id": project_id,
        }

    # --- L0 ---

    def _build_l0(
        self,
        *,
        path: str | None,
        caret: int | None,
        text: str | None,
        language_id: str | None,
        root: Path | None,
    ) -> dict[str, Any]:
        source = text
        if source is None and path and root is not None:
            try:
                source = self._files.read(path=path, workspace_root=str(root)).content
            except Exception as exc:  # noqa: BLE001 - 读不到就退化为空上下文
                logger.debug("ide_completion.l0_read_failed path=%s error=%s", path, exc)
                source = None
        if source is None:
            return {
                "text": "",
                "start_line": 0,
                "end_line": 0,
                "caret_line": 0,
                "language_id": language_id or "",
            }

        lines = source.splitlines()
        total = len(lines)
        offset = max(0, min(len(source), int(caret or 0)))
        caret_line = source.count("\n", 0, offset)
        start = max(0, caret_line - L0_BEFORE_LINES)
        end = min(total, caret_line + L0_AFTER_LINES + 1)
        excerpt = "\n".join(lines[start:end])
        # 单行超长（压缩后的 JS 等）时按行丢可能丢不干净，最后退化为保留尾部
        while len(excerpt) > L0_MAX_CHARS and start + 1 < end:
            start += 1
            excerpt = "\n".join(lines[start:end])
        excerpt = _tail_truncate(excerpt, L0_MAX_CHARS)
        return {
            "text": excerpt,
            "start_line": start,
            "end_line": end,
            "caret_line": caret_line,
            "language_id": language_id or "",
        }

    # --- L1 ---

    def _build_l1(
        self, *, path: str | None, root: Path | None, text: str | None = None
    ) -> list[dict[str, str]]:
        if not path or root is None:
            return []
        directory = posixpath.dirname(path)
        try:
            tree = self._files.tree(
                path=directory or None,
                depth=1,
                workspace_root=str(root),
            )
        except Exception as exc:  # noqa: BLE001 - 目录不存在 / 越界都退化为空
            logger.debug("ide_completion.l1_tree_failed path=%s error=%s", path, exc)
            return []

        current_ext = _extension(path)
        current_stem = posixpath.splitext(posixpath.basename(path))[0].lower()
        # 两个弱信号：当前文件 import 了谁、正文里提到了谁。同目录文件往往不少，
        # 只按扩展名/路径长度排会挑到"语法相似但毫无关系"的文件，白占预算。
        references = _reference_tokens(text)
        mentions = _mention_tokens(text)
        now = datetime.now(timezone.utc)
        candidates = [
            entry
            for entry in tree.entries
            if entry.kind == "file"
            and entry.path != path
            and _extension(entry.path) in CODE_EXTENSIONS
        ]
        candidates.sort(
            key=lambda entry: self._neighbour_rank(
                entry, current_ext, current_stem, references, mentions, now
            )
        )

        excerpts: list[dict[str, str]] = []
        used = 0
        for entry in candidates:
            if len(excerpts) >= L1_MAX_FILES or used >= L1_MAX_CHARS:
                break
            try:
                content = self._files.read(path=entry.path, workspace_root=str(root)).content
            except Exception:  # noqa: BLE001 - 单个坏文件不影响其它邻接文件
                continue
            signature = _signature_excerpt(content)
            if not signature:
                continue
            signature = _truncate(signature, L1_PER_FILE_CHARS)
            budget = L1_MAX_CHARS - used
            if budget <= 0:
                break
            signature = _truncate(signature, budget)
            used += len(signature)
            excerpts.append({"path": entry.path, "excerpt": signature})
        return excerpts

    @staticmethod
    def _neighbour_rank(
        entry: FileEntry,
        current_ext: str,
        current_stem: str,
        references: set[str],
        mentions: set[str],
        now: datetime,
    ) -> tuple[int, int, int, int, int, int, str]:
        name = posixpath.basename(entry.path)
        stem = posixpath.splitext(name)[0].lower()
        ext = _extension(entry.path)
        # 排序优先级（越小越靠前）：
        # 1) 当前文件 import 了这个模块 —— 最强相关；
        # 2) 当前正文里出现过这个名字（约定式 import / 同名字段）；
        # 3) 同语言；4) 文件名与当前文件相关；5) 最近改动过；6) 路径短的。
        imported = 0 if _is_referenced(name, stem, references) else 1
        mentioned = 0 if stem and stem in mentions else 1
        same_ext = 0 if ext == current_ext else 1
        related = 0 if (current_stem and (current_stem in stem or stem in current_stem)) else 1
        fresh = _age_bucket(entry.modified_at, now)
        return (imported, mentioned, same_ext, related, fresh, len(entry.path), entry.path)

    # --- L2 ---

    async def _build_l2(self, *, root: Path | None) -> dict[str, str]:
        if root is None:
            return {"spec": "", "stack": "", "recent": ""}
        profile = await self._scan_profile(root)
        stack = self._render_stack(profile)
        recent = self._render_recent(await self._git_status(root))
        spec = self._render_spec(root)

        stack = _truncate(stack, L2_STACK_CHARS)
        recent = _truncate(recent, L2_RECENT_CHARS)
        spec = _truncate(spec, L2_SPEC_CHARS)
        return {"spec": spec, "stack": stack, "recent": recent}

    async def _scan_profile(self, root: Path) -> dict[str, Any]:
        key = str(root)
        cached = self._cache_get(self._scan_cache, key)
        if cached is not None:
            return cached

        def _scan() -> dict[str, Any]:
            try:
                return self._scanner.scan(workspace_root=key).to_dict()
            except Exception as exc:  # noqa: BLE001 - 扫描失败只是没有 L2，不影响补全
                logger.debug("ide_completion.scan_failed root=%s error=%s", key, exc)
                return {}

        return await self._run_cached(self._scan_cache, key, _scan, ttl=SCAN_TTL_SECONDS)

    def _render_stack(self, profile: dict[str, Any]) -> str:
        if not profile:
            return ""
        primary = str(profile.get("primary_language") or "").strip()
        languages = [str(item) for item in profile.get("languages") or [] if str(item).strip()]
        frameworks = [str(item) for item in profile.get("frameworks") or [] if str(item).strip()]
        package_manager = str(profile.get("package_manager") or "").strip()
        lines: list[str] = []
        if primary:
            lines.append(f"主语言：{primary}")
        elif languages:
            lines.append(f"语言：{'、'.join(languages[:4])}")
        if frameworks:
            lines.append(f"框架：{'、'.join(frameworks[:6])}")
        if package_manager:
            lines.append(f"包管理：{package_manager}")
        return "\n".join(lines)

    async def _git_status(self, root: Path) -> GitStatus | None:
        key = str(root)
        cached = self._cache_get(self._git_cache, key)
        if cached is not None:
            return cached

        def _status() -> GitStatus | None:
            try:
                return self._git_client.status(workspace_root=key)
            except Exception as exc:  # noqa: BLE001 - 不是 Git 仓库就没有"近期改动"
                logger.debug("ide_completion.git_status_failed root=%s error=%s", key, exc)
                return None

        return await self._run_cached(self._git_cache, key, _status, ttl=GIT_TTL_SECONDS)

    def _render_recent(self, status: GitStatus | None) -> str:
        if status is None:
            return ""
        lines: list[str] = []
        if status.branch:
            lines.append(f"分支：{status.branch}")
        for item in status.files[:12]:
            code = f"{item.index_status}{item.worktree_status}".strip() or "?"
            lines.append(f"{code} {item.path}")
        return "\n".join(lines)

    def _render_spec(self, root: Path) -> str:
        specs_dir = root / ".flux" / "specs"
        try:
            paths = sorted(specs_dir.glob("*.md"))
        except OSError:
            return ""
        chunks: list[str] = []
        used = 0
        for spec_path in paths:
            if used >= L2_SPEC_CHARS:
                break
            try:
                content = spec_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # 去掉一级标题（文件名已经能表达主题），保留正文
            body = "\n".join(
                line for line in content.splitlines() if not line.startswith("# ")
            ).strip()
            if not body:
                continue
            chunk = _truncate(body, L2_SPEC_CHARS - used)
            chunks.append(f"## {spec_path.stem}\n{chunk}")
            used += len(chunk)
        return "\n\n".join(chunks)

    # --- 缓存 ---

    def _cache_get(self, cache: dict[str, _CacheEntry], key: str) -> Any | None:
        with self._lock:
            entry = cache.get(key)
            if entry is None or entry.expires_at <= time.monotonic():
                cache.pop(key, None)
                return None
            return entry.value

    async def _run_cached(self, cache: dict[str, _CacheEntry], key: str, fn, *, ttl: float) -> Any:
        # 扫描 / git 都是同步阻塞调用，放到线程池里，避免卡住事件循环（补全会并发触发）
        value = await asyncio.to_thread(fn)
        with self._lock:
            cache[key] = _CacheEntry(expires_at=time.monotonic() + ttl, value=value)
        return value

    # --- 其它 ---

    def _workspace_root(self) -> Path | None:
        raw = (self._settings.workspace_root or "").strip()
        if not raw:
            return None
        try:
            root = Path(raw).expanduser().resolve()
        except OSError:
            return None
        return root if root.is_dir() else None

    @staticmethod
    def _cache_key(
        *,
        path: str | None,
        caret: int | None,
        text: str | None,
        l1: list[dict[str, str]],
        l2: dict[str, str],
    ) -> str:
        digest = hashlib.blake2b(digest_size=8)
        digest.update((path or "").encode("utf-8"))
        digest.update(str(caret or 0).encode("utf-8"))
        digest.update((text or "").encode("utf-8", "replace"))
        for item in l1:
            digest.update(item["path"].encode("utf-8"))
            digest.update(item["excerpt"].encode("utf-8", "replace"))
        digest.update((l2.get("spec") or "").encode("utf-8", "replace"))
        digest.update((l2.get("stack") or "").encode("utf-8", "replace"))
        digest.update((l2.get("recent") or "").encode("utf-8", "replace"))
        return digest.hexdigest()


__all__ = [
    "CODE_EXTENSIONS",
    "GIT_TTL_SECONDS",
    "L0_AFTER_LINES",
    "L0_BEFORE_LINES",
    "L0_MAX_CHARS",
    "L1_MAX_CHARS",
    "L1_MAX_FILES",
    "L2_MAX_CHARS",
    "SCAN_TTL_SECONDS",
    "CompletionContextBuilder",
]
