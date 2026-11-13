"""TaskRunService：任务「开始执行」的编排收口（P2-1 §6.2）。

把原先散在 `api/v1/tasks.py` 里的启动编排（确认卡合并 → 计划/指令拼装 → 预登记 run_id →
起 Run → 广播 run_started）抽到这里，按 `agent.config["runtime"]` 分发到对应
`AgentRuntime`。开始执行不再往对话里落消息（执行过程由流事件呈现）：所有 runtime
（Claude Code / Codex / OpenCode）都走 CLI 路径，返回同形 Run 快照。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from flux.core.agent_runtime.runtimes.base import (
    CLI_RUNTIMES,
    DEFAULT_RUNTIME,
    AgentRuntime,
)
from flux.core.agent_runtime.runtimes.cli import build_cli_runtime
from flux.core.context.budget import history_message_limit
from flux.core.permissions.modes import normalize_mode
from flux.core.task_engine.assistant import validate_confirmation_items
from flux.core.task_engine.conversation_trace import (
    TRACE_BUDGET_CHARS,
    TRACE_MAX_MESSAGES,
    TRACE_RESUMED_BUDGET_CHARS,
    TRACE_RESUMED_MAX_MESSAGES,
    build_dialogue_trace,
)
from flux.enums import AgentInstallStatus, MemoryLayer, TaskMessageRole, TaskStatus
from flux.errors import AIOSError, ConflictError, NotFoundError
from flux.logging import get_logger
from flux.models.task import Task

if TYPE_CHECKING:
    from flux.container import Container

logger = get_logger(__name__)

#: 一次对话请求带上的历史条数上限（与 tasks.py 同口径，由 §6.1 的字符预算折算）
_HISTORY_LIMIT = history_message_limit()


@dataclass(slots=True)
class RunStartResult:
    """一次「开始执行」的结果（API 响应三件套）。

    `message` 恒为 None：开始执行不再往对话里落「已开始执行」气泡，
    执行过程改由 `run_started` / `run_event` 流事件呈现。
    """

    task: Task
    run: dict[str, Any]
    message: Any = None


class TaskRunService:
    """按 runtime 分发任务启动；run 运行时对象的获取延迟到调用时（测试可替换容器组件）。"""

    def __init__(self, container: Container, *, cli_adapters: Any = ()) -> None:
        self._container = container
        self._cli_adapters = tuple(cli_adapters)

    async def start(
        self,
        task: Task,
        confirmation_request: Any = None,
        *,
        followup: str | None = None,
    ) -> RunStartResult:
        container = self._container
        task_uuid = task.id

        confirmation: dict[str, Any] | None = task.confirmation
        if confirmation_request is not None:
            items = validate_confirmation_items(
                [item.model_dump() for item in confirmation_request]
            )
            confirmation = {"items": items, "actor": "user", "updated_at": _now_iso()}
            task = await container.task_repo.set_confirmation(task_uuid, confirmation)

        agent = self._agent(task)
        runtime_id = _runtime_of_task(task, agent)
        await self._require_ready(runtime_id)
        # 会话是否已在该 Agent + runtime 上存在：决定要不要省掉「原任务背景」（会话里本来就有）
        resumed = await _has_session(container, task_uuid, runtime_id, task.agent_id)

        # 续跑：新消息是这一轮的唯一指令，不叠加上一轮的确认卡与计划（那是历史上下文，
        # 由 Agent 自己的会话/项目 Brain 承接）。
        steps = [] if followup else await _latest_plan(container, task_uuid)
        summary = await _latest_summary(container, task_uuid)
        attachments = await _latest_attachments(container, task_uuid)
        # 跨轮记忆：最近对话轨迹 + 上一轮工作记忆卡，两者都进本轮指令
        messages, _ = await container.task_repo.list_messages(task_uuid, limit=_HISTORY_LIMIT)
        memory = await _latest_memory(container, task_uuid)
        user_memory = await _user_memory_instruction(
            container, getattr(task, "cloud_user_id", None)
        )
        bot_memory = await _bot_memory_instruction(container, task)
        instruction = _execution_instruction(
            task,
            None if followup else confirmation,
            steps,
            summary=summary,
            followup=followup,
            attachments=attachments,
            mode=normalize_mode(getattr(task, "permission_mode", None)),
            messages=messages,
            memory=memory,
            user_memory=user_memory,
            bot_memory=bot_memory,
            resumed=resumed,
        )
        # 额度闸门：默认只告警（FLUX_BILLING_ENFORCE=false）；开启拦截且已超额时在这里拒单。
        # 此刻还没预登记 run_id，抛错不需要回滚任务状态。
        billing = getattr(container, "billing", None)
        if billing is not None:
            await billing.ensure_can_start(
                model=str(getattr(task, "model", "") or ""), runtime_id=runtime_id
            )
        runtime = self._runtime(runtime_id)

        # 先登记 run_id 与 running，再真正起 Run：倒过来的话，Run 结束事件可能先于登记到达，
        # 任务就永远收不到自己的终态（P0-05）。
        run_id = uuid.uuid4().hex
        previous_status = task.status
        task = await container.task_repo.set_run_id(task_uuid, run_id)
        task = await container.task_repo.set_status(task_uuid, TaskStatus.RUNNING)
        # 先建立 run → 对话 的映射：CLI runtime 可能在 start() 返回前就开始吐事件
        container.task_stream.bind_run(run_id, str(task_uuid))
        try:
            # 任务级模型通道（密钥库）：没有 key 覆盖时为 None，runtime 用原生配置
            channel = await container.key_vault.channel_for_task(task)
            launch = await runtime.prepare(
                task=task, agent=agent, run_id=run_id, instruction=instruction, channel=channel
            )
            proc = await runtime.start(launch)
        except AIOSError:
            # 起 Run 失败（未启用 / 令牌签发失败 / patch 写不出 / 装配不齐）时撤回预登记：
            # 否则任务卡在"看起来在跑"，用户既看不到 Agent 也再也点不动「开始执行」。
            container.task_stream.unbind_run(run_id)
            await container.task_repo.set_run_id(task_uuid, None)
            task = await container.task_repo.set_status(task_uuid, TaskStatus(previous_status))
            raise

        await container.task_stream.publish(
            task_uuid,
            "run_started",
            {"run_id": run_id, "runtime": runtime_id},
        )
        return RunStartResult(task=task, run=proc.snapshot)

    async def preview_instruction(self, task: Task) -> str:
        """算出「下一轮开工会收到的执行指令」原文（上下文占用按它统计）。

        与 `start()` 共用同一套装配：约束 / 任务 / 摘要 / 工作记忆 / 对话轨迹 /
        附件 / 确认 / 计划。取数失败时返回空串，接口层只少算字符，不因此 500。
        """
        container = self._container
        try:
            steps = await _latest_plan(container, task.id)
            summary = await _latest_summary(container, task.id)
            attachments = await _latest_attachments(container, task.id)
            messages, _ = await container.task_repo.list_messages(task.id, limit=_HISTORY_LIMIT)
            memory = await _latest_memory(container, task.id)
            user_memory = await _user_memory_instruction(
                container, getattr(task, "cloud_user_id", None)
            )
            bot_memory = await _bot_memory_instruction(container, task)
            runtime_id = _runtime_of_task(task, self._agent(task))
            resumed = await _has_session(container, task.id, runtime_id, task.agent_id)
            return _execution_instruction(
                task,
                task.confirmation,
                steps,
                summary=summary,
                followup=None,
                attachments=attachments,
                mode=normalize_mode(getattr(task, "permission_mode", None)),
                messages=messages,
                memory=memory,
                user_memory=user_memory,
                bot_memory=bot_memory,
                resumed=resumed,
            )
        except Exception:  # noqa: BLE001 - 预览失败不影响对话本身
            logger.warning("预演执行指令失败 task=%s", task.id, exc_info=True)
            return ""

    # --- runtime 解析 ---

    def _agent(self, task: Task) -> Any:
        if not task.agent_id:
            return None
        try:
            return self._container.agents.get(task.agent_id)
        except NotFoundError:
            logger.warning(
                "任务 %s 的 Agent %s 不在注册表中，按默认 runtime 处理", task.id, task.agent_id
            )
            return None

    def _runtime(self, runtime_id: str) -> AgentRuntime:
        container = self._container
        return build_cli_runtime(
            runtime_id=runtime_id,
            adapters=self._cli_adapters,
            settings=container.settings,
            supervisor=container.run_supervisor,
            bus=container.bus,
            token_service=container.agent_tokens,
            container=container,
        )

    async def _require_ready(self, runtime_id: str) -> None:
        """CLI runtime 启动前必须已接入（installation = READY），否则 409 并给出上线指引。"""
        if runtime_id not in CLI_RUNTIMES:
            return
        row = await self._container.installations.get(runtime_id)
        status = row.status if row is not None else None
        if status != AgentInstallStatus.READY.value:
            raise ConflictError(
                f"{runtime_id} 尚未接入 Flux（当前状态：{status or '未发现'}），"
                f"请先运行 `flux agents connect {runtime_id}`",
                details={"runtime": runtime_id, "installation_status": status},
            )


def _runtime_of(agent: Any) -> str:
    """从 Agent 档案取 runtime（默认 claude-code）。"""
    if agent is None:
        return DEFAULT_RUNTIME
    spec = getattr(agent, "spec", None)
    runtime = getattr(spec, "runtime", None) or DEFAULT_RUNTIME
    return str(runtime)


def _runtime_of_task(task: Task, agent: Any) -> str:
    """Bot 触发的一律跑 Claude Code 底座（新老 Bot 同口径），其余按 Agent 档案。"""
    if getattr(task, "bot_id", None) is not None:
        return DEFAULT_RUNTIME
    return _runtime_of(agent)


async def _latest_plan(container: Container, task_uuid: uuid.UUID) -> list[str]:
    """取最近一轮助手给出的执行计划（开始执行时一并交给 Agent）。"""
    messages, _ = await container.task_repo.list_messages(task_uuid, limit=_HISTORY_LIMIT)
    for message in reversed(messages):
        steps = (message.payload or {}).get("steps")
        if message.role == TaskMessageRole.ASSISTANT.value and isinstance(steps, list) and steps:
            return [str(step) for step in steps]
    return []


async def _latest_summary(container: Container, task_uuid: uuid.UUID) -> str:
    """取这段对话最近的「前情摘要」（来自上一段对话）；没有则返回空串。"""
    message = await container.task_repo.latest_message(task_uuid, kind="summary")
    return message.content.strip() if message is not None else ""


async def _latest_attachments(container: Container, task_uuid: uuid.UUID) -> list[dict[str, Any]]:
    """取最近一次上传的附件清单（手机端「+」传文件）。

    只带最近一批：更早的附件在当轮的指令里已经给过，Agent 会话也还记得，
    每轮都重列会让长对话的指令无限膨胀。
    """
    message = await container.task_repo.latest_message(task_uuid, kind="attachments")
    files = (message.payload or {}).get("files") if message is not None else None
    return [item for item in files if isinstance(item, dict)] if isinstance(files, list) else []


async def _has_session(
    container: Container,
    task_uuid: uuid.UUID,
    runtime_id: str,
    agent_id: uuid.UUID | None,
) -> bool:
    """该任务是否已在该 Agent + runtime 上存有可复用的会话；读失败按「没有」处理。"""
    repo = getattr(container, "runtime_sessions", None)
    if repo is None:
        return False
    try:
        return bool(await repo.get(task_uuid, runtime_id, agent_id))
    except Exception:  # noqa: BLE001 - 会话表不可用时退回每轮新建
        logger.warning(
            "读取任务会话绑定失败 task=%s agent=%s runtime=%s",
            task_uuid,
            agent_id,
            runtime_id,
            exc_info=True,
        )
        return False


async def _latest_memory(container: Container, task_uuid: uuid.UUID) -> str:
    """取最近一张工作记忆卡；没有或表不存在时返回空串。"""
    repo = getattr(container, "task_memory", None)
    if repo is None:
        return ""
    try:
        card = await repo.latest(task_uuid)
    except Exception:  # noqa: BLE001 - 记忆卡读取失败不能挡住任务启动
        logger.warning("读取工作记忆卡失败 task=%s", task_uuid, exc_info=True)
        return ""
    return card.content.strip() if card is not None else ""


#: 执行指令里最多注入多少条用户长期记忆 / 多少字符（防止画像越滚越大挤占任务）
_USER_MEMORY_MAX_ENTRIES = 8
_USER_MEMORY_MAX_CHARS = 1200


#: 执行指令里提醒 Agent 顺手记录用户偏好（开关在手机端设置里，关掉后不出现）
_REMEMBER_HINT = (
    "## 记录用户长期记忆\n"
    "如果本轮对话里用户明确表达了稳定的偏好、习惯或禁忌（换项目、换设备仍然成立），"
    "调用 memory.remember 记下来：每轮最多 3 条，每条不超过 60 字。"
    "不要记项目细节、文件路径、一次性任务和密钥；已经记过的不要重复写。"
)


async def _user_memory_instruction(container: Container, cloud_user_id: str | None = None) -> str:
    """把当前人格的三层记忆压成背景（User 最近 8 条 / 1200 字 + 共享空间）。

    `cloud_user_id` 来自任务行（例程创建者 / 绑定的 IM 用户）；None 时沿用
    MemoryService 的主用户回退口径，兼容历史任务。
    """
    blocks: list[str] = []
    prefs = getattr(container, "memory_prefs", None)
    if prefs is None or prefs.auto_capture():
        blocks.append(_REMEMBER_HINT)
    memory = getattr(container, "memory", None)
    if memory is None:
        return "\n\n".join(blocks)
    try:
        entries = await memory.list_entries(layer=MemoryLayer.USER, cloud_user_id=cloud_user_id)
    except Exception:  # noqa: BLE001 - 记忆读取失败不能挡住任务启动
        logger.warning("读取用户长期记忆失败", exc_info=True)
        entries = []
    try:
        shared = await memory.render_shared_block(cloud_user_id)
    except Exception:  # noqa: BLE001 - 共享空间失败不能挡住任务启动
        logger.warning("读取共享空间记忆失败", exc_info=True)
        shared = ""
    if shared:
        blocks.append(shared.rstrip())
    if entries:
        picked = entries[-_USER_MEMORY_MAX_ENTRIES:]
        lines: list[str] = []
        used = 0
        for entry in reversed(picked):
            text = " ".join(entry.content.split())
            if not text:
                continue
            if used + len(text) > _USER_MEMORY_MAX_CHARS:
                break
            lines.append(f"- {text}")
            used += len(text)
        if lines:
            lines.reverse()
            blocks.append(
                "\n".join(["## 用户长期记忆（账号级，跨设备；仅作背景，不要逐条复述）", *lines])
            )
    return "\n\n".join(blocks)


async def _bot_memory_instruction(container: Container, task: Any) -> str:
    """Bot 自己的长期记忆（第一人称）：只在任务来自某个 Bot 时注入。

    `group_id=None` 表示当前不在任何群里：direct 全量、出群后的 indirect 只按
    任务描述命中关键词低权重召回。记忆只进执行指令，不写进 `task.description`。
    """
    bot_id = getattr(task, "bot_id", None)
    if not bot_id:
        return ""
    service = getattr(container, "bot_memories", None)
    bots = getattr(container, "bots", None)
    if service is None or bots is None:
        return ""
    try:
        bot = await bots.get(bot_id)
        return await service.render_block(
            bot, group_id=None, query=str(getattr(task, "description", "") or "")
        )
    except Exception:  # noqa: BLE001 - 记忆读取失败不能挡住任务启动
        logger.warning("读取 Bot 记忆失败 task=%s", task.id, exc_info=True)
        return ""


def _mode_constraints(mode: str) -> list[str]:
    """三种权限模式的执行约束（与 protocol.compose_instruction 的模式段保持一致）。"""
    if mode == "plan":
        return [
            "- 当前是 Plan 模式（只读）：只读项目上下文与设计稿（design.list / design.get / "
            "design.revisions），不要执行 shell、不要改代码、不要提交提案、"
            "不要 design.create/update/ai/revert/share。",
            "- 用 plan.write 输出实施计划（标题 + 1–12 个步骤 + 涉及文件），计划文档由服务端"
            "写入固定目录；计划卡会自动出现在对话里。",
            "- 交付的是可执行、可验收的计划，不是“已完成的改动”。",
        ]
    if mode == "unattended":
        return [
            "- 当前是免审核（无人值守）模式：高风险命令与受保护对象都会自动放行，"
            "没有人会在这轮执行中拦下你。",
            "- 越是没有把关，越要保守：不要执行 rm -rf、格式化、重启主机这类破坏性操作；"
            "不要读取或复制密钥内容，除非任务明确要求；不要凭空猜测后直接改生产数据。",
            "- 设计任务仍必须用 design.* 工具产出，完成后 design.share 发进对话。",
            "- 结束时如实汇报做了哪些有风险的操作、改了哪些文件、还有哪些没验证。",
        ]
    if mode == "full":
        return [
            "- 当前是完全访问模式：可以直接修改工作区、执行命令、提交 git，不必逐步提交提案审批。",
            "- 受保护对象（.env / *.pem / SSH 私钥等敏感文件，systemctl / service / reboot 等"
            "系统服务操作）仍会逐次请求用户确认；先自问两次是否真的需要，再说明理由申请，"
            "不要假设上一次允许继续有效，也不要尝试绕过闸门。",
            "- 设计任务仍必须用 design.* 工具产出，完成后 design.share 发进对话。",
            "- 结束时如实汇报改了哪些文件、跑了什么验证、还有哪些风险。",
        ]
    return [
        "- 只能通过 Flux MCP 读取项目上下文并把改动提交为提案；"
        "不要尝试直接写文件、执行 shell 或读取密钥。",
        "- 改动一律进入人工审核队列，落盘由人审通过后经 Apply Engine 执行，并在落盘时跑项目测试。",
        "- proposal.preview 只是预演，不产生提案、也不会进人审队列；"
        "预演通过后必须再调用 proposal.create 提交同一份 payload，才算交卷。",
        "- 高风险命令（删除、git commit/push、安装依赖、联网、改系统服务）会先请求用户允许；"
        "低风险只读命令自动执行，不要试图绕过审批。",
    ]


def _memory_quoted_followup(messages: list[Any], followup: str) -> str:
    """工作记忆卡「本轮目标」引用的那条用户消息：上一轮的后续消息原文。

    `followup` 非空说明最新一条用户消息就是本轮消息，往回再取一条才是记忆卡引用的那句。
    """
    if not followup:
        return ""
    skipped_current = False
    for message in reversed(messages):
        if str(getattr(message, "role", "") or "") != "user":
            continue
        text = str(getattr(message, "content", "") or "").strip()
        if not text:
            continue
        if not skipped_current:
            skipped_current = True
            continue
        return text
    return ""


def _execution_instruction(
    task: Task,
    confirmation: dict[str, Any] | None,
    steps: list[str],
    *,
    summary: str = "",
    followup: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    mode: str = "normal",
    messages: list[Any] | None = None,
    memory: str = "",
    user_memory: str = "",
    bot_memory: str = "",
    resumed: bool = False,
) -> str:
    """拼出交给 Agent 的执行指令。

    稳定段（执行约束）放在最前、动态段（任务 / 确认 / 计划 / 身份）放在后面：
    CLI 会话会把整个 prompt 当作前缀缓存，越早出现的内容越容易在后续 Run 里复用，
    把随任务变化的 task_id 和描述放在前面等于每轮都从第一个 token 开始失效。

    同一份信息只出现一次：前情摘要 / 附件 / 需求确认已单独成段时，对话轨迹里
    就不再重复这些消息；续聊（`resumed`，Runtime 已复用会话）时轨迹也收缩到
    最近几行——历史本来就在会话里，这里只为「复用失败退回新会话」留安全带。
    """
    background = bool(followup) and not resumed
    items = (confirmation or {}).get("items")
    has_items = isinstance(items, list) and bool(items)

    excluded_kinds: list[str] = []
    if summary:
        excluded_kinds.append("summary")
    if attachments:
        excluded_kinds.append("attachments")
    if has_items:
        excluded_kinds.append("confirmation")
    if memory:
        # 工作记忆卡已经复述了上一轮做了什么，轨迹里的结果落款不再重复
        excluded_kinds.append("run_finished")
    excluded_texts = [followup or ""]
    if memory:
        # 记忆卡的「本轮目标」就是上一轮的后续消息原文，轨迹里不再重复同一句
        excluded_texts.append(_memory_quoted_followup(messages or [], followup or ""))
    if background:
        # 原任务背景下面已经完整给出，轨迹里不再重复同一段描述
        excluded_texts.append(task.description)
    trace = build_dialogue_trace(
        messages or [],
        budget_chars=TRACE_RESUMED_BUDGET_CHARS if resumed else TRACE_BUDGET_CHARS,
        max_messages=TRACE_RESUMED_MAX_MESSAGES if resumed else TRACE_MAX_MESSAGES,
        exclude_texts=tuple(text for text in excluded_texts if text),
        exclude_kinds=tuple(excluded_kinds),
    )

    constraints = ["## 执行约束", *_mode_constraints(mode), ""]
    if followup:
        # 续跑轮：这条新消息才是这一轮要执行的事。原任务只作背景并显式标注「不要重做」，
        # 否则 Agent 会把最初的需求当成当前任务再执行一遍（真机反馈：每轮都重做最初的设计）。
        lines = [
            *constraints,
            "## 本轮任务（同一对话的后续消息，只执行这一条）",
            followup.strip(),
        ]
        if background:
            # 会话已复用（opencode/codex 续聊）时，最初的需求就在会话历史里，不再重复；
            # 没复用（新建会话）时仍要给背景，并明确「不要重做」。
            lines += [
                "",
                "## 原任务背景（对话最初的目标，此前已处理；"
                "除非本轮任务明确要求返工，不要重新执行）",
                task.description.strip(),
            ]
    else:
        lines = [*constraints, task.description.strip()]
    if summary:
        # 从上一段对话接手：新对话不拷贝旧消息，只带这份要点，保持上下文轻量。
        lines += [
            "",
            "## 前情摘要（来自上一段对话，供你接着做）",
            summary.strip(),
        ]
    if memory:
        # 上一轮结束由平台生成的事实卡：改过哪些文件、结果如何、为什么失败
        lines += ["", memory.strip()]
    if bot_memory:
        # Bot 自己的长期记忆：跟随 Bot 跨群、跨开工，按与本次任务的相关性排序
        lines += ["", bot_memory.strip()]
    if user_memory:
        # 账号级长期记忆：稳定的个人偏好/禁忌，跨设备跨项目都适用
        lines += ["", user_memory.strip()]
    if trace:
        # 最近几轮的真实对话：用户改了什么要求、助手承诺过什么，避免只记住最初任务
        lines += ["", trace.strip()]
    if attachments:
        # 用户从手机附上的文件：路径是服务端真实落盘位置，直接用原生读文件/终端看
        lines += ["", "## 附件（用户随本轮消息上传，可直接按路径读取）"]
        lines += [f"- {item.get('path')}（{item.get('name')}）" for item in attachments]
    if has_items:
        lines += ["", "## 需求确认（用户已确认，以此为准）"]
        lines += [f"- {item['label']}：{item['value']}" for item in items]
    if steps:
        lines += ["", "## 执行计划"]
        lines += [f"{index + 1}. {step}" for index, step in enumerate(steps)]
    lines += [
        "",
        "## 平台身份（调用 Flux MCP 时带上）",
        f"- task_id：{task.id}",
        f"- project_id：{task.project_id or '未关联'}",
        "- 开工先调 `context.boot` 并带上 task_id：一次拿回运行时身份/能力、项目 Brain"
        "（项目结构、技术栈、入口文件）与近期提案、以及跨会话长期记忆；不确定文件路径时"
        "以它为准，再用 workspace.read 核对内容。需要刷新某一份时再单独调 flux_context / "
        "context.get / memory.recall。",
        "- 探索优先用 workspace.tree 看地图、workspace.search 定位、workspace.read_many "
        "一次读多个文件；确定行号后再用 workspace.read 精读，减少往返。",
    ]
    return "\n".join(lines)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ["RunStartResult", "TaskRunService"]
