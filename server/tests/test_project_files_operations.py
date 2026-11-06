from pathlib import Path

import pytest

from flux.core.project_files.explorer import WorkspaceFileExplorer
from flux.errors import ConflictError, ValidationError


def test_user_file_operations_round_trip(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    explorer = WorkspaceFileExplorer(workspace_root=tmp_path)

    assert explorer.create_file(path="src/app.ts", content="export const x = 1;") == "src/app.ts"
    assert (tmp_path / "src/app.ts").read_text(encoding="utf-8") == "export const x = 1;"

    assert explorer.create_directory(path="src/components") == "src/components"
    assert explorer.rename(path="src/app.ts", new_path="src/main.ts") == "src/main.ts"
    assert (tmp_path / "src/main.ts").exists()
    assert not (tmp_path / "src/app.ts").exists()

    assert explorer.delete(path="src/components") == "src/components"
    assert explorer.delete(path="src/main.ts") == "src/main.ts"
    assert not (tmp_path / "src/main.ts").exists()


def test_user_file_operations_reject_internal_paths_and_workspace_root(tmp_path: Path) -> None:
    explorer = WorkspaceFileExplorer(workspace_root=tmp_path)

    with pytest.raises(ValidationError):
        explorer.create_file(path="../escape.txt")

    with pytest.raises(ValidationError):
        explorer.create_file(path=".flux/escape.txt")

    with pytest.raises(ValidationError):
        explorer.create_directory(path=".git/hijack")

    with pytest.raises(ValidationError):
        explorer.rename(path=".", new_path="renamed")

    with pytest.raises(ValidationError):
        explorer.delete(path=".")


def test_user_file_operations_reject_collisions(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    explorer = WorkspaceFileExplorer(workspace_root=tmp_path)

    with pytest.raises(ConflictError):
        explorer.create_file(path="a.txt")

    with pytest.raises(ConflictError):
        explorer.create_directory(path="a.txt")

    with pytest.raises(ConflictError):
        explorer.rename(path="a.txt", new_path="a.txt")


def test_user_directory_rename_cannot_move_inside_itself(tmp_path: Path) -> None:
    (tmp_path / "parent").mkdir()
    (tmp_path / "parent" / "child").mkdir()
    explorer = WorkspaceFileExplorer(workspace_root=tmp_path)

    with pytest.raises(ValidationError):
        explorer.rename(path="parent", new_path="parent/child/new-parent")


def test_workspace_search_returns_line_and_column_and_ignores_binary(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.ts").write_text(
        "const target = 1;\nconst value = target + 1;\n", encoding="utf-8"
    )
    (tmp_path / "src" / "data.bin").write_bytes(b"target\x00binary")
    explorer = WorkspaceFileExplorer(workspace_root=tmp_path)

    hits = explorer.search(query="target")
    assert [(hit.path, hit.line, hit.column) for hit in hits] == [
        ("src/app.ts", 1, 7),
        ("src/app.ts", 2, 15),
    ]


def test_workspace_search_rejects_invalid_regex(tmp_path: Path) -> None:
    explorer = WorkspaceFileExplorer(workspace_root=tmp_path)

    with pytest.raises(ValidationError):
        explorer.search(query="[", regex=True)


def test_workspace_replace_updates_matching_text(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    target = tmp_path / "src" / "app.ts"
    target.write_text("target target\nkeep\n", encoding="utf-8")
    explorer = WorkspaceFileExplorer(workspace_root=tmp_path)

    result = explorer.replace(query="target", replacement="value")

    assert result["replacements"] == 2
    assert result["files"] == ["src/app.ts"]
    assert target.read_text(encoding="utf-8") == "value value\nkeep\n"


def test_workspace_replace_preserves_regex_backreferences(tmp_path: Path) -> None:
    target = tmp_path / "app.ts"
    target.write_text("foo=alpha\nfoo=beta\n", encoding="utf-8")
    explorer = WorkspaceFileExplorer(workspace_root=tmp_path)

    result = explorer.replace(
        query=r"foo=(\w+)",
        replacement=r"bar=\1",
        regex=True,
    )

    assert result["replacements"] == 2
    assert target.read_text(encoding="utf-8") == "bar=alpha\nbar=beta\n"
