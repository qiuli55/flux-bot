"""DSH Run API 请求模型（集成方案 §18 Phase 1）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DshRunCreateRequest(BaseModel):
    """起一次 DSH Run：instruction 必填，session_id 留空则由服务端生成。

    agent_id 留空 = 平台配置的内置 Agent；填 Agent Registry 的 canonical UUID
    则由该 Agent 执行这次 Run（「单独跟某一个 agent 干活」用它）。
    """

    instruction: str = Field(min_length=1)
    session_id: str | None = None
    agent_id: str | None = None
    #: 任务级模型选择（Web Solo 对话用）：key_id 非空 = 使用密钥库中的模型
    model: str | None = Field(default=None, max_length=128)
    key_id: str | None = Field(default=None, max_length=64)


__all__ = ["DshRunCreateRequest"]
