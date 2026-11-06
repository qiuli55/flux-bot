"""工作区文件浏览（只读）：文件树 + 读取单个文件内容。"""

from flux.core.project_files.explorer import (
    DEFAULT_TREE_DEPTH,
    MAX_CONTENT_BYTES,
    MAX_TREE_DEPTH,
    MAX_TREE_ENTRIES,
    FileContent,
    FileEntry,
    FileTree,
    WorkspaceFileExplorer,
)

__all__ = [
    "DEFAULT_TREE_DEPTH",
    "MAX_CONTENT_BYTES",
    "MAX_TREE_DEPTH",
    "MAX_TREE_ENTRIES",
    "FileContent",
    "FileEntry",
    "FileTree",
    "WorkspaceFileExplorer",
]
