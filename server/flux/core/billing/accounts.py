"""账号指纹：同一套餐的多个订阅账号必须分开聚合。

供应商额度是**账号级**的：同一台机器换一个 apiKey，看到的就是另一份配额。
本地账本与官方快照因此都要带账号标识，否则两个账号的窗口会互相覆盖。

指纹 = `sha256(凭据)[:8]`，不可逆，只回答「是不是同一个账号」，
不会把 apiKey / token 写进任何表；解析不到凭据时留空（`未识别账号`）。
"""

from __future__ import annotations

import hashlib

from flux.config import Settings
from flux.core.billing.plans import (
    PLAN_CODEX,
    PLAN_OPENCODE_GO,
    PLAN_OPENCODE_ZEN,
    PlanDefinition,
)
from flux.core.billing.sources import codex_account_id, opencode_go_key

#: 官方采集器标识 → 账号来源（内置套餐用；自定义套餐按 key 前缀兜底）
_OPENCODE_SOURCES = ("opencode_go", "opencode")
_CODEX_SOURCES = ("chatgpt_codex", "codex")

#: 空指纹的展示名
UNKNOWN_ACCOUNT_LABEL = "未识别账号"


def account_fingerprint(secret: str | None) -> str:
    """把凭据折成 8 位十六进制指纹；空凭据返回空串。"""
    text = (secret or "").strip()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def account_label(account_key: str) -> str:
    return f"账号 {account_key}" if account_key else UNKNOWN_ACCOUNT_LABEL


def plan_account_source(plan: PlanDefinition) -> str:
    """套餐的账号来源：`opencode` / `codex` / `''`（无法识别）。"""
    if plan.key in (PLAN_OPENCODE_GO, PLAN_OPENCODE_ZEN):
        return "opencode"
    if plan.key == PLAN_CODEX:
        return "codex"
    if plan.official_source in _OPENCODE_SOURCES:
        return "opencode"
    if plan.official_source in _CODEX_SOURCES:
        return "codex"
    return ""


def resolve_plan_accounts(settings: Settings, plans: dict[str, PlanDefinition]) -> dict[str, str]:
    """本机在各套餐上使用的账号指纹（按凭据能力解析；解析不到为空）。"""
    opencode_key = opencode_go_key(settings)
    opencode_fingerprint = account_fingerprint(opencode_key)
    # 只有需要时才读 Codex 登录态（文件可能不存在）
    codex_fingerprint: str | None = None
    resolved: dict[str, str] = {}
    for key, plan in plans.items():
        source = plan_account_source(plan)
        if source == "opencode":
            resolved[key] = opencode_fingerprint
        elif source == "codex":
            if codex_fingerprint is None:
                codex_fingerprint = account_fingerprint(codex_account_id(settings))
            resolved[key] = codex_fingerprint
        else:
            resolved[key] = ""
    return resolved


__all__ = [
    "UNKNOWN_ACCOUNT_LABEL",
    "account_fingerprint",
    "account_label",
    "plan_account_source",
    "resolve_plan_accounts",
]
