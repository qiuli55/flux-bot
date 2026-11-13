"""Claude Code 子进程的 Anthropic 鉴权变量解析。

官方文档口径（code.claude.com/docs/en/env-vars，[2026-11-12 快照]）：

- `ANTHROPIC_API_KEY` 以 `X-Api-Key` 头发送，适合直连 `api.anthropic.com`；
- `ANTHROPIC_AUTH_TOKEN` 以 `Authorization: Bearer <value>` 头发送，是
  `ANTHROPIC_BASE_URL` 指向非 Anthropic 主机（LLM 网关）时的推荐方式；
- 两者同时存在会触发 CLI 的鉴权冲突告警，因此任何时刻只注入其中一个。

父进程继承来的同名变量同样要防冲突，但不能一律清空：部署可能直接在进程环境里
配了一套密钥。Flux 注入时只摘另一套；没注入时原样继承，仅两套同时存在才择一。

这里只做纯解析（不读 Settings），调用方负责落进子进程 env 并打日志；日志只出现
变量名，永远不出现变量值。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

API_KEY_VAR = "ANTHROPIC_API_KEY"
AUTH_TOKEN_VAR = "ANTHROPIC_AUTH_TOKEN"
#: 互斥的两个鉴权变量（顺序固定，便于测试与稳定日志）
AUTH_VARS = (API_KEY_VAR, AUTH_TOKEN_VAR)

MODE_AUTO = "auto"
MODE_API_KEY = "api_key"
MODE_AUTH_TOKEN = "auth_token"
AUTH_MODES = (MODE_AUTO, MODE_API_KEY, MODE_AUTH_TOKEN)

#: 视为官方直连的域名：本身及其所有子域
OFFICIAL_DOMAIN = "anthropic.com"


def normalize_auth_mode(mode: str | None) -> str:
    """把配置值归一成 auto / api_key / auth_token；非法值告警后按 auto 处理。"""
    value = (mode or "").strip().lower()
    if not value:
        return MODE_AUTO
    if value in AUTH_MODES:
        return value
    logger.warning(
        "未知的 Claude 鉴权模式 mode=%r，按 auto 处理（可选值：auto / api_key / auth_token）",
        mode,
    )
    return MODE_AUTO


def is_official_base_url(base_url: str | None) -> bool:
    """base_url 是否指向 Anthropic 官方：空值也算（SDK 默认就是官方端点）。"""
    raw = (base_url or "").strip()
    if not raw:
        return True
    # 允许只写主机名（如 api.anthropic.com）：urlsplit 需要 // 才能解析出 netloc
    candidate = raw if "://" in raw else f"//{raw}"
    try:
        host = (urlsplit(candidate).hostname or "").lower().rstrip(".")
    except ValueError:
        host = ""
    return host == OFFICIAL_DOMAIN or host.endswith(f".{OFFICIAL_DOMAIN}")


def resolve_auth_mode(base_url: str | None, mode: str = MODE_AUTO) -> str:
    """解析最终模式：显式模式强制生效，auto 按 base_url 是否官方来选。"""
    resolved = normalize_auth_mode(mode)
    if resolved == MODE_AUTO:
        return MODE_API_KEY if is_official_base_url(base_url) else MODE_AUTH_TOKEN
    return resolved


def auth_mode_to_var(mode: str) -> str:
    """模式 → 环境变量名。"""
    return API_KEY_VAR if normalize_auth_mode(mode) == MODE_API_KEY else AUTH_TOKEN_VAR


def claude_auth_env(key: str | None, base_url: str | None, mode: str = MODE_AUTO) -> dict[str, str]:
    """返回应注入子进程的鉴权变量（最多一个）。

    空 key 返回空字典：既不是 API key 也不是 Bearer token，调用方沿用
    `claude login` 的账号凭据兜底。
    """
    value = (key or "").strip()
    if not value:
        return {}
    return {auth_mode_to_var(resolve_auth_mode(base_url, mode)): value}


def trim_inherited_auth_env(
    env: dict[str, str],
    injected: Mapping[str, str] | None = None,
    base_url: str | None = None,
    mode: str = MODE_AUTO,
) -> list[str]:
    """按 Flux 实际注入的通道裁剪父进程继承来的鉴权变量，返回被摘掉的变量名。

    - Flux 注入了一个鉴权变量：只摘另一套继承值（注入的那个随后会覆盖同名继承值）；
    - Flux 什么都没注入：继承值原样保留，兼容「服务进程自己导出密钥」的部署；
      仅当两套同时继承时才按 `resolve_auth_mode` 的选择留下一个，并告警点名丢弃的
      那个（只打变量名，不打值）。
    """
    injected_vars = [name for name in AUTH_VARS if injected and name in injected]
    inherited = [name for name in AUTH_VARS if name in env]
    removed: list[str] = []
    if injected_vars:
        keep = injected_vars[0]
        removed = [name for name in inherited if name != keep]
    elif len(inherited) == len(AUTH_VARS):
        keep = auth_mode_to_var(resolve_auth_mode(base_url, mode))
        removed = [name for name in inherited if name != keep]
        logger.warning(
            "父进程同时导出了 %s 与 %s，Claude CLI 会报鉴权冲突；"
            "按当前通道保留 %s，丢弃 %s（只保留一个鉴权变量）",
            API_KEY_VAR,
            AUTH_TOKEN_VAR,
            keep,
            ",".join(removed),
        )
    for name in removed:
        env.pop(name, None)
    return removed


__all__ = [
    "API_KEY_VAR",
    "AUTH_MODES",
    "AUTH_TOKEN_VAR",
    "AUTH_VARS",
    "MODE_API_KEY",
    "MODE_AUTH_TOKEN",
    "MODE_AUTO",
    "OFFICIAL_DOMAIN",
    "auth_mode_to_var",
    "claude_auth_env",
    "is_official_base_url",
    "normalize_auth_mode",
    "resolve_auth_mode",
    "trim_inherited_auth_env",
]
