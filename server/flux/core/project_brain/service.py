"""Project Brain 服务层（主规格 §5.6；实施计划 ⑪）。

第一版是**结构化记忆**，不是 RAG：七个分区各自存文本，读取时按固定顺序拼成
一段可供 Agent 直接消费的项目上下文。不上 embedding、不建向量索引——
按实施计划 ⑪ 的边界，"以后再加 Semantic Search"，现在强行引入只会把
闭环验证拖长。

分区的两种写入语义（见 `flux.enums.SINGLETON_BRAIN_SECTIONS`）：

- `overview` / `tech_stack` / `architecture` / `coding_rules` / `progress`：
  项目**现状**，覆盖写入（`progress` 由助理从线程摘要自动汇总）；
- `decisions` / `agent_notes`：**累积**记录，只追加，历史不可被覆盖。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flux.core.event.bus import EventBus, Events
from flux.core.memory.policies import PROJECT_SECTION_MAX_ENTRIES
from flux.core.memory.secrets import ensure_no_secret
from flux.core.project_brain.repository import ProjectBrainRepository
from flux.core.project_scanner.scanner import ProjectProfile, ProjectScanner
from flux.enums import SINGLETON_BRAIN_SECTIONS, BrainSection
from flux.errors import ValidationError
from flux.logging import get_logger
from flux.models.project import Project, ProjectMemory

logger = get_logger(__name__)

#: 拼上下文时的分区顺序与标题（与实施计划 ⑪ 的记忆分区一一对应）
SECTION_TITLES: dict[BrainSection, str] = {
    BrainSection.OVERVIEW: "Project Overview",
    BrainSection.TECH_STACK: "Tech Stack",
    BrainSection.ARCHITECTURE: "Architecture",
    BrainSection.CODING_RULES: "Coding Rules",
    BrainSection.PROGRESS: "Project Progress",
    BrainSection.DECISIONS: "Decisions",
    BrainSection.AGENT_NOTES: "Agent Notes",
}

#: 累积型分区在上下文里最多带多少条（只带最新的，避免上下文无限膨胀）
CONTEXT_MAX_ENTRIES = 20


@dataclass(frozen=True)
class ScanOutcome:
    """一次扫描的结果：画像本身 + 由它写入 Brain 的条目。"""

    profile: ProjectProfile
    recorded: tuple[ProjectMemory, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile.to_dict(),
            "recorded": [entry.to_dict() for entry in self.recorded],
        }


def render_overview(profile: ProjectProfile, *, scanned_at: str) -> str:
    """把画像渲染成 Overview 文本（自动生成，可被人工或 Agent 覆盖）。"""
    git = (
        f"是（当前分支 {profile.git_branch}）"
        if profile.git_repository and profile.git_branch
        else ("是" if profile.git_repository else "不是 Git 仓库")
    )
    return "\n".join(
        [
            "## 项目画像（由 Project Scanner 自动生成，可被覆盖）",
            "",
            f"- 根目录：{profile.root}",
            f"- 文件数：{profile.files_scanned}"
            f"（{'已按上限裁剪' if profile.truncated else '未裁剪'}）",
            f"- 顶层结构：{'、'.join(profile.structure) or '空目录'}",
            f"- 清单与配置：{'、'.join(profile.manifests) or '未发现'}",
            f"- Git：{git}",
            f"- 扫描时间：{scanned_at}",
        ]
    )


def render_tech_stack(profile: ProjectProfile) -> str:
    """把画像渲染成 Tech Stack 文本。"""

    def joined(values: tuple[str, ...]) -> str:
        return "、".join(values) if values else "未识别"

    primary = f"（主：{profile.primary_language}）" if profile.primary_language else ""
    return "\n".join(
        [
            f"- 语言：{joined(profile.languages)}{primary}",
            f"- 框架：{joined(profile.frameworks)}",
            f"- 包管理器：{profile.package_manager or '未识别'}",
            f"- 入口文件：{joined(profile.entry_points)}",
            f"- 测试命令：{joined(profile.test_commands)}",
            f"- 构建命令：{joined(profile.build_commands)}",
        ]
    )


class ProjectBrain:
    def __init__(
        self,
        repository: ProjectBrainRepository,
        *,
        scanner: ProjectScanner | None = None,
        bus: EventBus | None = None,
        workspace_root: str | Path | None = None,
        section_max_entries: int = PROJECT_SECTION_MAX_ENTRIES,
    ) -> None:
        self._repository = repository
        self._scanner = scanner
        self._bus = bus
        self._workspace_root = workspace_root
        # 累积型分区的容量上限（批次② §4.2）：超出即裁掉最旧的，库不因只追加而无界增长
        self._section_max_entries = section_max_entries

    @property
    def workspace_root(self) -> str | Path | None:
        """当前扫描/画像的工作区根（桌面端切换工作目录时由 Container 赋值）。"""
        return self._workspace_root

    @workspace_root.setter
    def workspace_root(self, value: str | Path | None) -> None:
        self._workspace_root = value

    # --- 项目 ---

    async def create_project(
        self, *, name: str, repository: str | None = None, meta: dict | None = None
    ) -> Project:
        clean = (name or "").strip()
        if not clean:
            raise ValidationError("项目名称不能为空")
        return await self._repository.create_project(name=clean, repository=repository, meta=meta)

    async def get_project(self, project_id: str | uuid.UUID) -> Project:
        return await self._repository.get_project(project_id)

    async def list_projects(self) -> list[Project]:
        return await self._repository.list_projects()

    # --- 扫描（⑩ → ⑪）---

    async def scan(
        self,
        project_id: str | uuid.UUID,
        *,
        workspace_root: str | Path | None = None,
        record: bool = True,
    ) -> ScanOutcome:
        """扫描工作区并（默认）把画像写进 Brain。

        扫描是纯只读的文件遍历，放到线程里执行，不阻塞事件循环。
        """
        project = await self._repository.get_project(project_id)
        if self._scanner is None:
            raise ValidationError("未接入 Project Scanner，无法扫描项目")
        root = workspace_root if workspace_root is not None else self._workspace_root
        profile = await asyncio.to_thread(self._scanner.scan, workspace_root=root)

        recorded: list[ProjectMemory] = []
        if record:
            scanned_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            meta = {
                "source": "scanner",
                "workspace_root": profile.root,
                "scanned_at": scanned_at,
                "truncated": profile.truncated,
            }
            recorded.append(
                await self.write(
                    project.id,
                    section=BrainSection.OVERVIEW,
                    content=render_overview(profile, scanned_at=scanned_at),
                    meta=meta,
                )
            )
            recorded.append(
                await self.write(
                    project.id,
                    section=BrainSection.TECH_STACK,
                    content=render_tech_stack(profile),
                    meta=meta,
                )
            )
        if self._bus is not None:
            await self._bus.publish(
                Events.PROJECT_SCANNED,
                {
                    "project_id": str(project.id),
                    "root": profile.root,
                    "files_scanned": profile.files_scanned,
                    "primary_language": profile.primary_language,
                    "recorded": [entry.type for entry in recorded],
                },
            )
        return ScanOutcome(profile=profile, recorded=tuple(recorded))

    # --- 记忆 ---

    async def write(
        self,
        project_id: str | uuid.UUID,
        *,
        section: BrainSection,
        content: str,
        meta: dict | None = None,
    ) -> ProjectMemory:
        """按分区语义写入：现状型分区覆盖，累积型分区追加。

        写入前先过记忆红线（密钥 / 令牌 / 凭证永不入库——批次② §4.2）；
        累积型分区追加后按容量上限裁剪最旧条目（清理机制）。
        """
        text = (content or "").strip()
        if not text:
            raise ValidationError("记忆内容不能为空", details={"section": str(section)})
        ensure_no_secret(text, where="Project Memory")
        if section in SINGLETON_BRAIN_SECTIONS:
            entry = await self._repository.replace_section(
                project_id, section=section, content=text, meta=meta
            )
        else:
            entry = await self._repository.append_entry(
                project_id, section=section, content=text, meta=meta
            )
            await self._repository.prune_section(
                project_id, section=section, keep=self._section_max_entries
            )
        if self._bus is not None:
            await self._bus.publish(
                Events.BRAIN_UPDATED,
                {
                    "project_id": str(entry.project_id),
                    "section": entry.type,
                    "entry_id": str(entry.id),
                    "replaced": section in SINGLETON_BRAIN_SECTIONS,
                },
            )
        return entry

    async def sections(self, project_id: str | uuid.UUID) -> dict[str, list[dict[str, Any]]]:
        """按分区列出全部记忆（七个分区都会出现，缺的给空列表，便于前端直接渲染）。"""
        entries = await self._repository.entries(project_id)
        grouped: dict[str, list[dict[str, Any]]] = {section.value: [] for section in BrainSection}
        for entry in entries:
            grouped.setdefault(entry.type, []).append(entry.to_dict())
        return grouped

    async def context(
        self,
        project_id: str | uuid.UUID,
        *,
        sections: list[BrainSection] | None = None,
        max_entries: int = CONTEXT_MAX_ENTRIES,
    ) -> str:
        """把 Brain 拼成一段项目上下文，供 Agent 执行前注入（跨会话的项目长期记忆）。"""
        project = await self._repository.get_project(project_id)
        wanted = sections or list(BrainSection)
        entries = await self._repository.entries(project_id)
        by_section: dict[str, list[ProjectMemory]] = {}
        for entry in entries:
            by_section.setdefault(entry.type, []).append(entry)

        blocks: list[str] = [f"# 项目：{project.name}"]
        if project.repository:
            blocks.append(f"仓库：{project.repository}")
        for section in wanted:
            found = by_section.get(section.value, [])
            if not found:
                continue
            # 现状型分区只有一条（覆盖写入）；累积型只带最新的若干条
            current = found if section not in SINGLETON_BRAIN_SECTIONS else found[-1:]
            current = current[-max_entries:]
            body = (
                current[0].content
                if section in SINGLETON_BRAIN_SECTIONS
                else "\n".join(f"- {entry.content}" for entry in current)
            )
            blocks.append(f"\n## {SECTION_TITLES[section]}\n{body}")
        return "\n".join(blocks)
