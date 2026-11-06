"""工作区文件浏览（实施计划 ⑫ 前端 File Explorer / Code Editor 的后端前置）。

Explorer 提供三类能力：

1. `tree()` / `read()` —— 只读浏览工作区；
2. 用户文件操作 —— 新建、重命名/移动、删除；
3. `search()` —— 有界的 Workspace 文本搜索。

四条边界（与 §12.5 的落地说明一致）：

1. **只读**：不写、不建、不改任何用户文件；
2. **不跟随符号链接**：树遍历跳过所有软链，读文件遇到软链一律拒绝——指向工作区根
   之外的软链永远拿不到内容；
3. **路径必须落在工作区根内**：复用 Apply Engine 的 `safe_relative_path` 与
   `resolve_workspace_root`，软链防护用同一份 `resolve_within_root`（§7.6 同源），
   `../` 与绝对路径在入口就被拒绝；
4. **有界**：条目数、深度、单文件字节数都有上限，触顶时明确标记 `truncated`，
   绝不静默丢数据、也绝不把巨型文件整个读进内存。

忽略规则复用 Project Scanner 的 `IGNORED_DIRS`（`.git` / `node_modules` /
`__pycache__` / `.venv` / `dist` / `.flux` 等），不另起一套。
"""

from __future__ import annotations

import os
import re
import shutil
import stat
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flux.core.project_scanner.scanner import IGNORED_DIRS
from flux.core.virtual_workspace.apply_engine import resolve_workspace_root, safe_relative_path
from flux.core.virtual_workspace.path_guard import resolve_within_root
from flux.errors import ConflictError, NotFoundError, ValidationError

#: 文件树一次最多返回多少条（超出即截断，不让"列目录"变成无界内存操作）
MAX_TREE_ENTRIES = 2000
#: 文件树默认向下展开几层（根的直接子项算第 1 层）
DEFAULT_TREE_DEPTH = 2
#: 文件树允许的最大层数（再深由前端逐层请求）
MAX_TREE_DEPTH = 4
#: 单文件最大返回字节数；超出只截断返回，不拒绝（配合 `truncated` 标记）
MAX_CONTENT_BYTES = 256 * 1024
#: 全局搜索读取单个文件的最大字节数；超过即跳过该文件
MAX_SEARCH_FILE_BYTES = 1024 * 1024
#: 全局搜索最多返回的命中数
MAX_SEARCH_RESULTS = 1000
#: 一次 Replace All 最多修改多少个文件
MAX_REPLACE_FILES = 100
#: 一次 Replace All 最多替换多少处
MAX_REPLACEMENTS = 10000


@dataclass(frozen=True)
class FileEntry:
    """文件树里的一条（路径是相对工作区根的 POSIX 路径，前端可直接拼接）。"""

    path: str
    name: str
    kind: str  # "dir" | "file"
    size: int | None
    modified_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "name": self.name,
            "kind": self.kind,
            "size": self.size,
            "modified_at": self.modified_at,
        }


@dataclass(frozen=True)
class FileTree:
    root: str
    path: str
    entries: tuple[FileEntry, ...]
    truncated: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "path": self.path,
            "entries": [entry.to_dict() for entry in self.entries],
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class SearchHit:
    """One bounded text-search match in the active workspace."""

    path: str
    line: int
    column: int
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line": self.line,
            "column": self.column,
            "text": self.text,
        }


@dataclass(frozen=True)
class FileContent:
    path: str
    content: str
    size: int
    truncated: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "content": self.content,
            "size": self.size,
            "truncated": self.truncated,
        }


def _modified_at(timestamp: float) -> str:
    """UTC ISO-8601（秒精度），与 §12.5 的响应示例一致。"""
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds")


def _scan_directory(directory: Path) -> list[tuple[str, str, bool, bool, bool, int, float]]:
    """读一层目录（`lstat`，不跟随软链），返回可排序的裸数据。

    `os.scandir` 的 DirEntry 在迭代器关闭后的行为依平台而定，所以这里当场把所有
    需要的字段取出来，后续排序/遍历只碰普通数据。
    """
    items: list[tuple[str, str, bool, bool, bool, int, float]] = []
    try:
        with os.scandir(directory) as scanner:
            for entry in scanner:
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                items.append(
                    (
                        entry.name,
                        entry.path,
                        stat.S_ISLNK(info.st_mode),
                        stat.S_ISDIR(info.st_mode),
                        stat.S_ISREG(info.st_mode),
                        info.st_size,
                        info.st_mtime,
                    )
                )
    except OSError:
        # 权限不足 / 竞态删除：把这一层当作空目录，不因为一个坏目录让整棵树失败
        return []
    # 目录在前、文件在后，同级按名字升序（与 Scanner 的 `_structure` 同一种直观顺序）
    items.sort(key=lambda item: (not item[3], item[0]))
    return items


class WorkspaceFileExplorer:
    """Workspace file tree, text read, and bounded human file operations."""

    def __init__(self, *, workspace_root: str | Path | None = None) -> None:
        self._workspace_root = workspace_root

    @property
    def workspace_root(self) -> str | Path | None:
        """当前工作区根（只读投影，供容器切换根时读取核对）。"""
        return self._workspace_root

    @workspace_root.setter
    def workspace_root(self, value: str | Path | None) -> None:
        # 桌面端「打开文件夹」切换根时由 Container 赋值；写这里才真的改到服务自身，
        # 只改 settings 而漏掉服务实例会让接口说新根、实际仍读写旧根。
        self._workspace_root = value

    # --- 文件树 ---

    def tree(
        self,
        *,
        path: str | None = None,
        depth: int = DEFAULT_TREE_DEPTH,
        workspace_root: str | Path | None = None,
    ) -> FileTree:
        root = resolve_workspace_root(self._pick_root(workspace_root))
        relative = self._relative_directory(path)
        target = resolve_within_root(root, relative)
        if not target.exists():
            raise NotFoundError(
                f"目录不存在：{relative.as_posix() or '.'}",
                details={"path": relative.as_posix()},
            )
        if not target.is_dir():
            raise ValidationError(
                f"路径不是目录，无法列出文件树：{relative.as_posix()}",
                details={"path": relative.as_posix()},
            )
        entries, truncated = self._walk(root, target, depth=depth)
        return FileTree(
            root=str(root),
            path=relative.as_posix() if relative.parts else "",
            entries=tuple(entries),
            truncated=truncated,
        )

    def _walk(self, root: Path, base: Path, *, depth: int) -> tuple[list[FileEntry], bool]:
        """广度优先展开 `base` 下方至多 `depth` 层；条目数触顶立即停止并标记截断。"""
        entries: list[FileEntry] = []
        queue: deque[tuple[Path, int]] = deque([(base, 0)])
        while queue:
            directory, level = queue.popleft()
            for name, raw_path, is_link, is_dir, is_file, size, mtime in _scan_directory(directory):
                if is_link:  # 不跟随软链：根外的软链既进不了结果，也不会被展开
                    continue
                if is_dir:
                    if name in IGNORED_DIRS:
                        continue
                    if len(entries) >= MAX_TREE_ENTRIES:
                        return entries, True
                    child = Path(raw_path)
                    entries.append(
                        FileEntry(
                            path=child.relative_to(root).as_posix(),
                            name=name,
                            kind="dir",
                            size=None,
                            modified_at=_modified_at(mtime),
                        )
                    )
                    if level + 1 < depth:
                        queue.append((child, level + 1))
                elif is_file:
                    if len(entries) >= MAX_TREE_ENTRIES:
                        return entries, True
                    child = Path(raw_path)
                    entries.append(
                        FileEntry(
                            path=child.relative_to(root).as_posix(),
                            name=name,
                            kind="file",
                            size=size,
                            modified_at=_modified_at(mtime),
                        )
                    )
                # 其余类型（socket / 设备 / fifo）不是"目录或文件"，不进文件树
        return entries, False

    # --- 读文件 ---

    def read(self, *, path: str, workspace_root: str | Path | None = None) -> FileContent:
        root = resolve_workspace_root(self._pick_root(workspace_root))
        relative = safe_relative_path(path)
        target = resolve_within_root(root, relative)
        if not target.exists():
            raise NotFoundError(
                f"文件不存在：{relative.as_posix()}", details={"path": relative.as_posix()}
            )
        info = os.stat(target)
        if not stat.S_ISREG(info.st_mode):
            raise ValidationError(
                f"路径不是普通文件：{relative.as_posix()}",
                details={"path": relative.as_posix()},
            )

        with open(target, "rb") as handle:
            data = handle.read(MAX_CONTENT_BYTES)
            truncated = handle.read(1) != b""
        if b"\x00" in data:
            raise ValidationError(
                f"文件是二进制文件（含 NUL 字节），无法作为文本返回：{relative.as_posix()}",
                details={"path": relative.as_posix()},
            )
        content = self._decode(data, truncated=truncated, relative=relative)
        return FileContent(
            path=relative.as_posix(),
            content=content,
            size=info.st_size,  # 真实字节数，不是截断后的长度
            truncated=truncated,
        )

    # --- 用户发起的文件操作 ---

    def write(
        self,
        *,
        path: str,
        workspace_root: str | Path | None = None,
        content: str = "",
    ) -> FileContent:
        """覆写既有文件的全部内容（编辑器的「保存」）。

        与 `create_file` 的区别是**不新建**：目标必须是已存在的普通文件。
        原地截断写入，保留 inode 与权限位，避免 `os.replace` 让文件监视器、
        语言服务器把它当成「新文件」。
        """
        root = resolve_workspace_root(self._pick_root(workspace_root))
        relative = safe_relative_path(path)
        self._guard_user_path(relative)
        target = resolve_within_root(root, relative)
        if not target.exists():
            raise NotFoundError(
                f"文件不存在：{relative.as_posix()}", details={"path": relative.as_posix()}
            )
        info = os.stat(target)
        if not stat.S_ISREG(info.st_mode):
            raise ValidationError(
                f"路径不是普通文件：{relative.as_posix()}",
                details={"path": relative.as_posix()},
            )
        data = content.encode("utf-8")
        if len(data) > MAX_CONTENT_BYTES:
            raise ValidationError(
                f"保存内容超过 {MAX_CONTENT_BYTES} 字节上限",
                details={"path": relative.as_posix(), "max_bytes": MAX_CONTENT_BYTES},
            )
        with open(target, "r+b") as handle:
            handle.truncate(0)
            handle.write(data)
        return FileContent(
            path=relative.as_posix(),
            content=content,
            size=len(data),
            truncated=False,
        )

    def create_file(
        self, *, path: str, workspace_root: str | Path | None = None, content: str = ""
    ) -> str:
        """Create one regular UTF-8 text file as an explicit user operation."""
        root = resolve_workspace_root(self._pick_root(workspace_root))
        relative = safe_relative_path(path)
        self._guard_user_path(relative)
        target = resolve_within_root(root, relative)
        parent = resolve_within_root(root, relative.parent)
        if target.exists():
            raise ConflictError(
                f"文件已存在：{relative.as_posix()}", details={"path": relative.as_posix()}
            )
        if not parent.is_dir():
            raise NotFoundError(
                f"父目录不存在：{relative.parent.as_posix() or '.'}",
                details={"path": relative.parent.as_posix()},
            )
        data = content.encode("utf-8")
        if len(data) > MAX_CONTENT_BYTES:
            raise ValidationError(
                f"新建文件内容超过 {MAX_CONTENT_BYTES} 字节上限",
                details={"path": relative.as_posix(), "max_bytes": MAX_CONTENT_BYTES},
            )
        with open(target, "xb") as handle:
            handle.write(data)
        return relative.as_posix()

    def create_directory(self, *, path: str, workspace_root: str | Path | None = None) -> str:
        """Create one directory; parent must already exist."""
        root = resolve_workspace_root(self._pick_root(workspace_root))
        relative = safe_relative_path(path)
        self._guard_user_path(relative)
        target = resolve_within_root(root, relative)
        parent = resolve_within_root(root, relative.parent)
        if target.exists():
            raise ConflictError(
                f"路径已存在：{relative.as_posix()}", details={"path": relative.as_posix()}
            )
        if not parent.is_dir():
            raise NotFoundError(
                f"父目录不存在：{relative.parent.as_posix() or '.'}",
                details={"path": relative.parent.as_posix()},
            )
        target.mkdir()
        return relative.as_posix()

    def rename(
        self,
        *,
        path: str,
        new_path: str,
        workspace_root: str | Path | None = None,
    ) -> str:
        """Rename/move a user-selected file or directory within the workspace."""
        root = resolve_workspace_root(self._pick_root(workspace_root))
        source = safe_relative_path(path)
        target_relative = safe_relative_path(new_path)
        if not source.parts:
            raise ValidationError("不能重命名工作区根目录")
        self._guard_user_path(source)
        self._guard_user_path(target_relative)
        source_path = resolve_within_root(root, source)
        target_path = resolve_within_root(root, target_relative)
        if not source_path.exists():
            raise NotFoundError(
                f"路径不存在：{source.as_posix()}", details={"path": source.as_posix()}
            )
        if target_path.exists():
            raise ConflictError(
                f"目标路径已存在：{target_relative.as_posix()}",
                details={"path": target_relative.as_posix()},
            )
        if source_path.is_dir() and source_path in target_path.parents:
            raise ValidationError("不能把目录移动到自己的子目录中")
        parent = resolve_within_root(root, target_relative.parent)
        if not parent.is_dir():
            raise NotFoundError(
                f"目标父目录不存在：{target_relative.parent.as_posix() or '.'}",
                details={"path": target_relative.parent.as_posix()},
            )
        source_path.rename(target_path)
        return target_relative.as_posix()

    def delete(self, *, path: str, workspace_root: str | Path | None = None) -> str:
        """Delete a user-selected file or directory recursively."""
        root = resolve_workspace_root(self._pick_root(workspace_root))
        relative = safe_relative_path(path)
        if not relative.parts:
            raise ValidationError("不能删除工作区根目录")
        self._guard_user_path(relative)
        target = resolve_within_root(root, relative)
        if not target.exists():
            raise NotFoundError(
                f"路径不存在：{relative.as_posix()}", details={"path": relative.as_posix()}
            )
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        return relative.as_posix()

    @staticmethod
    def _guard_user_path(relative: Path) -> None:
        """Human Explorer may write, but never into Flux/Git internal directories."""
        if relative.parts and relative.parts[0] == ".flux":
            raise ValidationError(
                f"路径落在 Flux 内部目录 .flux/ 内，已拒绝：{relative.as_posix()}",
                details={"path": relative.as_posix()},
            )
        if relative.parts and relative.parts[0] == ".git":
            raise ValidationError(
                f"路径落在 Git 元数据目录 .git/ 内，已拒绝：{relative.as_posix()}",
                details={"path": relative.as_posix()},
            )

    # --- 全局文本搜索 ---

    def search(
        self,
        *,
        query: str,
        path: str | None = None,
        case_sensitive: bool = False,
        regex: bool = False,
        max_results: int = MAX_SEARCH_RESULTS,
        workspace_root: str | Path | None = None,
    ) -> list[SearchHit]:
        """Search text across the workspace without following symlinks."""

        query = query.strip()
        if not query:
            return []
        max_results = max(1, min(int(max_results), MAX_SEARCH_RESULTS))
        root = resolve_workspace_root(self._pick_root(workspace_root))
        relative = self._relative_directory(path)
        base = resolve_within_root(root, relative)
        if not base.exists():
            raise NotFoundError(
                f"目录不存在：{relative.as_posix() or '.'}",
                details={"path": relative.as_posix()},
            )
        if not base.is_dir():
            raise ValidationError(
                f"搜索范围不是目录：{relative.as_posix() or '.'}",
                details={"path": relative.as_posix()},
            )

        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            expression = re.compile(query if regex else re.escape(query), flags)
        except re.error as exc:
            raise ValidationError(
                f"无效的搜索正则表达式：{query}",
                details={"query": query},
            ) from exc

        hits: list[SearchHit] = []
        queue: deque[Path] = deque([base])
        while queue and len(hits) < max_results:
            directory = queue.popleft()
            for name, raw_path, is_link, is_dir, is_file, _size, _mtime in _scan_directory(
                directory
            ):
                if is_link:
                    continue
                if is_dir:
                    if name in IGNORED_DIRS:
                        continue
                    queue.append(Path(raw_path))
                    continue
                if not is_file:
                    continue

                candidate = Path(raw_path)
                try:
                    with open(candidate, "rb") as handle:
                        data = handle.read(MAX_SEARCH_FILE_BYTES + 1)
                except OSError:
                    continue
                if len(data) > MAX_SEARCH_FILE_BYTES or b"\x00" in data:
                    continue
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    continue

                for line_number, line_text in enumerate(text.splitlines(), start=1):
                    for match in expression.finditer(line_text):
                        relative_path = candidate.relative_to(root).as_posix()
                        hits.append(
                            SearchHit(
                                path=relative_path,
                                line=line_number,
                                column=match.start() + 1,
                                text=line_text[:500],
                            )
                        )
                        if len(hits) >= max_results:
                            break
                    if len(hits) >= max_results:
                        break
                if len(hits) >= max_results:
                    break
        return hits

    def replace(
        self,
        *,
        query: str,
        replacement: str,
        path: str | None = None,
        case_sensitive: bool = False,
        regex: bool = False,
        max_files: int = MAX_REPLACE_FILES,
        max_replacements: int = MAX_REPLACEMENTS,
        workspace_root: str | Path | None = None,
    ) -> dict[str, Any]:
        """Replace text in bounded UTF-8 files as an explicit human operation."""
        query = query.strip()
        if not query:
            return {"files": [], "replacements": 0, "truncated": False}
        root = resolve_workspace_root(self._pick_root(workspace_root))
        relative = self._relative_directory(path)
        base = resolve_within_root(root, relative)
        if not base.exists() or not base.is_dir():
            raise ValidationError(
                f"搜索范围不是目录：{relative.as_posix() or '.'}",
                details={"path": relative.as_posix()},
            )
        max_files = max(1, min(int(max_files), MAX_REPLACE_FILES))
        max_replacements = max(1, min(int(max_replacements), MAX_REPLACEMENTS))
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            expression = re.compile(query if regex else re.escape(query), flags)
        except re.error as exc:
            raise ValidationError(
                f"无效的替换正则表达式：{query}",
                details={"query": query},
            ) from exc

        changed_files: list[str] = []
        replacements = 0
        queue: deque[Path] = deque([base])
        while queue and len(changed_files) < max_files and replacements < max_replacements:
            directory = queue.popleft()
            for name, raw_path, is_link, is_dir, is_file, _size, _mtime in _scan_directory(
                directory
            ):
                if is_link:
                    continue
                if is_dir:
                    if name in IGNORED_DIRS:
                        continue
                    queue.append(Path(raw_path))
                    continue
                if not is_file:
                    continue
                candidate = Path(raw_path)
                try:
                    data = candidate.read_bytes()
                except OSError:
                    continue
                if len(data) > MAX_SEARCH_FILE_BYTES or b"\x00" in data:
                    continue
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                available = max_replacements - replacements
                updated, count = expression.subn(replacement, text, count=available)
                if count == 0:
                    continue
                replacements += count
                relative_path = candidate.relative_to(root).as_posix()
                relative_obj = safe_relative_path(relative_path)
                self._guard_user_path(relative_obj)
                # 字节级写回：文本模式会把 \n 翻译成 \r\n（Windows），把原文件里的
                # CRLF 变成 \r\r\n，与读取时保留的换行口径不一致（V2 同源教训）。
                candidate.write_bytes(updated.encode("utf-8"))
                changed_files.append(relative_path)
                if replacements >= max_replacements:
                    break
            if replacements >= max_replacements:
                break
        truncated = (
            bool(queue) or len(changed_files) >= max_files or replacements >= max_replacements
        )
        return {
            "files": changed_files,
            "replacements": replacements,
            "truncated": truncated,
        }

    # --- 内部工具 ---

    @staticmethod
    def _decode(data: bytes, *, truncated: bool, relative: Path) -> str:
        """UTF-8 解码；截断可能正好切在多字节字符中间，此时丢掉残缺尾部再解码。"""
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            if truncated:
                for drop in range(1, 4):
                    trimmed = data[: max(0, len(data) - drop)]
                    try:
                        return trimmed.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
            raise ValidationError(
                f"文件不是合法的 UTF-8 文本：{relative.as_posix()}",
                details={"path": relative.as_posix()},
            ) from exc

    def _pick_root(self, workspace_root: str | Path | None) -> str | Path | None:
        """显式传入的根优先，其次用服务配置的根；都没有交给 `resolve_workspace_root` 报错。"""
        if workspace_root:
            return workspace_root
        return self._workspace_root

    @staticmethod
    def _relative_directory(path: str | None) -> Path:
        """把可选子目录参数规整成相对路径；空值表示工作区根。"""
        if path is None or not path.strip():
            return Path()
        return safe_relative_path(path)


__all__ = [
    "DEFAULT_TREE_DEPTH",
    "MAX_CONTENT_BYTES",
    "MAX_TREE_DEPTH",
    "MAX_TREE_ENTRIES",
    "FileContent",
    "FileEntry",
    "FileTree",
    "MAX_SEARCH_FILE_BYTES",
    "MAX_REPLACE_FILES",
    "MAX_REPLACEMENTS",
    "MAX_SEARCH_RESULTS",
    "SearchHit",
    "WorkspaceFileExplorer",
]
