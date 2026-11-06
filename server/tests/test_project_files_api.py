"""工作区文件接口测试（§12.5 的只读文件树 / 读文件）。

全部在**真实临时目录**上跑，不 mock 文件系统：这两个接口的价值就是"如实反映磁盘
事实"，同时把越界路径、软链、二进制、超大文件挡在门口。夹具风格与 `test_api`
/ `test_project_brain` 一致：走真实数据库，项目经 HTTP 登记。
"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from flux.config import Settings
from flux.main import create_app

PREFIX = "/api/v1"
MAX_CONTENT_BYTES = 256 * 1024


def _new_project(client: TestClient, *, name: str = "文件浏览项目") -> str:
    response = client.post(f"{PREFIX}/projects", json={"name": name})
    assert response.status_code == 200, response.text
    return response.json()["data"]["id"]


def _write(root: Path, relative: str, content: str = "") -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    # 固定 LF：Windows 默认会把 \n 翻成 CRLF；接口返回的是按字节解码的真实内容（不折行），
    # fixture 必须两平台字节一致，否则 size/content 断言会漂移。
    target.write_text(content, encoding="utf-8", newline="\n")
    return target


def _tree(client: TestClient, project_id: str, **params: object) -> dict:
    response = client.get(f"{PREFIX}/projects/{project_id}/files", params=params)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    return body["data"]


def _content(client: TestClient, project_id: str, path: str, **params: object):
    return client.get(
        f"{PREFIX}/projects/{project_id}/files/content", params={"path": path, **params}
    )


def _by_path(entries: list[dict], path: str) -> dict:
    for entry in entries:
        if entry["path"] == path:
            return entry
    raise AssertionError(f"文件树里没有 {path}：{[e['path'] for e in entries]}")


# --- 列目录 ---


def test_list_root_reports_dirs_and_files(apply_client: TestClient, workspace_root: Path) -> None:
    _write(workspace_root, "app/auth.py", "def login():\n    return True\n")
    _write(workspace_root, "README.md", "# 演示\n")
    project_id = _new_project(apply_client)

    data = _tree(apply_client, project_id)

    assert data["root"] == str(workspace_root)
    assert data["path"] == ""
    assert data["truncated"] is False
    app = _by_path(data["entries"], "app")
    assert (app["name"], app["kind"], app["size"]) == ("app", "dir", None)
    assert app["modified_at"].endswith("+00:00")
    auth = _by_path(data["entries"], "app/auth.py")
    assert (auth["name"], auth["kind"]) == ("auth.py", "file")
    assert auth["size"] == len(b"def login():\n    return True\n")
    assert _by_path(data["entries"], "README.md")["kind"] == "file"
    # 目录在前、文件在后（与 Scanner 的顶层结构同一种直观顺序）
    assert data["entries"][0]["kind"] == "dir"


def test_list_subdirectory_respects_depth(apply_client: TestClient, workspace_root: Path) -> None:
    _write(workspace_root, "a/top.py", "x = 1\n")
    _write(workspace_root, "a/b/c/d/deep.py", "deep = True\n")
    project_id = _new_project(apply_client)

    shallow = _tree(apply_client, project_id, path="a", depth=1)
    assert shallow["path"] == "a"
    assert sorted(entry["path"] for entry in shallow["entries"]) == ["a/b", "a/top.py"]

    nested = _tree(apply_client, project_id, path="a", depth=2)
    assert "a/b/c" in [entry["path"] for entry in nested["entries"]]
    assert "a/b/c/d" not in [entry["path"] for entry in nested["entries"]]

    deeper = _tree(apply_client, project_id, path="a", depth=4)
    assert _by_path(deeper["entries"], "a/b/c/d/deep.py")["kind"] == "file"


def test_depth_above_limit_is_rejected(apply_client: TestClient, workspace_root: Path) -> None:
    project_id = _new_project(apply_client)

    too_deep = apply_client.get(f"{PREFIX}/projects/{project_id}/files", params={"depth": 5})
    zero = apply_client.get(f"{PREFIX}/projects/{project_id}/files", params={"depth": 0})

    assert too_deep.status_code == 422
    assert too_deep.json()["code"] == "validation_error"
    assert zero.status_code == 422


def test_ignored_directories_are_hidden(apply_client: TestClient, workspace_root: Path) -> None:
    for ignored in (".git", "node_modules", "__pycache__", ".venv", "dist", ".flux"):
        _write(workspace_root, f"{ignored}/inside.txt", "should not show\n")
    _write(workspace_root, "app/main.py", "app = None\n")
    project_id = _new_project(apply_client)

    data = _tree(apply_client, project_id, depth=4)
    paths = [entry["path"] for entry in data["entries"]]

    assert "app/main.py" in paths
    for ignored in (".git", "node_modules", "__pycache__", ".venv", "dist", ".flux"):
        assert ignored not in paths
        assert f"{ignored}/inside.txt" not in paths


def test_entry_cap_marks_truncated(apply_client: TestClient, workspace_root: Path) -> None:
    for index in range(2001):
        (workspace_root / f"file_{index:04d}.txt").write_text("x\n", encoding="utf-8")
    project_id = _new_project(apply_client)

    data = _tree(apply_client, project_id, depth=1)

    assert data["truncated"] is True
    assert len(data["entries"]) == 2000


def test_path_pointing_at_a_file_is_rejected(
    apply_client: TestClient, workspace_root: Path
) -> None:
    _write(workspace_root, "app/main.py", "app = None\n")
    project_id = _new_project(apply_client)

    response = apply_client.get(
        f"{PREFIX}/projects/{project_id}/files", params={"path": "app/main.py"}
    )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


# --- 读文件 ---


def test_read_file_returns_content_and_true_size(
    apply_client: TestClient, workspace_root: Path
) -> None:
    text = "def login():\n    return '密钥'\n"
    _write(workspace_root, "app/auth.py", text)
    project_id = _new_project(apply_client)

    response = _content(apply_client, project_id, "app/auth.py")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["path"] == "app/auth.py"
    assert data["content"] == text
    assert data["size"] == len(text.encode("utf-8"))
    assert data["truncated"] is False


def test_large_file_is_truncated(apply_client: TestClient, workspace_root: Path) -> None:
    content = "a" * (MAX_CONTENT_BYTES + 4096)
    _write(workspace_root, "big.txt", content)
    project_id = _new_project(apply_client)

    response = _content(apply_client, project_id, "big.txt")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["truncated"] is True
    assert len(data["content"]) == MAX_CONTENT_BYTES
    assert data["size"] == len(content)
    assert data["content"] == content[:MAX_CONTENT_BYTES]


def test_binary_file_is_rejected(apply_client: TestClient, workspace_root: Path) -> None:
    target = workspace_root / "blob.bin"
    target.write_bytes(b"\x00\x01\x02\x03binary")
    project_id = _new_project(apply_client)

    response = _content(apply_client, project_id, "blob.bin")

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert "二进制" in body["message"]


def test_invalid_utf8_file_is_rejected(apply_client: TestClient, workspace_root: Path) -> None:
    (workspace_root / "bad.txt").write_bytes("中文".encode() + b"\xff\xfe")
    project_id = _new_project(apply_client)

    response = _content(apply_client, project_id, "bad.txt")

    assert response.status_code == 422
    assert "UTF-8" in response.json()["message"]


def test_content_on_directory_is_rejected(apply_client: TestClient, workspace_root: Path) -> None:
    _write(workspace_root, "app/main.py", "app = None\n")
    project_id = _new_project(apply_client)

    response = _content(apply_client, project_id, "app")

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


# --- 越界与软链 ---


def test_escaping_paths_are_rejected(apply_client: TestClient, workspace_root: Path) -> None:
    (workspace_root.parent / "secret.py").write_text("secret\n", encoding="utf-8")
    project_id = _new_project(apply_client)

    for escape in ("../secret.py", "/etc/passwd"):
        response = _content(apply_client, project_id, escape)
        assert response.status_code == 422, escape
        assert response.json()["code"] == "validation_error"


def test_symlink_outside_root_is_rejected_and_hidden(
    apply_client: TestClient, workspace_root: Path
) -> None:
    outside = workspace_root.parent / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("token = 'x'\n", encoding="utf-8")
    (workspace_root / "link.py").symlink_to(outside / "secret.py")
    (workspace_root / "linkdir").symlink_to(outside, target_is_directory=True)
    _write(workspace_root, "app/main.py", "app = None\n")
    project_id = _new_project(apply_client)

    read = _content(apply_client, project_id, "link.py")
    assert read.status_code == 422
    assert "符号链接" in read.json()["message"]

    data = _tree(apply_client, project_id, depth=4)
    paths = [entry["path"] for entry in data["entries"]]
    assert "link.py" not in paths
    assert "linkdir" not in paths
    assert all(not path.startswith("linkdir/") for path in paths)


# --- not_found ---


def test_missing_path_is_not_found(apply_client: TestClient, workspace_root: Path) -> None:
    project_id = _new_project(apply_client)

    tree = apply_client.get(f"{PREFIX}/projects/{project_id}/files", params={"path": "nope"})
    read = _content(apply_client, project_id, "nope.txt")

    assert tree.status_code == 404
    assert tree.json()["code"] == "not_found"
    assert read.status_code == 404
    assert read.json()["code"] == "not_found"


def test_unknown_project_is_not_found(apply_client: TestClient, workspace_root: Path) -> None:
    missing = uuid.uuid4()

    tree = apply_client.get(f"{PREFIX}/projects/{missing}/files")
    read = apply_client.get(
        f"{PREFIX}/projects/{missing}/files/content", params={"path": "app/auth.py"}
    )

    assert tree.status_code == 404
    assert tree.json()["code"] == "not_found"
    assert read.status_code == 404
    assert read.json()["code"] == "not_found"


def test_content_requires_path(apply_client: TestClient, workspace_root: Path) -> None:
    project_id = _new_project(apply_client)

    response = apply_client.get(f"{PREFIX}/projects/{project_id}/files/content")

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


# --- 工作区根解析 ---


def test_workspace_root_can_be_overridden_per_request(client: TestClient, tmp_path: Path) -> None:
    root = tmp_path / "another-root"
    _write(root, "app/main.py", "app = None\n")
    project_id = _new_project(client)

    data = _tree(client, project_id, workspace_root=str(root))
    read = _content(client, project_id, "app/main.py", workspace_root=str(root))

    assert _by_path(data["entries"], "app/main.py")["kind"] == "file"
    assert read.status_code == 200
    assert read.json()["data"]["content"] == "app = None\n"


def test_unconfigured_workspace_root_is_rejected(client: TestClient) -> None:
    project_id = _new_project(client)

    response = client.get(f"{PREFIX}/projects/{project_id}/files")

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_desktop_switching_root_rebinds_live_file_services(
    settings: Settings, workspace_root: Path, tmp_path: Path, db_schema: None
) -> None:
    """桌面端「打开文件夹」后，文件树与读文件必须立刻跟随新根。

    回归：`Container.set_workspace_root` 曾只写服务上不存在的属性名，接口答新根、
    实际仍读写旧根——用户看到"选完文件夹，Explorer 还是原来那棵树"。
    """
    other = tmp_path / "other-root"
    _write(other, "OTHER.md", "# 新根\n")
    _write(workspace_root, "OLD.md", "旧根\n")
    app = create_app(
        settings.model_copy(update={"env": "local", "workspace_root": str(workspace_root)})
    )
    with TestClient(app) as desktop:
        project_id = _new_project(desktop)

        response = desktop.put(
            f"{PREFIX}/workspace/root",
            json={"root": str(other)},
            headers={"X-Flux-Desktop": "1"},
        )

        assert response.status_code == 200, response.text
        assert response.json()["data"]["root"] == str(other.resolve())
        data = _tree(desktop, project_id, depth=1)
        assert data["root"] == str(other.resolve())
        assert [entry["name"] for entry in data["entries"]] == ["OTHER.md"]
        read = _content(desktop, project_id, "OTHER.md")
        assert read.status_code == 200, read.text
        assert read.json()["data"]["content"] == "# 新根\n"


def test_switching_root_stays_gated_outside_local_desktop(
    apply_client: TestClient, tmp_path: Path
) -> None:
    """env 非 local（此处为 test）时，即便带桌面端请求头也不允许换根。"""
    other = tmp_path / "other-root"
    other.mkdir()

    response = apply_client.put(
        f"{PREFIX}/workspace/root",
        json={"root": str(other)},
        headers={"X-Flux-Desktop": "1"},
    )

    assert response.status_code == 403
