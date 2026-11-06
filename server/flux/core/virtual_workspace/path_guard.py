"""工作区路径守卫：所有碰工作区文件的地方共用这一份"不许逃逸"的实现（§7.6 / §12.5）。

`safe_relative_path` 只做字符串级校验（拒绝对路径与 `..`），但"工作区内"的路径
只要有一环是软链，`Path` 接口的读写就会跟着软链跑到根外——仓库里被提交的
`.flux -> /home/user/.ssh`、`config.py -> /etc/passwd` 都是真实可行的逃逸路径。

因此真正碰盘之前必须再过这一关：逐段 `is_symlink()` fail-closed（指向根内的软链
同样拒绝，不做例外判断）+ realpath 包含性兜底。Apply Engine（写）、Backup Service
（备份 / 回滚）与 Project File Explorer（读）都调用这里，保护逻辑只有这一份真源。
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

from flux.errors import PermissionDeniedError, ValidationError

#: Flux 自己的内部目录（备份、锁、临时文件）。提案不得写进这里，否则 Agent 能改
#: Apply Engine 的回滚依据（备份），等于绕过"改动必须可回退"这条底线。
FLUX_INTERNAL_DIRNAME = ".flux"

#: Git 元数据目录。提案不得触碰 `.git/` 内部——改配置/引用等于绕过版本控制，
#: 删除或改坏它更可能直接毁掉用户仓库（P1-1 路径守卫）。
GIT_INTERNAL_DIRNAME = ".git"

#: 密钥/凭证类文件名（小写精确匹配）。Secret 读取是 MCP 面的硬禁令（目标架构 §3.5），
#: 这里不是配置项，是一份固定名单。
_SECRET_FILENAMES = frozenset(
    {
        ".env",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".htpasswd",
        ".git-credentials",
        "credentials",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }
)

#: `.env.xxx` 里属于"模板/示例"的后缀——没有真实密钥，允许读写。
_SECRET_ENV_ALLOWED_SUFFIXES = frozenset({"example", "sample", "template", "dist", "defaults"})

#: 密钥类扩展名（证书 / 私钥 / 密钥库）
_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore", ".jks")


def is_secret_path(path: str) -> bool:
    """路径是否指向密钥类文件。只看文件名——密钥文件放在哪一层都一样敏感。"""
    name = PurePosixPath((path or "").replace("\\", "/")).name.lower()
    if not name:
        return False
    if name.startswith(".env."):
        return name.rsplit(".", 1)[-1] not in _SECRET_ENV_ALLOWED_SUFFIXES
    if name in _SECRET_FILENAMES or name.startswith(("id_rsa", "id_ed25519", "id_dsa", "id_ecdsa")):
        return True
    return name.endswith(_SECRET_SUFFIXES)


def ensure_not_secret_path(path: str, *, where: str = "MCP 能力面") -> None:
    """拒绝一切密钥类文件读写；命中即 403，不做例外判断（fail-closed）。"""
    if is_secret_path(path):
        raise PermissionDeniedError(
            f"{where}禁止读写密钥/凭证类文件：{path}",
            details={"path": path, "policy": "secret.read 是硬禁令（目标架构 §3.5）"},
        )


def ensure_not_flux_internal(relative: Path) -> None:
    """拒绝写进 Flux 内部目录 `.flux/`（备份、锁都在那）。"""
    if relative.parts and relative.parts[0] == FLUX_INTERNAL_DIRNAME:
        raise ValidationError(
            f"路径落在 Flux 内部目录 {FLUX_INTERNAL_DIRNAME}/ 内，已拒绝：{relative.as_posix()}",
            details={"path": relative.as_posix()},
        )


def ensure_not_git_internal(relative: Path) -> None:
    """拒绝触碰 `.git/` 内部（P1-1）：提案永远不该改用户的版本控制元数据。"""
    if relative.parts and relative.parts[0] == GIT_INTERNAL_DIRNAME:
        raise ValidationError(
            f"路径落在 Git 元数据目录 {GIT_INTERNAL_DIRNAME}/ 内，已拒绝：{relative.as_posix()}",
            details={"path": relative.as_posix()},
        )


def within_root(root: Path, candidate: Path) -> bool:
    """`candidate` 的真实路径（realpath，解析全部软链）是否落在 `root` 之内。"""
    root_real = Path(os.path.realpath(root))
    candidate_real = Path(os.path.realpath(candidate))
    return candidate_real == root_real or root_real in candidate_real.parents


def resolve_within_root(root: Path, relative: Path) -> Path:
    """在工作区根内解析相对路径；沿途任何一段是软链都 fail-closed 拒绝。

    返回值与 `root / relative` 相同——校验通过就代表"沿这条路径碰盘不会跑出根"。
    """
    if not relative.parts:
        return root
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            resolved = Path(os.path.realpath(current))
            if not within_root(root, resolved):
                raise ValidationError(
                    f"路径是符号链接且指向工作区根之外，已拒绝：{relative.as_posix()}",
                    details={"path": relative.as_posix(), "resolved": str(resolved)},
                )
            raise ValidationError(
                f"路径是符号链接，本接口不跟随符号链接：{relative.as_posix()}",
                details={"path": relative.as_posix()},
            )
    if not within_root(root, current):
        raise ValidationError(
            f"路径越出工作区根，已拒绝：{relative.as_posix()}",
            details={"path": relative.as_posix()},
        )
    return current


__all__ = [
    "FLUX_INTERNAL_DIRNAME",
    "GIT_INTERNAL_DIRNAME",
    "ensure_not_flux_internal",
    "ensure_not_git_internal",
    "ensure_not_secret_path",
    "is_secret_path",
    "resolve_within_root",
    "within_root",
]
