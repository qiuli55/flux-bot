"""给 Bot 聊天用的 GitHub 只读工具。

只查当前账号自己已授权的数据（令牌来自 `GitHubConnectorService`），不提供任何写操作。
工具返回裁剪过的摘要，避免把 GitHub 的完整 JSON 塞进上下文。
"""

from __future__ import annotations

from typing import Any

from flux.core.github.service import GitHubConnectorService
from flux.errors import BadRequestError

_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "github_list_repos",
            "description": "列出当前已连接 GitHub 账号可见的仓库（按最近更新排序）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "visibility": {
                        "type": "string",
                        "enum": ["all", "public", "private"],
                        "description": "仓库可见性，默认 all",
                    },
                    "sort": {
                        "type": "string",
                        "enum": ["updated", "pushed", "full_name"],
                        "description": "排序方式，默认 updated",
                    },
                    "per_page": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "description": "返回条数，默认 20",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_get_repo",
            "description": "查看一个仓库的概况（描述、语言、默认分支、star/fork、开放 issue 数）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "repository": {
                        "type": "string",
                        "description": "仓库全名，格式 owner/repo，例如 qiuli55/flux",
                    },
                },
                "required": ["repository"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_list_issues",
            "description": "列出一个仓库的 Issue（不包含 Pull Request）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "repository": {"type": "string", "description": "owner/repo"},
                    "state": {
                        "type": "string",
                        "enum": ["open", "closed", "all"],
                        "description": "默认 open",
                    },
                    "per_page": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "required": ["repository"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_list_pulls",
            "description": "列出一个仓库的 Pull Request。",
            "parameters": {
                "type": "object",
                "properties": {
                    "repository": {"type": "string", "description": "owner/repo"},
                    "state": {
                        "type": "string",
                        "enum": ["open", "closed", "all"],
                        "description": "默认 open",
                    },
                    "per_page": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "required": ["repository"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_list_commits",
            "description": "列出一个仓库最近的提交记录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "repository": {"type": "string", "description": "owner/repo"},
                    "branch": {"type": "string", "description": "分支名，留空用默认分支"},
                    "per_page": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "required": ["repository"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_list_branches",
            "description": "列出一个仓库的分支。",
            "parameters": {
                "type": "object",
                "properties": {
                    "repository": {"type": "string", "description": "owner/repo"},
                    "per_page": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "required": ["repository"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_get_readme",
            "description": "读取一个仓库的 README（markdown 原文，过长会截断）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "repository": {"type": "string", "description": "owner/repo"},
                },
                "required": ["repository"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "github_list_actions_runs",
            "description": "查看一个仓库最近的 GitHub Actions 工作流运行记录与结论。",
            "parameters": {
                "type": "object",
                "properties": {
                    "repository": {"type": "string", "description": "owner/repo"},
                    "branch": {"type": "string", "description": "只看某个分支，可留空"},
                    "per_page": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "required": ["repository"],
            },
        },
    },
]

GITHUB_TOOLS: tuple[dict[str, Any], ...] = tuple(_TOOL_DEFS)

#: 工具返回给模型的单次文本上限（再长就截断，避免撑爆上下文）
_MAX_README_CHARS = 6000


def _repo(args: dict[str, Any]) -> str:
    value = str(args.get("repository") or "").strip().strip("/")
    if value.count("/") != 1 or not all(value.split("/")):
        raise BadRequestError(
            "repository 必须是 owner/repo 格式，例如 qiuli55/flux",
            details={"field": "repository", "value": value},
        )
    return value


def _per_page(args: dict[str, Any], default: int, maximum: int = 50) -> int:
    try:
        value = int(args.get("per_page") or default)
    except (TypeError, ValueError):
        value = default
    return max(1, min(maximum, value))


def _login(user: Any) -> str:
    return str(user.get("login") or "") if isinstance(user, dict) else ""


async def run_github_tool(
    service: GitHubConnectorService,
    account_key: str,
    name: str,
    arguments: dict[str, Any],
) -> Any:
    """执行一个 GitHub 工具。未知工具直接报错，让模型换一个可用的。"""
    args = arguments if isinstance(arguments, dict) else {}

    if name == "github_list_repos":
        params: dict[str, Any] = {
            "sort": str(args.get("sort") or "updated"),
            "per_page": _per_page(args, 20),
            "affiliation": "owner,collaborator,organization_member",
        }
        visibility = str(args.get("visibility") or "all")
        if visibility in {"public", "private"}:
            params["visibility"] = visibility
        rows = await service.api_get(account_key, "/user/repos", params=params)
        return {
            "count": len(rows or []),
            "repos": [
                {
                    "full_name": item.get("full_name"),
                    "private": item.get("private"),
                    "description": item.get("description"),
                    "language": item.get("language"),
                    "stars": item.get("stargazers_count"),
                    "default_branch": item.get("default_branch"),
                    "updated_at": item.get("updated_at"),
                }
                for item in (rows or [])
                if isinstance(item, dict)
            ],
        }

    if name == "github_get_repo":
        repo = _repo(args)
        item = await service.api_get(account_key, f"/repos/{repo}")
        return {
            "full_name": item.get("full_name"),
            "description": item.get("description"),
            "private": item.get("private"),
            "language": item.get("language"),
            "default_branch": item.get("default_branch"),
            "stars": item.get("stargazers_count"),
            "forks": item.get("forks_count"),
            "open_issues": item.get("open_issues_count"),
            "html_url": item.get("html_url"),
            "updated_at": item.get("updated_at"),
        }

    if name == "github_list_issues":
        repo = _repo(args)
        rows = await service.api_get(
            account_key,
            f"/repos/{repo}/issues",
            params={
                "state": str(args.get("state") or "open"),
                "per_page": _per_page(args, 20),
            },
        )
        issues = [
            {
                "number": item.get("number"),
                "title": item.get("title"),
                "state": item.get("state"),
                "author": _login(item.get("user")),
                "labels": [
                    label.get("name")
                    for label in (item.get("labels") or [])
                    if isinstance(label, dict)
                ],
                "comments": item.get("comments"),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
            }
            for item in (rows or [])
            if isinstance(item, dict) and "pull_request" not in item
        ]
        return {"count": len(issues), "issues": issues}

    if name == "github_list_pulls":
        repo = _repo(args)
        rows = await service.api_get(
            account_key,
            f"/repos/{repo}/pulls",
            params={
                "state": str(args.get("state") or "open"),
                "per_page": _per_page(args, 20),
            },
        )
        pulls = [
            {
                "number": item.get("number"),
                "title": item.get("title"),
                "state": item.get("state"),
                "author": _login(item.get("user")),
                "draft": item.get("draft"),
                "head": (item.get("head") or {}).get("ref"),
                "base": (item.get("base") or {}).get("ref"),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
            }
            for item in (rows or [])
            if isinstance(item, dict)
        ]
        return {"count": len(pulls), "pulls": pulls}

    if name == "github_list_commits":
        repo = _repo(args)
        params = {"per_page": _per_page(args, 20)}
        branch = str(args.get("branch") or "").strip()
        if branch:
            params["sha"] = branch
        rows = await service.api_get(account_key, f"/repos/{repo}/commits", params=params)
        commits = []
        for item in rows or []:
            if not isinstance(item, dict):
                continue
            commit = item.get("commit") or {}
            commits.append(
                {
                    "sha": str(item.get("sha") or "")[:8],
                    "message": str(commit.get("message") or "").splitlines()[0][:200],
                    "author": (commit.get("author") or {}).get("name"),
                    "date": (commit.get("author") or {}).get("date"),
                }
            )
        return {"count": len(commits), "commits": commits}

    if name == "github_list_branches":
        repo = _repo(args)
        rows = await service.api_get(
            account_key,
            f"/repos/{repo}/branches",
            params={"per_page": _per_page(args, 30, maximum=100)},
        )
        return {
            "count": len(rows or []),
            "branches": [
                {"name": item.get("name"), "protected": item.get("protected")}
                for item in (rows or [])
                if isinstance(item, dict)
            ],
        }

    if name == "github_get_readme":
        repo = _repo(args)
        text = await service.api_get_text(
            account_key,
            f"/repos/{repo}/readme",
            accept="application/vnd.github.raw+json",
        )
        truncated = len(text) > _MAX_README_CHARS
        return {
            "repository": repo,
            "truncated": truncated,
            "content": text[:_MAX_README_CHARS],
        }

    if name == "github_list_actions_runs":
        repo = _repo(args)
        params = {"per_page": _per_page(args, 20)}
        branch = str(args.get("branch") or "").strip()
        if branch:
            params["branch"] = branch
        data = (
            await service.api_get(account_key, f"/repos/{repo}/actions/runs", params=params) or {}
        )
        runs = [
            {
                "name": item.get("name"),
                "status": item.get("status"),
                "conclusion": item.get("conclusion"),
                "branch": item.get("head_branch"),
                "event": item.get("event"),
                "created_at": item.get("created_at"),
                "html_url": item.get("html_url"),
            }
            for item in (data.get("workflow_runs") or [])
            if isinstance(item, dict)
        ]
        return {"total_count": data.get("total_count"), "runs": runs}

    raise BadRequestError(f"未知的 GitHub 工具：{name}", details={"tool": name})


__all__ = ["GITHUB_TOOLS", "run_github_tool"]
