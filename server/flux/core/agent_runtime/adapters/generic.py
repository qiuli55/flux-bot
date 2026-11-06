"""Generic CLI Adapter（最终方案 §3.3）：用一套可配置的探测规则适配任意 CLI Agent。

它只回答"装没装、什么版本、登没登录、有什么能力"——具体差异（版本参数、认证命令、
成功/失败的关键字、跑一轮的子命令）全部来自配置，具体 Adapter 只需给这几项赋值。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from flux.core.agent_runtime.adapters.base import (
    CliAgentAdapter,
    CliAgentProbe,
    ProbeRunner,
    default_which,
    run_probe,
)
from flux.core.agent_runtime.protocol import compose_instruction
from flux.enums import AgentInstallStatus

#: 从版本输出里抽取语义化版本号（"codex-cli 0.157.1" → "0.157.1"）
_VERSION_PATTERN = re.compile(r"\d+\.\d+(?:\.\d+)?")


class GenericCliAdapter(CliAgentAdapter):
    """按配置探测一个 CLI Agent。`which` / `probe_runner` 可注入，便于测试。"""

    def __init__(
        self,
        *,
        name: str,
        executable: str,
        version_args: Sequence[str] = ("--version",),
        auth_args: Sequence[str] | None = None,
        auth_ok_markers: Sequence[str] = (),
        auth_missing_markers: Sequence[str] = (),
        run_args: Sequence[str] = (),
        capabilities: Sequence[str] = (),
        which: Callable[[str], str | None] = default_which,
        probe_runner: ProbeRunner = run_probe,
    ) -> None:
        self.name = name
        self._executable = executable
        self._version_args = tuple(version_args)
        self._auth_args = tuple(auth_args) if auth_args else None
        self._auth_ok_markers = tuple(m.lower() for m in auth_ok_markers)
        self._auth_missing_markers = tuple(m.lower() for m in auth_missing_markers)
        self._run_args = tuple(run_args)
        self._capabilities = tuple(capabilities)
        self._which = which
        self._probe = probe_runner
        #: 上次 discovery 命中的可执行文件绝对路径（run_args 拼接时用）
        self._path: str | None = None

    # --- 发现 ---

    def _resolve_path(self) -> str | None:
        """解析可执行文件绝对路径并缓存；未安装返回 None。"""
        if self._path is None:
            self._path = self._which(self._executable)
        return self._path

    def discover(self) -> CliAgentProbe:
        path = self._resolve_path()
        if path is None:
            return CliAgentProbe(
                name=self.name,
                adapter=self.name,
                status=AgentInstallStatus.NOT_INSTALLED,
                executable=self._executable,
                capabilities=self._capabilities,
                detail={"reason": "PATH 中未找到可执行文件"},
            )
        version = self.get_version()
        auth_status = self.check_auth()
        status = AgentInstallStatus.DISCOVERED
        if version:
            status = AgentInstallStatus.VERIFIED
        if auth_status == "ok":
            status = AgentInstallStatus.CONNECTED
        return CliAgentProbe(
            name=self.name,
            adapter=self.name,
            status=status,
            executable=self._executable,
            path=path,
            version=version,
            auth_status=auth_status,
            capabilities=self._capabilities,
        )

    def get_version(self) -> str | None:
        path = self._resolve_path()
        if path is None:
            return None
        try:
            _, output = self._probe((path, *self._version_args))
        except (OSError, TimeoutError):
            return None
        match = _VERSION_PATTERN.search(output)
        if match:
            return match.group(0)
        first_line = output.splitlines()[0].strip() if output else ""
        return first_line or None

    def check_auth(self) -> str:
        path = self._resolve_path()
        if path is None or self._auth_args is None:
            return "unknown"
        try:
            _, output = self._probe((path, *self._auth_args))
        except (OSError, TimeoutError):
            return "unknown"
        text = output.lower()
        # 先判失败标记："not logged in" 里也含 "logged in"，顺序不能反
        if any(marker in text for marker in self._auth_missing_markers):
            return "missing"
        if any(marker in text for marker in self._auth_ok_markers):
            return "ok"
        return "unknown"

    def capabilities(self) -> tuple[str, ...]:
        return self._capabilities

    # --- 起一轮 ---

    def build_run_argv(
        self, instruction: str, *, bootstrap: bool = True, mode: str = "normal"
    ) -> tuple[str, ...]:
        """拼出"跑一条指令"的命令；具体 Agent 的子命令由 run_args 配置。

        `bootstrap=True`（默认）时把 Flux Runtime Bootstrap 拼在指令前（收口方案 §8.2）：
        被 Flux 托管启动的 CLI Agent 需要知道自己在 Flux 里、改动要走 proposal，
        而不是把 workspace 直写当成完成路径。Runtime Identity 由调用方经
        `build_runtime_env` 注入 `env`，与 DSH 侧同一份真源。
        """
        executable = self._path or self._executable
        prompt = compose_instruction(instruction, mode) if bootstrap else instruction
        return (executable, *self._run_args, prompt)
