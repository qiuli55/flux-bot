"""Diff Engine 测试（主规格 §7.4；实施计划 §6）。"""

from __future__ import annotations

import pytest

from flux.core.virtual_workspace.diff_engine import (
    build_unified_diff,
    compute_file_diff,
    content_hash,
)

ORIGINAL = "def login(user):\n    return False\n"
PROPOSED = "def login(user):\n    return check_password(user)\n"


def test_content_hash_is_stable_and_sensitive() -> None:
    assert content_hash(ORIGINAL) == content_hash(ORIGINAL)
    assert len(content_hash(ORIGINAL)) == 64
    assert content_hash(ORIGINAL) != content_hash(PROPOSED)
    # 空文件也有确定的 hash（新建文件用它做基线）
    assert content_hash("") == content_hash("")


def test_unified_diff_marks_changed_line() -> None:
    diff = build_unified_diff("auth/login.py", ORIGINAL, PROPOSED)
    assert "--- a/auth/login.py" in diff
    assert "+++ b/auth/login.py" in diff
    assert "-    return False" in diff
    assert "+    return check_password(user)" in diff


def test_identical_content_has_no_diff() -> None:
    result = compute_file_diff("a.py", ORIGINAL, ORIGINAL)
    assert result.changed is False
    assert result.unified == ""
    assert (result.added_lines, result.removed_lines, result.hunks) == (0, 0, 0)


def test_stats_count_one_change_block() -> None:
    result = compute_file_diff("auth/login.py", ORIGINAL, PROPOSED)
    assert result.changed is True
    assert result.added_lines == 1
    assert result.removed_lines == 1
    assert result.hunks == 1


def test_stats_ignore_file_headers() -> None:
    """`---` / `+++` 是文件头，不能被算成删除行与新增行。"""
    result = compute_file_diff("a.py", "x\n", "y\n")
    assert (result.added_lines, result.removed_lines) == (1, 1)


def test_stats_count_lines_starting_with_dashes() -> None:
    """内容以 -- / ++ 开头的行要计入 removed_lines / added_lines，不能被当成文件头漏计。"""
    result = compute_file_diff("a.py", "-- comment\n", "++ count\n")
    assert (result.removed_lines, result.added_lines) == (1, 1)


def test_two_far_apart_edits_produce_two_hunks() -> None:
    original = "".join(f"line{i}\n" for i in range(40))
    proposed = original.replace("line1\n", "line1-changed\n").replace(
        "line38\n", "line38-changed\n"
    )
    result = compute_file_diff("big.py", original, proposed)
    assert result.hunks == 2
    assert (result.added_lines, result.removed_lines) == (2, 2)


def test_new_file_diff_is_all_additions() -> None:
    result = compute_file_diff("app/health.py", "", "def health():\n    return {'status': 'ok'}\n")
    assert result.hunks == 1
    assert result.added_lines == 2
    assert result.removed_lines == 0
    assert "@@ -0,0 +1,2 @@" in result.unified


def test_to_dict_exposes_ui_fields() -> None:
    payload = compute_file_diff("a.py", ORIGINAL, PROPOSED).to_dict()
    assert set(payload) == {
        "file_path",
        "unified",
        "added_lines",
        "removed_lines",
        "hunks",
        "changed",
    }
    assert payload["file_path"] == "a.py"


@pytest.mark.parametrize("path", ["a.py", "deep/nested/dir/b.py"])
def test_file_headers_keep_both_paths(path: str) -> None:
    diff = build_unified_diff(path, "a\n", "b\n")
    assert f"--- a/{path}" in diff
    assert f"+++ b/{path}" in diff
