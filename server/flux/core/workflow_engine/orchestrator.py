"""多 Agent 工作流编排（主规格 §6.6 典型工作流）。

M0 只做"把请求展开成有序步骤链"这一步——这是可验证的确定行为。
真正按步骤调度 Agent、处理失败升级与人工审批点，属 M1 Task Engine 交付物。
"""

from __future__ import annotations

from dataclasses import dataclass

from flux.enums import AgentRole
from flux.errors import NotFoundError


@dataclass(frozen=True)
class WorkflowStep:
    order: int
    role: AgentRole
    instruction: str

    def to_dict(self) -> dict[str, object]:
        return {"order": self.order, "role": str(self.role), "instruction": self.instruction}


def _steps(request: str, roles: list[tuple[AgentRole, str]]) -> list[WorkflowStep]:
    return [
        WorkflowStep(order=index, role=role, instruction=template.format(request=request))
        for index, (role, template) in enumerate(roles, start=1)
    ]


class FeatureDevelopmentWorkflow:
    """功能开发：Tech Lead 分析 → Architect 设计 → Developer 实现 → Reviewer 审查 → Tester 验证。"""

    name = "feature_development"

    def plan(self, request: str) -> list[WorkflowStep]:
        return _steps(
            request,
            [
                (AgentRole.TECH_LEAD, "分析需求并拆解任务：{request}"),
                (AgentRole.ARCHITECT, "为以下需求设计技术方案：{request}"),
                (
                    AgentRole.DEVELOPER,
                    "按已批准的设计实现改动（产出 Virtual Workspace 提案）：{request}",
                ),
                (AgentRole.REVIEWER, "审查代码质量、安全性与测试影响：{request}"),
                (AgentRole.TESTER, "制定并执行测试计划，回传失败项：{request}"),
            ],
        )


class BugFixWorkflow:
    """缺陷修复（主规格 §6.6：简单缺陷先由轻量 Agent 排查，不成功再升级到更强的编码 Agent）。"""

    name = "bug_fix"

    def plan(self, request: str, *, escalated: bool = False) -> list[WorkflowStep]:
        if escalated:
            return _steps(
                request,
                [
                    (AgentRole.TECH_LEAD, "评估升级原因并重新定位缺陷：{request}"),
                    (AgentRole.DEVELOPER, "产出修复提案：{request}"),
                    (AgentRole.REVIEWER, "复核修复方案：{request}"),
                    (AgentRole.TESTER, "跑回归测试确认修复：{request}"),
                ],
            )
        return _steps(
            request,
            [
                (AgentRole.TESTER, "复现并定位缺陷：{request}"),
                (AgentRole.DEVELOPER, "产出修复提案：{request}"),
                (AgentRole.REVIEWER, "复核修复方案：{request}"),
            ],
        )


class ArchitectureChangeWorkflow:
    """架构变更：Architect 出方案 → Tech Lead 评估影响 → Developer 实施迁移 → Tester 跑回归。"""

    name = "architecture_change"

    def plan(self, request: str) -> list[WorkflowStep]:
        return _steps(
            request,
            [
                (AgentRole.ARCHITECT, "产出架构变更方案：{request}"),
                (AgentRole.TECH_LEAD, "评估变更影响面与风险：{request}"),
                (AgentRole.DEVELOPER, "实施迁移：{request}"),
                (AgentRole.TESTER, "执行回归测试：{request}"),
            ],
        )


WORKFLOWS: dict[str, object] = {
    FeatureDevelopmentWorkflow.name: FeatureDevelopmentWorkflow(),
    BugFixWorkflow.name: BugFixWorkflow(),
    ArchitectureChangeWorkflow.name: ArchitectureChangeWorkflow(),
}


def plan_workflow(name: str, request: str) -> list[WorkflowStep]:
    workflow = WORKFLOWS.get(name)
    if workflow is None:
        raise NotFoundError(
            f"未知工作流 {name}",
            details={"workflow": name, "available": sorted(WORKFLOWS)},
        )
    return workflow.plan(request)  # type: ignore[attr-defined]
