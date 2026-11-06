"""Diff Engine（主规格 §7.4；实施计划 §6）。

不自研 diff 算法：直接基于标准库 difflib 产出标准 unified diff。
职责只有一件事——`original + proposed → unified diff + 概览`，供人工审阅与 UI 展示。
"""

from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass
from typing import Any


def content_hash(text: str) -> str:
    """文件内容的 sha256（十六进制）。

    提案生成时记下它，Apply 前再算一次与磁盘现状比对：不一致说明用户中途改过文件，
    必须禁止直接落盘（实施计划 §5 关键规则）。
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_unified_diff(file_path: str, original: str, proposed: str) -> str:
    """行级 unified diff，文件头固定为 a/<path> 与 b/<path>（git 风格）。"""
    return "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            proposed.splitlines(keepends=True),
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
        )
    )


@dataclass(frozen=True)
class FileDiff:
    """一份文件的完整差异：文本 diff + 概览（新增/删除行、改动块数）。"""

    file_path: str
    unified: str
    added_lines: int
    removed_lines: int
    hunks: int
    changed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_path": self.file_path,
            "unified": self.unified,
            "added_lines": self.added_lines,
            "removed_lines": self.removed_lines,
            "hunks": self.hunks,
            "changed": self.changed,
        }


def compute_delete_diff(file_path: str, original: str) -> FileDiff:
    """删除类提案的整文件移除 diff（P1-1）。

    与"把 proposed 当空串"的普通 diff 不同：这里 tofile 用 `/dev/null`（git 的删除写法），
    且即使原文为空也强制 `changed=True`——删除一个空文件仍然是一次真实的文件变更。
    removed_lines 为原文行数、added_lines 恒为 0。
    """
    lines = original.splitlines(keepends=True)
    unified = "".join(
        difflib.unified_diff(lines, [], fromfile=f"a/{file_path}", tofile="/dev/null")
    )
    if not unified:
        # 空文件：difflib 认为没有差异，补一个只含文件头的删除标记
        unified = f"--- a/{file_path}\n+++ /dev/null\n"
    hunks = sum(1 for line in unified.splitlines() if line.startswith("@@"))
    return FileDiff(
        file_path=file_path,
        unified=unified,
        added_lines=0,
        removed_lines=len(lines),
        hunks=hunks,
        changed=True,
    )


def compute_file_diff(file_path: str, original: str, proposed: str) -> FileDiff:
    unified = build_unified_diff(file_path, original, proposed)
    added = removed = hunks = 0
    # 只跳过真正的文件头那两行（--- a/<path> / +++ b/<path>），
    # 之后内容行即使以 -- / ++ 开头也照常按 hunk 计数。
    header_lines = 2
    for line in unified.splitlines():
        if header_lines:
            header_lines -= 1
            continue
        if line.startswith("@@"):
            hunks += 1
        elif line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return FileDiff(
        file_path=file_path,
        unified=unified,
        added_lines=added,
        removed_lines=removed,
        hunks=hunks,
        changed=bool(unified),
    )
