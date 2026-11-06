"""领域异常与统一错误码（主规格 §12.10：所有 API 必须有错误码）。

异常 → 统一响应体的映射在 flux.api.errors 中处理。
"""

from __future__ import annotations

from typing import Any


class AIOSError(Exception):
    """所有可预期领域异常的基类。"""

    code: str = "internal_error"
    http_status: int = 500

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        http_status: int | None = None,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status
        self.details = details


class NotFoundError(AIOSError):
    code = "not_found"
    http_status = 404


class ConflictError(AIOSError):
    code = "conflict"
    http_status = 409


class InvalidTransitionError(ConflictError):
    """Agent / Virtual Workspace 状态机非法跃迁（主规格 §5.1、§7.2）。"""

    code = "invalid_state_transition"


class ProposalExpiredError(ConflictError):
    """提案已过期/被更新提案取代，不能再审核或落盘（P0-02 生命周期）。

    与 InvalidTransitionError 分开：前者是"时间到了"，后者是"状态机不允许"，
    两者对用户的下一步指引不同（过期要重新发起提案，非法跃迁通常是用错了接口）。
    """

    code = "proposal_expired"


class ValidationError(AIOSError):
    code = "validation_error"
    http_status = 422


class BadRequestError(AIOSError):
    """请求语义非法（如标识串不是合法 UUID）。主规格 §12.10 错误码 400。"""

    code = "bad_request"
    http_status = 400


class PermissionDeniedError(AIOSError):
    code = "permission_denied"
    http_status = 403


class AuthenticationError(AIOSError):
    """调用方身份未通过校验（MCP 面的 401，目标架构 §3.2 fail-closed）。

    无令牌、令牌非法、令牌已撤销一律同一个错误——区分原因等于给攻击者提供探测信号。
    """

    code = "unauthenticated"
    http_status = 401


class ProviderNotConfiguredError(AIOSError):
    code = "provider_not_configured"
    http_status = 503


class ConfigurationError(AIOSError):
    """能力未启用或未配置（例如 DSH Agent Runtime 未开启）。集成方案 §18 Phase 1。"""

    code = "configuration_error"
    http_status = 503


class ProviderError(AIOSError):
    """上游模型供应商调用失败（主规格 §5.1 错误处理）。"""

    code = "provider_error"
    http_status = 502


class ProviderAuthError(ProviderError):
    """上游鉴权失败。注意：是本服务对上游的 Key 无效，不是客户端鉴权失败。"""

    code = "provider_auth_error"


class ProviderBadRequestError(ProviderError):
    """上游拒绝了请求（参数、模型名不存在等）。"""

    code = "provider_bad_request"


class ProviderRateLimitedError(ProviderError):
    code = "provider_rate_limited"
    http_status = 503


class ProviderUnavailableError(ProviderError):
    code = "provider_unavailable"
    http_status = 503


class ProviderTimeoutError(ProviderError):
    code = "provider_timeout"
    http_status = 504


class ConnectorNotRegisteredError(AIOSError):
    code = "connector_not_registered"
    http_status = 404


class ApplyFailedError(AIOSError):
    """Apply 在落盘阶段失败（校验不通过 / 跑测试失败）。

    调用方报错时提案已被置为 `failed` 并尽可能回滚原文件，错误详情落在提案的 apply_error。
    """

    code = "apply_failed"
    http_status = 500


class GitError(AIOSError):
    """git 命令执行失败（实施计划 ⑨）。

    git 的非零退出与超时都归到这里，错误详情带 `args` / `exit_code` / `stderr`，
    便于调用方直接把 git 的原话呈现给用户。
    """

    code = "git_failed"
    http_status = 500


class NotAGitRepositoryError(GitError):
    """工作区根不是 Git 仓库。这是环境问题而非命令写错，单独给一个错误码。"""

    code = "not_a_git_repository"
    http_status = 409
