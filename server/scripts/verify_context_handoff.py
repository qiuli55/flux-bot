"""生产环境上下文占用 / 前情摘要 / 带摘要建任务 端到端自检。

用法：python scripts/verify_context_handoff.py
读取 /opt/flux/server/flux-server.env 里的令牌与本机 8801 端口，不打印任何令牌。

自检会新建一个临时任务，直接向库里写入若干轮对话把上下文推到 full，
验证 /context、/handoff、带摘要建任务三个入口后删除该任务，不触碰既有数据。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from flux.core.task_engine.repository import MessageDraft, TaskRepository
from flux.db.session import create_engine, create_session_factory

BASE = os.environ.get("FLUX_VERIFY_BASE", "http://127.0.0.1:8801")
ENV_FILE = Path("/opt/flux/server/flux-server.env")
DB_URL = "sqlite+aiosqlite:////opt/flux/server/flux.db"


def load_token() -> str:
    token = os.environ.get("FLUX_AUTH_TOKEN")
    if token:
        return token
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("FLUX_AUTH_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("找不到 FLUX_AUTH_TOKEN")


TOKEN = load_token()
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


SEED_TURNS = [
    (
        "我想把手机端的对话上下文做成可轮换的",
        "可以，先确认口径：以平台助手每轮实际能带上的历史为准。",
    ),
    ("对，就按实际能带上的算，不要按模型窗口", "明白，阈值定在 80%，满了再提醒一次。"),
    ("提醒出现后我能选继续聊或者新建", "继续聊就按当前等级不再打扰，新建会把摘要带过去。"),
    ("摘要要写进新对话里，旧对话别动", "旧对话原样保留，新对话只带一份摘要，不拷贝消息。"),
    ("新对话什么时候真正落成任务", "等你在新对话里发出第一条消息时再创建，避免孤儿任务。"),
    ("摘要也要给 Agent 看到", "会写进执行指令里，Agent 接手时能看到前情。"),
    ("手机端和桌面端都要有这些能力", "两端都接同一套接口，读数与提醒保持一致。"),
    ("占用的进度条要一直显示在对话页", "常驻细进度条，warning 琥珀色、full 玫红色。"),
    ("新建任务要清掉上一段的用量读数", "新建即全新上下文，读数、摘要暂存一起清空。"),
    ("验证完把自检任务删掉", "自检任务删除后，任务、消息、运行记录一并清除。"),
]


async def seed_messages(task_id: str) -> None:
    engine = create_engine(DB_URL)
    session_factory = create_session_factory(engine)
    repo = TaskRepository(session_factory)
    import uuid

    task_uuid = uuid.UUID(task_id)
    drafts = []
    for user_text, reply_text in SEED_TURNS:
        drafts.append(MessageDraft(role="user", kind="text", content=user_text))
        drafts.append(MessageDraft(role="assistant", kind="text", content=reply_text))
    await repo.add_messages(task_uuid, drafts)
    await engine.dispose()


def main() -> int:
    code, created = call("POST", "/api/v1/tasks", {"description": "上下文轮换自检（临时任务）"})
    assert code in (200, 201), (code, created)
    task_id = created["data"]["id"]
    print(f"自检任务 {task_id[:8]} 已创建")

    try:
        asyncio.run(seed_messages(task_id))
        print(f"已写入 {len(SEED_TURNS) * 2} 条对话")

        code, ctx = call("GET", f"/api/v1/tasks/{task_id}/context")
        assert code == 200, (code, ctx)
        usage = ctx["data"]
        print(
            f"上下文 {usage['used_messages']}/{usage['limit']} 条 · "
            f"{usage['used_chars']}/{usage['budget_chars']} 字 · "
            f"{usage['percent']}% · {usage['level']}"
        )
        assert usage["message_count"] == len(SEED_TURNS) * 2, usage
        assert usage["used_messages"] == min(usage["message_count"], usage["limit"]), usage
        assert usage["level"] == "full", usage

        code, res = call("POST", f"/api/v1/tasks/{task_id}/handoff")
        assert code == 200, (code, res)
        summary = res["data"]
        print(
            f"摘要 {len(summary['summary'])} 字 · 来源 {summary['source_messages']} 条 · "
            f"{summary['model']}"
        )
        print("摘要首行：" + summary["summary"].splitlines()[0][:80])

        code, created_new = call(
            "POST",
            "/api/v1/tasks",
            {
                "description": "验证带摘要建任务（自检用）",
                "summary": summary["summary"],
                "summary_from": task_id,
            },
        )
        assert code in (200, 201), (code, created_new)
        new_id = created_new["data"]["id"]
        try:
            code, msgs = call("GET", f"/api/v1/tasks/{new_id}/messages?limit=20")
            assert code == 200, (code, msgs)
            kinds = [m["kind"] for m in msgs["data"]]
            assert kinds == ["summary"], kinds
            print(
                f"新任务 {new_id[:8]} 只有一条 kind=summary 消息，"
                f"正文 {len(msgs['data'][0]['content'])} 字"
            )

            code, new_ctx = call("GET", f"/api/v1/tasks/{new_id}/context")
            assert code == 200 and new_ctx["data"]["used_messages"] == 1, new_ctx
            print(f"新任务上下文 {new_ctx['data']['percent']}% · {new_ctx['data']['level']}")
        finally:
            code, _ = call("DELETE", f"/api/v1/tasks/{new_id}")
            assert code == 200, code
            print("带摘要的新任务已清理")
    finally:
        code, _ = call("DELETE", f"/api/v1/tasks/{task_id}")
        print("自检任务已清理" if code == 200 else f"清理失败：{code}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
