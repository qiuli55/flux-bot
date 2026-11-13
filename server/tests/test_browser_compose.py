"""浏览器 sidecar overlay 与文档的静态契约测试。

只解析仓库文件，不拉镜像、不启容器、不联网：本机没有 Playwright 运行时。
正常路径校验真实的 `docker-compose.browser.yml` 与 `docs/browser-agent.md`；
失败路径对解析结果做最小篡改，确认校验逻辑确实会拒绝缺陷，而不是形同虚设。
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
import yaml

from flux.core.agent_runtime.extra_mcp import parse_extra_mcp_servers

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = ROOT / "docker-compose.browser.yml"
DOCS_PATH = ROOT / "docs" / "browser-agent.md"

PINNED_IMAGE = (
    "mcr.microsoft.com/playwright/mcp:v0.0.85"
    "@sha256:cc70a6922ff856f3aba0fe97d0d94220dc9a7f367f6d332facc6d81d9a91e60e"
)
BROWSER_URL = "http://playwright-mcp:8931/mcp"
UP_COMMAND = "docker compose -f docker-compose.yml -f docker-compose.browser.yml up -d"
DOC_PLACEHOLDERS = ("<version", "<playwright-mcp-image")
SOURCE_URLS = (
    "https://registry.npmjs.org/@playwright%2Fmcp",
    "https://mcr.microsoft.com/v2/playwright/mcp/tags/list",
    "https://github.com/microsoft/playwright-mcp",
)


def _load_compose() -> dict[str, Any]:
    loaded = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), "overlay 顶层必须是 YAML 对象"
    return loaded


def _flag_value(command: list[Any], flag: str) -> str | None:
    """取 flag 紧跟的 value；未出现或没跟 value 时返回 None。"""
    tokens = [str(token) for token in command]
    for index in range(len(tokens) - 1):
        if tokens[index] == flag:
            return tokens[index + 1]
    return None


def _has_flag_value(command: list[Any], flag: str, value: str) -> bool:
    """命令里 flag 必须紧跟对应的 value（避免只靠子串匹配放过拆开的参数）。"""
    return _flag_value(command, flag) == value


def _compose_problems(doc: dict[str, Any]) -> list[str]:
    """返回 overlay 偏离约定的问题列表；空列表表示通过。"""
    problems: list[str] = []
    services = doc.get("services")
    if not isinstance(services, dict):
        return ["services 必须是对象"]
    playwright = services.get("playwright-mcp")
    flux_bot = services.get("flux-bot")
    if not isinstance(playwright, dict):
        return ["缺少 playwright-mcp 服务"]
    if not isinstance(flux_bot, dict):
        return ["缺少 flux-bot 服务"]

    if playwright.get("image") != PINNED_IMAGE:
        problems.append("image 必须钉死到 v0.0.85 及其 sha256 digest")
    if "ports" in playwright:
        problems.append("playwright-mcp 不能发布 ports")
    if playwright.get("expose") != ["8931"]:
        problems.append("playwright-mcp 必须 expose 8931")
    if not playwright.get("shm_size"):
        problems.append("playwright-mcp 必须设置 shm_size")
    if playwright.get("init") is not True:
        problems.append("playwright-mcp 必须设置 init: true")

    if playwright.get("entrypoint") != ["node"]:
        problems.append("entrypoint 必须是 node")
    command = playwright.get("command")
    output_dir: str | None = None
    if not isinstance(command, list):
        problems.append("command 必须是列表")
    else:
        tokens = [str(token) for token in command]
        for flag in ("--headless", "--isolated"):
            if flag not in tokens:
                problems.append(f"command 缺少 {flag}")
        for flag, value in (("--port", "8931"), ("--host", "0.0.0.0")):
            if not _has_flag_value(command, flag, value):
                problems.append(f"command 缺少 {flag} {value}")

        # 客户端访问 http://host:port/mcp 时发的 Host 头带端口，白名单只写服务名
        # 会被 403 拒绝；port 必须与 MCP url 里的端口一致。
        expected_host_port = urlsplit(BROWSER_URL).netloc
        allowed = _flag_value(command, "--allowed-hosts") or ""
        allowed_hosts = [entry.strip() for entry in allowed.split(",") if entry.strip()]
        if expected_host_port not in allowed_hosts:
            problems.append(f"--allowed-hosts 必须包含 {expected_host_port}（Host 头带端口）")

        output_dir = _flag_value(command, "--output-dir")
        if not output_dir:
            problems.append("command 缺少 --output-dir")

    environment = flux_bot.get("environment")
    raw = (
        environment.get("FLUX_CLAUDE_EXTRA_MCP_SERVERS") if isinstance(environment, dict) else None
    )
    if not isinstance(raw, str):
        problems.append("flux-bot 必须用字符串设置 FLUX_CLAUDE_EXTRA_MCP_SERVERS")
    else:
        browser = parse_extra_mcp_servers(raw).get("browser")
        if not isinstance(browser, dict) or browser.get("url") != BROWSER_URL:
            problems.append("FLUX_CLAUDE_EXTRA_MCP_SERVERS 解析后必须是 browser http server")

    for label, service in (("playwright-mcp", playwright), ("flux-bot", flux_bot)):
        networks = service.get("networks")
        if not isinstance(networks, list) or "browser" not in networks:
            problems.append(f"{label} 必须加入 browser network")
    flux_networks = flux_bot.get("networks")
    if isinstance(flux_networks, list) and "default" not in flux_networks:
        problems.append("flux-bot 必须保留 default network")

    networks = doc.get("networks")
    browser_network = networks.get("browser") if isinstance(networks, dict) else None
    if not isinstance(browser_network, dict):
        problems.append("必须声明 browser network")
    elif browser_network.get("internal") is True:
        problems.append("browser network 不能是 internal，否则浏览器无法出站")

    volumes = playwright.get("volumes")
    volume_entries = [str(entry) for entry in volumes] if isinstance(volumes, list) else []
    mount_targets = [entry.split(":")[1] for entry in volume_entries if entry.count(":") >= 1]
    # /home/node 在镜像里已存在且属于 node，空命名卷首次挂载会继承属主；
    # 挂到镜像里不存在的路径（旧配置的 /data/output）会被 Docker 建成 root:root。
    if "playwright-home:/home/node" not in volume_entries:
        problems.append("playwright-mcp 必须把命名卷 playwright-home 挂到 /home/node")
    if "/data/output" in mount_targets:
        problems.append("不能把卷挂到 /data/output：镜像里不存在，会被 root:root 创建")
    if output_dir:
        if output_dir == "/data/output" or output_dir.startswith("/data/"):
            problems.append(
                f"--output-dir {output_dir} 不能落在 /data 下：镜像里不存在，会被 root:root 创建"
            )
        elif not any(
            output_dir == target or output_dir.startswith(target.rstrip("/") + "/")
            for target in mount_targets
        ):
            problems.append(f"--output-dir {output_dir} 必须位于已挂载的卷路径下")
    top_volumes = doc.get("volumes")
    if not isinstance(top_volumes, dict) or "playwright-home" not in top_volumes:
        problems.append("顶层 volumes 必须声明 playwright-home")

    return problems


# --- 正常路径 ---


def test_overlay_pins_image_and_wires_browser_sidecar() -> None:
    doc = _load_compose()

    assert _compose_problems(doc) == []
    playwright = doc["services"]["playwright-mcp"]
    assert playwright["networks"] == ["browser"]
    # 输出目录落在 node 可写的挂载卷里，且不再是镜像里不存在的 /data/output
    assert playwright["volumes"] == ["playwright-home:/home/node"]
    command = playwright["command"]
    assert _flag_value(command, "--output-dir") == "/home/node/output"
    # allowed-hosts 必须含 host:port，才能匹配客户端发的 Host 头
    allowed_hosts = (_flag_value(command, "--allowed-hosts") or "").split(",")
    assert urlsplit(BROWSER_URL).netloc in allowed_hosts
    assert "playwright-home" in doc["volumes"]
    # flux-bot 保留默认网络（宿主机端口发布照常），同时加入 browser 网络
    assert set(doc["services"]["flux-bot"]["networks"]) == {"default", "browser"}

    raw = doc["services"]["flux-bot"]["environment"]["FLUX_CLAUDE_EXTRA_MCP_SERVERS"]
    assert parse_extra_mcp_servers(raw) == {
        "browser": {"type": "http", "url": BROWSER_URL},
    }


# --- 失败路径：篡改后的 overlay 必须被识别 ---


def _unpin_image(doc: dict[str, Any]) -> None:
    doc["services"]["playwright-mcp"]["image"] = "mcr.microsoft.com/playwright/mcp:v0.0.85"


def _publish_port(doc: dict[str, Any]) -> None:
    doc["services"]["playwright-mcp"]["ports"] = ["8931:8931"]


def _drop_isolated(doc: dict[str, Any]) -> None:
    doc["services"]["playwright-mcp"]["command"].remove("--isolated")


def _drop_allowed_host_port(doc: dict[str, Any]) -> None:
    """只留服务名、去掉 host:port：实机上就是 403 的成因。"""
    command = doc["services"]["playwright-mcp"]["command"]
    command[command.index("--allowed-hosts") + 1] = "playwright-mcp"


def _break_extra_mcp(doc: dict[str, Any]) -> None:
    doc["services"]["flux-bot"]["environment"]["FLUX_CLAUDE_EXTRA_MCP_SERVERS"] = (
        '{"browser":{"type":"stdio","command":"chromium"}}'
    )


def _detach_flux_bot(doc: dict[str, Any]) -> None:
    doc["services"]["flux-bot"]["networks"].remove("browser")


def _make_network_internal(doc: dict[str, Any]) -> None:
    doc["networks"]["browser"]["internal"] = True


def _drop_home_volume(doc: dict[str, Any]) -> None:
    doc["services"]["playwright-mcp"]["volumes"].remove("playwright-home:/home/node")


def _mount_volume_at_data_output(doc: dict[str, Any]) -> None:
    """回到旧配置：卷挂到镜像里不存在的 /data/output。"""
    doc["services"]["playwright-mcp"]["volumes"] = ["playwright-home:/data/output"]


def _output_dir_at_data_output(doc: dict[str, Any]) -> None:
    command = doc["services"]["playwright-mcp"]["command"]
    command[command.index("--output-dir") + 1] = "/data/output"


def _output_dir_outside_mount(doc: dict[str, Any]) -> None:
    command = doc["services"]["playwright-mcp"]["command"]
    command[command.index("--output-dir") + 1] = "/tmp/output"


@pytest.mark.parametrize(
    ("mutator", "expected"),
    [
        (_unpin_image, "image"),
        (_publish_port, "ports"),
        (_drop_isolated, "--isolated"),
        (_drop_allowed_host_port, "--allowed-hosts"),
        (_break_extra_mcp, "FLUX_CLAUDE_EXTRA_MCP_SERVERS"),
        (_detach_flux_bot, "browser network"),
        (_make_network_internal, "internal"),
        (_drop_home_volume, "/home/node"),
        (_mount_volume_at_data_output, "/data/output"),
        (_output_dir_at_data_output, "不能落在 /data 下"),
        (_output_dir_outside_mount, "已挂载"),
    ],
)
def test_validator_rejects_broken_overlay(
    mutator: Callable[[dict[str, Any]], None], expected: str
) -> None:
    doc = copy.deepcopy(_load_compose())
    mutator(doc)

    problems = _compose_problems(doc)
    assert any(expected in problem for problem in problems), problems


# --- 文档：无占位符且记录了快照与限制 ---


def _placeholder_hits(text: str) -> list[str]:
    return [marker for marker in DOC_PLACEHOLDERS if marker in text]


def test_docs_have_no_version_placeholders() -> None:
    text = DOCS_PATH.read_text(encoding="utf-8")
    assert _placeholder_hits(text) == []


def test_placeholder_scan_detects_leftovers(tmp_path: Path) -> None:
    """失败路径：占位符扫描本身要能命中，否则上面的断言没有意义。"""
    sample = tmp_path / "sample.md"
    sample.write_text(
        "image: <playwright-mcp-image>:<version, verify before use>\n",
        encoding="utf-8",
    )

    assert _placeholder_hits(sample.read_text(encoding="utf-8")) == list(DOC_PLACEHOLDERS)


def test_docs_record_snapshot_command_and_limits() -> None:
    text = DOCS_PATH.read_text(encoding="utf-8")

    assert "2026-11-12" in text
    assert PINNED_IMAGE in text
    assert UP_COMMAND in text
    assert BROWSER_URL in text
    for flag in ("--headless", "--isolated", "--allowed-hosts", "--output-dir"):
        assert flag in text
    for url in SOURCE_URLS:
        assert url in text
    # 声明验证范围：实机跑过 Docker 29，但镜像大小仍未实测
    assert "2026-11-13" in text
    assert "镜像大小仍未实测" in text
    # 说明 browser 网络允许出站，以及 internal 会破坏浏览器
    assert "internal: true" in text
    assert "出站" in text


def test_docs_explain_the_two_verified_pitfalls() -> None:
    """两个实机坑必须写进文档：Host 头带端口、输出目录不能是 /data/output。"""
    text = DOCS_PATH.read_text(encoding="utf-8")

    assert "两个已知坑" in text
    # 修法 1：白名单含 host:port，403 是白名单问题不是网络问题
    assert "playwright-mcp,playwright-mcp:8931" in text
    assert "403" in text
    # 修法 2：卷挂到 /home/node，输出目录在其下；EACCES 来自 root:root 挂载点
    assert "playwright-home" in text
    assert "/home/node/output" in text
    assert "EACCES" in text
    assert "root:root" in text
