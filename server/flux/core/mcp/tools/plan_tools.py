"""计划文档工具 `plan.write`（Plan 模式的核心产出）。

Plan 模式只读：Agent 先读代码与项目上下文，再把计划写成 Flux 自有目录下的一份 Markdown
（`/opt/flux/plans/<task_id>.md`），同时在对话里落一张 `kind=plan_card` 的卡片，手机端
据此渲染可展开的步骤卡。

三条边界：

- 路径固定由服务端拼接（`<plans_dir>/<task_id>.md`），Agent 不能自选路径，因此不可能借
  这个工具写代码或覆盖工作区文件；
- 工具能力位是 `file.read`（所有模式都具备），真正的限制是 Plan 模式白名单——但 Normal /
  Full 模式也允许先用它产出计划，属于只增不减的能力；
- 计划卡消息与该 Run 归属的任务绑定，找不到 Run 时拒绝（不猜任务）。
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

from flux.core.mcp.tools.base import (
    ToolContext,
    ToolSpec,
    optional_str,
    reject_unknown,
    require_str,
)
from flux.core.task_engine.repository import MessageDraft
from flux.enums import Capability, TaskMessageRole
from flux.errors import ValidationError

#: 计划文档目录：Flux 自有目录，不污染用户仓库、不进 git。可用环境变量覆盖（测试/部署）。
PLANS_DIR = Path(os.environ.get("FLUX_PLANS_DIR", "/opt/flux/plans"))

#: 一次计划最多几项步骤（再多卡片读不完）
MAX_STEPS = 12

PLAN_WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "计划标题（一句话说清目标）"},
        "steps": {
            "type": "array",
            "description": f"实施步骤（1–{MAX_STEPS} 项），按执行顺序排列",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "这一步做什么"},
                    "detail": {"type": "string", "description": "（可空）怎么做、验收标准"},
                },
                "required": ["title"],
                "additionalProperties": False,
            },
        },
        "files": {
            "type": "array",
            "description": "（可空）计划会涉及的文件路径",
            "items": {"type": "string"},
        },
        "markdown": {
            "type": "string",
            "description": "（可空）补充说明，追加为计划文档的最后一节",
        },
    },
    "required": ["title", "steps"],
    "additionalProperties": False,
}


def _steps(params: dict[str, Any]) -> list[dict[str, Any]]:
    raw = params.get("steps")
    if not isinstance(raw, list) or not raw:
        raise ValidationError("steps 必须是非空数组", details={"param": "steps"})
    if len(raw) > MAX_STEPS:
        raise ValidationError(
            f"steps 最多 {MAX_STEPS} 项", details={"param": "steps", "max": MAX_STEPS}
        )
    steps: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValidationError("每个步骤必须是对象", details={"param": "steps"})
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValidationError("步骤缺少 title", details={"param": "steps"})
        detail = item.get("detail")
        steps.append(
            {
                "title": title.strip(),
                "detail": detail.strip() if isinstance(detail, str) and detail.strip() else None,
            }
        )
    return steps


def _files(params: dict[str, Any]) -> list[str]:
    raw = params.get("files")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValidationError("files 必须是字符串数组", details={"param": "files"})
    files: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ValidationError("files 每项必须是非空字符串", details={"param": "files"})
        files.append(item.strip())
    return files[:40]


async def _task_for_run(ctx: ToolContext) -> uuid.UUID:
    run_id = ctx.identity.run_id
    if not run_id:
        raise ValidationError("plan.write 必须在 Flux 任务 Run 内调用（当前调用没有关联 Run）")
    run = await ctx.container.run_repo.get(run_id)
    if run is None or run.task_id is None:
        raise ValidationError("plan.write 找不到当前 Run 归属的任务", details={"run_id": run_id})
    return run.task_id


def _render(
    title: str,
    steps: list[dict[str, Any]],
    files: list[str],
    markdown: str | None,
) -> str:
    lines = [f"# {title}", "", "## 实施步骤", ""]
    for index, step in enumerate(steps, 1):
        lines.append(f"{index}. **{step['title']}**")
        if step["detail"]:
            lines.extend(["", f"   {step['detail']}"])
    if files:
        lines.extend(["", "## 涉及文件", ""])
        lines.extend(f"- `{item}`" for item in files)
    if markdown:
        lines.extend(["", "## 补充说明", "", markdown.strip()])
    return "\n".join(lines) + "\n"


def _write_plan(task_id: uuid.UUID, body: str) -> Path:
    """先写临时文件再原子替换：读到的永远是完整计划，不会看到写一半的正文。"""
    PLANS_DIR.mkdir(parents=True, exist_ok=True)
    target = PLANS_DIR / f"{task_id}.md"
    temp = target.with_suffix(".md.tmp")
    temp.write_text(body, encoding="utf-8")
    temp.replace(target)
    return target


async def plan_write(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(params, {"title", "steps", "files", "markdown"})
    title = require_str(params, "title")[:200]
    steps = _steps(params)
    files = _files(params)
    markdown = optional_str(params, "markdown")

    task_id = await _task_for_run(ctx)
    plan_path = _write_plan(task_id, _render(title, steps, files, markdown))

    payload = {
        "title": title,
        "steps": steps,
        "files": files,
        "plan_path": str(plan_path),
        "markdown": markdown,
    }
    content = f"实施计划：{title}（{len(steps)} 步）"
    message = (
        await ctx.container.task_repo.add_messages(
            task_id,
            [
                MessageDraft(
                    role=TaskMessageRole.ASSISTANT.value,
                    kind="plan_card",
                    content=content,
                    payload=payload,
                )
            ],
        )
    )[0]
    await ctx.container.task_stream.publish(
        task_id, "assistant_message", {"message": message.to_dict()}
    )
    return {
        "written": True,
        "plan_path": str(plan_path),
        "steps": len(steps),
        "files": files,
        "message_id": str(message.id),
        "note": "计划已写入 Flux 计划目录并在对话中生成计划卡；Plan 模式不执行任何改动。",
    }


def plan_tools() -> tuple[ToolSpec, ...]:
    return (
        ToolSpec(
            name="plan.write",
            title="写计划文档",
            description=(
                "把实施计划写成 Flux 计划文档并在对话里生成一张计划卡（标题 + 步骤 + 涉及文件）。"
                "Plan 模式下这是唯一允许的写入动作；路径由服务端固定为计划目录，"
                "不会写进工作区。"
            ),
            input_schema=PLAN_WRITE_SCHEMA,
            capability=Capability.FILE_READ,
            handler=plan_write,
        ),
    )
