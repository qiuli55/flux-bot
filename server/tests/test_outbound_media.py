"""出站媒体规则：附件记录、平台上限、路径安全与 attach_file 工具。

这些用例不触网、不发送：`attach_file` 只登记，真正的上传 / 发送在频道里由
审批结果触发，另见三个频道各自的集成用例。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from flux.config import Settings
from flux.core.attachment_store import UploadedFile, register_workspace_file, resolve_upload
from flux.core.channels.outbound_media import (
    ATTACH_FILE_TOOL_NAME,
    attachment_meta,
    attachment_record,
    normalize_attachment_records,
    render_outbound_detail,
    run_attach_file_tool,
    validate_channel_attachment,
)
from flux.errors import ValidationError


def _uploaded(name: str = "chart.png", mime: str = "image/png", size: int = 2048) -> UploadedFile:
    return UploadedFile(id="upload123", name=name, size=size, mime=mime, path="/tmp/x")


def test_attachment_record_classifies_image_and_file() -> None:
    image = attachment_record(_uploaded())
    assert image == {
        "upload_id": "upload123",
        "name": "chart.png",
        "mime": "image/png",
        "size": 2048,
        "kind": "image",
    }

    document = attachment_record(_uploaded("report.pdf", "application/pdf"))
    assert document["kind"] == "file"
    # 不带绝对路径：审批 meta / API 都不应该泄露宿主目录。
    assert "path" not in document


def test_normalize_drops_unknown_ids_and_dedupes() -> None:
    records = normalize_attachment_records(
        [
            {"upload_id": "abc123", "name": "a.png", "mime": "image/png", "size": 1},
            {"upload_id": "abc123", "name": "dup.png"},
            {"name": "no-id.png"},
            {"upload_id": "../escape", "name": "bad.png"},
            "not-a-dict",
        ]
    )
    assert [record["upload_id"] for record in records] == ["abc123"]
    assert records[0]["kind"] == "image"


def test_approval_text_and_meta_show_name_type_and_size() -> None:
    records = [
        attachment_record(_uploaded("chart.png", "image/png", 2048)),
        attachment_record(_uploaded("report.pdf", "application/pdf", 3 * 1024 * 1024)),
    ]
    detail = render_outbound_detail("准备发送到飞书会话 chat-1：", "", records)
    assert "准备发送到飞书会话 chat-1" in detail
    assert "图片 chart.png" in detail
    assert "image/png" in detail
    assert "2.0KB" in detail
    assert "文件 report.pdf" in detail
    assert "3.0MB" in detail

    meta = attachment_meta(records)
    assert meta[0]["name"] == "chart.png"
    assert meta[0]["kind"] == "image"
    assert meta[0]["size_label"] == "2.0KB"
    assert all("path" not in item for item in meta)


def test_each_channel_enforces_official_size_and_type_limits() -> None:
    # 飞书：图片 10MB / 文件 30MB
    validate_channel_attachment(
        "feishu", name="a.png", mime="image/png", size=10 * 1024 * 1024, kind="image"
    )
    with pytest.raises(ValidationError) as image_error:
        validate_channel_attachment(
            "feishu", name="a.png", mime="image/png", size=10 * 1024 * 1024 + 1, kind="image"
        )
    assert "超过飞书出站上限 10MB" in image_error.value.message

    with pytest.raises(ValidationError) as file_error:
        validate_channel_attachment(
            "feishu", name="a.zip", mime="application/zip", size=31 * 1024 * 1024, kind="file"
        )
    assert "超过飞书出站上限 30MB" in file_error.value.message

    # 企业微信：图片只收 JPG/PNG；文件 20MB
    with pytest.raises(ValidationError) as wecom_type:
        validate_channel_attachment("wecom", name="a.gif", mime="image/gif", size=100, kind="image")
    assert "企业微信出站图片仅支持 JPG/PNG" in wecom_type.value.message

    with pytest.raises(ValidationError) as wecom_size:
        validate_channel_attachment(
            "wecom", name="a.zip", mime="application/zip", size=20 * 1024 * 1024 + 1, kind="file"
        )
    assert "超过企业微信出站上限 20MB" in wecom_size.value.message

    # 钉钉：文件 20MB
    with pytest.raises(ValidationError) as dingtalk_size:
        validate_channel_attachment(
            "dingtalk", name="a.zip", mime="application/zip", size=20 * 1024 * 1024 + 1, kind="file"
        )
    assert "超过钉钉出站上限 20MB" in dingtalk_size.value.message


def test_register_workspace_file_copies_and_rejects_escape(
    settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "report.pdf").write_bytes(b"%PDF-1.4 fake")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    uploaded = register_workspace_file(settings, root, "report.pdf")
    assert uploaded.name == "report.pdf"
    assert uploaded.mime == "application/pdf"
    assert uploaded.size == len(b"%PDF-1.4 fake")
    # 已复制进 attachment_store，发送端不需要再碰工作区
    assert uploaded.path != str(root / "report.pdf")
    assert resolve_upload(settings, uploaded.id) is not None

    with pytest.raises(ValidationError) as traversal:
        register_workspace_file(settings, root, "../outside.txt")
    assert "不能包含 .." in traversal.value.message

    with pytest.raises(ValidationError) as absolute:
        register_workspace_file(settings, root, str(outside))
    assert "必须位于 Bot 工作区内" in absolute.value.message

    with pytest.raises(ValidationError) as missing:
        register_workspace_file(settings, root, "nope.txt")
    assert "找不到文件" in missing.value.message


def test_attach_file_tool_registers_but_never_sends(settings: Settings, tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "chart.png").write_bytes(b"\x89PNG fake")
    bot = SimpleNamespace(id="bot-1")
    container = SimpleNamespace(
        settings=settings,
        bots=SimpleNamespace(workspace_path=lambda _bot: root),
    )

    async def scenario() -> None:
        targets: list[Any] = []
        result = await run_attach_file_tool(
            container, bot, {"path": "chart.png", "name": "季度图.png"}, targets
        )
        assert result["registered"] is True
        assert result["kind"] == "image"
        assert result["name"] == "季度图.png"
        assert len(targets) == 1

        # 重复登记同一个 upload_id 不会产生第二份
        again = await run_attach_file_tool(
            container, bot, {"upload_id": result["upload_id"]}, targets
        )
        assert again["upload_id"] == result["upload_id"]
        assert len(targets) == 1

        with pytest.raises(ValidationError):
            await run_attach_file_tool(container, bot, {}, targets)
        with pytest.raises(ValidationError):
            await run_attach_file_tool(container, bot, {"path": "../escape.png"}, targets)

    asyncio.run(scenario())


def test_attach_tool_schema_is_registered() -> None:
    assert ATTACH_FILE_TOOL_NAME == "attach_file"
