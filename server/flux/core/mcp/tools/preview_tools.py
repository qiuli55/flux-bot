"""预演工具 `proposal.preview`：提交提案前，在临时副本里先跑一遍测试。

与 `proposal.create` 吃同一份 payload，区别只在"没有副作用"：改动落在临时副本，
测试命令来自平台配置（`FLUX_TEST_COMMAND`），跑完副本即删。真实工作区一个字节都不动。

它解决的是自检的语义问题：提案落盘前磁盘上仍是旧代码，`terminal.execute` 跑出来的
绿是假绿；只有把改动先落进副本再跑，测到的才是提案后的世界。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from flux.core.mcp.tools.base import ToolContext, ToolSpec, reject_unknown
from flux.core.virtual_workspace.preview import run_preview
from flux.core.virtual_workspace.proposal_parser import parse_code_change_set
from flux.enums import Capability
from flux.errors import ValidationError

PREVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "payload": {
            "description": (
                "与 proposal.create 相同的提案内容：形如 "
                "{summary, changes:[{path, op, content, reason}]} 的 JSON 对象或其 JSON 文本。"
                "改动会应用到工作区的临时副本后跑项目配置的测试命令，不写入真实文件。"
            ),
            "anyOf": [{"type": "object"}, {"type": "string"}],
        },
        # 预演没有副作用，也不落库归属；但平台执行指令会让 Agent 一并带上这两个标识，
        # 与 proposal.create 保持一致地接受并忽略，避免整次预演因多余入参被拒。
        "task_id": {"type": "string", "description": "（可空）该改动服务的任务 UUID"},
        "project_id": {"type": "string", "description": "（可空）项目 UUID"},
    },
    "required": ["payload"],
    "additionalProperties": False,
}


async def proposal_preview(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    reject_unknown(params, {"payload", "task_id", "project_id"})
    raw = _payload_text(params.get("payload"))
    change_set = parse_code_change_set(raw, source="proposal.preview")
    settings = ctx.container.settings
    # 复制工作区与跑测试都是阻塞操作，放线程里执行，避免卡住 MCP 事件循环
    result = await asyncio.to_thread(
        run_preview,
        workspace_root=settings.workspace_root,
        changes=change_set.changes,
        test_command=settings.test_command,
        timeout_seconds=settings.test_timeout_seconds,
    )
    return result.to_dict()


def _payload_text(payload: Any) -> str:
    if isinstance(payload, (dict, list)):
        return json.dumps(payload, ensure_ascii=False)
    if isinstance(payload, str) and payload.strip():
        return payload
    raise ValidationError("参数 payload 必须是提案对象或其 JSON 文本", details={"param": "payload"})


def preview_tools() -> tuple[ToolSpec, ...]:
    return (
        ToolSpec(
            name="proposal.preview",
            title="预演提案并跑测试",
            description=(
                "把一组文件改动应用到工作区的临时副本，再跑项目配置的测试命令，返回通过与否"
                "及输出摘要；真实文件不会被改动。提交 proposal.create 前用它自检——"
                "直接 terminal.execute 跑的是尚未落盘的旧代码，结果不可信。"
            ),
            input_schema=PREVIEW_SCHEMA,
            capability=Capability.TERMINAL_EXECUTE,
            handler=proposal_preview,
        ),
    )
