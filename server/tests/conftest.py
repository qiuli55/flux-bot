"""公共测试夹具。"""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flux.config import Settings
from flux.container import Container
from flux.db.session import create_engine
from flux.enums import Capability
from flux.main import create_app
from flux.models import Base


def python_command(code: str) -> str:
    """一条跨平台可执行的 `python -c <code>` 命令。

    测试命令在 Windows 上由 cmd.exe 执行：它不认 POSIX 的单引号，而 `shlex.quote`
    因反斜杠不在安全字符集里，会把 `C:\\...\\python.exe` 整个包进单引号，命令直接失败
    （CI 实测 exit=1）。两个平台各选各自安全的引用方式，命令语义保持一致。
    `code` 在 Windows 分支被双引号包裹，因此其中不要再用双引号（用单引号）。
    """
    exe = f'"{sys.executable}"' if os.name == "nt" else shlex.quote(sys.executable)
    body = f'"{code}"' if os.name == "nt" else shlex.quote(code)
    return f"{exe} -c {body}"


def python_script(path: str | Path) -> str:
    """跨平台可执行的 `python <script>` 命令（脚本文件靠解释器直跑，不依赖 shebang）。"""
    exe = f'"{sys.executable}"' if os.name == "nt" else shlex.quote(sys.executable)
    arg = f'"{path}"' if os.name == "nt" else shlex.quote(str(path))
    return f"{exe} {arg}"


@pytest.fixture()
def settings(tmp_path) -> Settings:
    """测试用配置：SQLite 落临时目录，日志降到 WARNING，供应商用离线回显。

    三个真实供应商的密钥显式传 None：pydantic-settings 的默认值仍是"读环境"，
    开发机仓库根的 .env 一旦填了 FLUX_OPENAI_API_KEY 之类，就会让"未配置供应商"
    的断言变成"已配置"，测试结果随开发机环境漂移。显式钉死让用例与外部环境无关。
    """
    return Settings(
        env="test",
        log_level="WARNING",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'flux-test.db'}",
        # 测试一律不碰账号云：开发机 /etc 下可能有真实设备凭据，必须显式关掉
        memory_cloud_sync=False,
        # 同上：套餐用量归集也要关掉，且游标落临时目录（防污染 ~/.flux/billing-sync.json）
        billing_cloud_sync=False,
        billing_sync_file=str(tmp_path / "billing-sync.json"),
        # 对话上下文上云同理：测试不能把对话推到开发机 /etc 凭据对应的真实设备
        context_cloud_sync=False,
        # 助理后台 worker 关掉：否则事件刚入队就被判断消费，幂等断言会看到不同的 outbox 数
        supervisor_worker_enabled=False,
        # 知识库同理：后台索引会读写对话/docs，用例自己按需显式触发 run_once
        knowledge_enabled=False,
        knowledge_worker_enabled=False,
        # Bot 调度循环同理：后台按 next_run_at 自动开工会让任务状态随用例时序漂移
        bot_scheduler_enabled=False,
        # Webhook 密钥显式置空：避免开发机 .env 里的真实密钥让 fail-closed 断言失真
        routine_webhook_secret=None,
        github_webhook_secret=None,
        knowledge_embedding_api_key=None,
        memory_prefs_file=str(tmp_path / "memory-prefs.json"),
        # 补全偏好同样落临时目录：不能读写开发机 ~/.flux/completion-prefs.json
        completion_prefs_file=str(tmp_path / "completion-prefs.json"),
        # 埋点统计也必须落临时目录：用例不能污染开发机的接受率 / 延迟数据
        completion_stats_file=str(tmp_path / "completion-stats.json"),
        openai_api_key=None,
        anthropic_api_key=None,
        deepseek_api_key=None,
        default_provider="local",
        # 密钥库用固定主密钥：用例不读 ~/.flux，也不在开发机留下密钥文件
        key_vault_secret="flux-test-key-vault-secret",
        key_vault_key_file=str(tmp_path / "key-vault.key"),
        # 同样显式钉死：开发机仓库根 .env 的鉴权令牌/工作区会让整批 REST 用例 401 或走错目录
        auth_token=None,
        desktop_control_token=None,
        # 桌面入口测试不访问公网 desktop.json：默认置空，需要时用例注入 MockTransport
        desktop_config_url="",
        workspace_root=None,
        # 不读开发机 ~/.flux 里的自定义套餐，避免扩展套餐影响内置断言
        billing_plans_file="",
        # 同理：价目表用内置条目，不读开发机覆盖文件
        billing_prices_file="",
        # 采集器与账号指纹不能读开发机的真实凭据：否则账本账号随机器漂移
        opencode_config_file="",
        codex_account_home=str(tmp_path / "codex-account"),
        # 附件默认落到临时目录：测试绝不能往真实 /opt/flux/uploads 写文件
        uploads_dir=str(tmp_path / "uploads"),
        cloud_secret_key=None,
        cloud_login_required=False,
    )


@pytest.fixture()
def workspace_root(tmp_path: Path) -> Path:
    """一个真实存在的"被改项目"根目录（Apply 的落盘目标）。"""
    root = tmp_path / "project"
    root.mkdir()
    return root


@pytest.fixture()
def apply_settings(settings: Settings, workspace_root: Path) -> Settings:
    """带工作区配置的 Settings：Apply 只有配了 workspace_root 才允许落盘（§7.6）。"""
    return settings.model_copy(update={"workspace_root": str(workspace_root)})


@pytest.fixture()
def db_schema(settings: Settings) -> Iterator[None]:
    """为测试用 SQLite 库建表。

    任务落 tasks 表后，API 不再依赖「内存任务字典」，必须有表结构才能使用。
    这里用独立引擎建表后立即 dispose，容器/应用各自再开自己的引擎即可。
    """
    engine = create_engine(settings.database_url)

    async def _create_all() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    try:
        asyncio.run(_create_all())
    finally:
        asyncio.run(engine.dispose())
    yield


@pytest.fixture()
def container(settings: Settings, db_schema: None) -> Container:
    return Container(settings)


@pytest.fixture()
def apply_container(apply_settings: Settings, db_schema: None) -> Container:
    """配好工作区根目录的容器，供真正会落盘的 Apply 用例使用。"""
    return Container(apply_settings)


@pytest.fixture()
def client(settings: Settings, db_schema: None) -> Iterator[TestClient]:
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def apply_client(apply_settings: Settings, db_schema: None) -> Iterator[TestClient]:
    """配好工作区根目录的 app，供经 API 落盘的用例使用。"""
    app = create_app(apply_settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def mcp_client(apply_client: TestClient) -> TestClient:
    """MCP 用例专用：工作区根已配置（proposal.create 要读原文件）。"""
    return apply_client


#: 测试档案的默认权限：除硬禁令（secret.access）外的全部能力。
#: 单测关心的是"令牌 scopes 生效"，档案权限给足，交集即等于令牌声明的 scopes。
ALL_TEST_CAPABILITIES = tuple(c for c in Capability if c is not Capability.SECRET_ACCESS)


def create_agent(
    client: TestClient,
    *,
    name: str,
    permissions: list[Capability] | None = None,
    role: str = "developer",
) -> str:
    """按名字登记一个 Agent 档案（已存在则复用），返回 canonical UUID（P3-16）。"""
    for agent in client.get("/api/v1/agents").json()["data"]:
        if agent["spec"]["name"] == name:
            return str(agent["id"])
    response = client.post(
        "/api/v1/agents",
        json={
            "name": name,
            "role": role,
            "permissions": [str(p) for p in (permissions or ALL_TEST_CAPABILITIES)],
        },
    )
    assert response.status_code == 200, response.text
    return str(response.json()["data"]["id"])


def issue_token(
    client: TestClient,
    *,
    agent_id: str = "codex",
    scopes: list[Capability] | None = None,
    permissions: list[Capability] | None = None,
    label: str = "",
) -> str:
    """经 REST 签发一枚接入令牌，返回明文（测试里直接用真实签发路径，不走内部捷径）。

    P3-16 起令牌只能绑定注册表里的 canonical UUID：先按 name 建/取档案，再拿 UUID 签发。
    `permissions` 可单独指定档案权限，用于验证"档案收权 → 令牌可用面收窄"。
    """
    canonical = create_agent(client, name=agent_id, permissions=permissions)
    response = client.post(
        f"/api/v1/agents/{canonical}/tokens",
        json={"scopes": [str(s) for s in (scopes or [Capability.FILE_READ])], "label": label},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["data"]["token"])


# --- Windows CI 诊断 ---


if sys.platform == "win32":

    def pytest_runtest_logreport(report: pytest.TestReport) -> None:
        """Windows 上把每个失败**即时**打到 stdout。

        windows-latest 的 pwsh wrapper 在 pytest 跑到后段会进入交互调试模式
        （日志出现 `[DBG]: PS ...`），末尾的 FAILED 列表与 traceback 会整体丢失，
        只留下进度点里的 F，无法定位是哪些用例。即时打印出现在进度点之间，
        不会被吞掉；仅 Windows 生效，不影响本地输出。
        """
        if report.failed and report.when in ("setup", "call"):
            text = f"\n### FAILED {report.nodeid}\n{report.longrepr}\n"
            # Windows 控制台默认 cp1252：直接打印中文（用例 docstring 里有）会抛
            # UnicodeEncodeError 并让 pytest 以 INTERNALERROR 崩掉，反而看不到清单。
            # 降级成 ascii 可读字符——用例名与断言都是英文，信息不丢。
            print(text.encode("ascii", "replace").decode("ascii"), flush=True)
