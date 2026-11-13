"""按 Flux 云用户隔离的公共判定。

Phase 2 起 User 层记忆、API Key、账号连接与用量都按「人」隔离。身份来源统一是
`flux.api.auth.current_cloud_user_id`（云 JWT 的 `sub` → `FLUX_OWNER_CLOUD_USER_ID`）；
本模块只提供不依赖 FastAPI 的纯判定，供仓储 / 服务层复用。

历史数据的归属规则（全仓库一致）：

- 显式 NULL（或旧账单里的空串）表示「Phase 2 之前写入的老数据」；
- 老数据归 `FLUX_OWNER_CLOUD_USER_ID` 配置的主用户；
- 没有配置主用户时，老数据不属于任何人（不会被别的用户看到）。

环境层记忆是平台规则，保持全局，不参与这里的过滤。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import ColumnElement, or_

#: 显式区分「没传参数」与「传了 None」：前者表示内部调用（沿用旧口径），
#: 后者表示已经确认「本次请求没有可解析的人」。
UNSET: object = object()


def normalize_cloud_user_id(value: Any) -> str | None:
    """云用户 id 一律按非空字符串处理；None / 空白视为未提供。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def owns(
    record_user_id: Any,
    current_user_id: Any,
    owner_user_id: Any,
) -> bool:
    """一条记录是否属于当前用户（含「老 NULL 归主用户」的兼容规则）。"""
    current = normalize_cloud_user_id(current_user_id)
    if current is None:
        return False
    record = normalize_cloud_user_id(record_user_id)
    if record is None:
        # 历史 NULL 只对配置的主用户可见。
        return current == normalize_cloud_user_id(owner_user_id)
    return record == current


def owned_clause(
    column: Any,
    current_user_id: str,
    owner_user_id: str | None,
) -> ColumnElement[bool]:
    """SQL 版本的 `owns`：当前用户的行 + （仅主用户）历史 NULL / 空串行。

    `usage_records.account_key` 等旧列用空串表示未识别，所以这里把空串也当作
    历史数据；记忆 / 密钥表的新行不会写空串，条件多余但无害。
    """
    conditions: list[ColumnElement[bool]] = [column == current_user_id]
    if current_user_id == normalize_cloud_user_id(owner_user_id):
        conditions.append(column.is_(None))
        conditions.append(column == "")
    return or_(*conditions)


__all__ = ["UNSET", "normalize_cloud_user_id", "owned_clause", "owns"]
