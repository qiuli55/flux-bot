"""IDE 行内补全（AI_COMPLETION_SPEC §3 / §4 / §5）的单元与接口测试。"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from flux.core.ide_completion.context import L0_AFTER_LINES, L0_BEFORE_LINES
from flux.core.ide_completion.fim import (
    CHAT_MIN_MAX_TOKENS,
    CompletionService,
    _LeadingDeduper,
    _parse_fim_line,
    _StreamFormatter,
)
from flux.core.ide_completion.prefs import (
    DEFAULT_DEBOUNCE_MS,
    DEFAULT_MAX_LINES,
    FOLLOW_AGENT,
    ActiveAgentSelection,
    CompletionPrefs,
    CompletionPrefsStore,
)
from flux.core.ide_completion.stats import MAX_DAYS, CompletionStatsStore
from flux.schemas.api import CompletionContextRequest, FimRequest
from tests.conftest import create_agent

# --- 流式格式化：去围栏 / 截断 / 保留缩进 ---


def test_stream_formatter_strips_code_fence_keeps_indent() -> None:
    formatter = _StreamFormatter(max_lines=8)
    pieces = [formatter.feed("```python\ndef a():\n    return 1\n```"), formatter.finish()]

    assert "".join(pieces) == "def a():\n    return 1"
    assert formatter.truncated is False


def test_stream_formatter_truncates_at_max_lines() -> None:
    formatter = _StreamFormatter(max_lines=2)
    pieces = [formatter.feed("a\nb\nc\n"), formatter.finish()]

    assert "".join(pieces) == "a\nb"
    assert formatter.truncated is True


def test_stream_formatter_unchanged_when_within_budget() -> None:
    formatter = _StreamFormatter(max_lines=8)
    pieces = [formatter.feed("x = 1\ny = 2\n"), formatter.feed("z = 3"), formatter.finish()]

    assert "".join(pieces) == "x = 1\ny = 2\nz = 3"
    assert formatter.truncated is False


# --- 前缀回显去重（模型复读光标前内容时剥掉） ---


def test_leading_deduper_strips_strong_echo() -> None:
    deduper = _LeadingDeduper("def add(a, b):\n    return ")

    out = deduper.feed("def add(a, b):\n    return a + b")

    assert out == "a + b"


def test_leading_deduper_keeps_normal_continuation() -> None:
    deduper = _LeadingDeduper("x = ")

    assert deduper.feed("1\n") == "1\n"
    assert deduper.finish() == ""


def test_leading_deduper_defers_short_echo_until_next_chunk() -> None:
    deduper = _LeadingDeduper("    return ")

    head = deduper.feed("return ")
    tail = deduper.feed("value\n")

    assert head == ""
    assert tail == "value\n"


def test_leading_deduper_finish_flushes_pending_echo() -> None:
    deduper = _LeadingDeduper("    return ")

    assert deduper.feed("return ") == ""
    assert deduper.finish() == ""


def test_leading_deduper_does_not_strip_identifier_continuation() -> None:
    # `foo` 是正常续写（重叠只有 3 个字符），不能当成回显剥掉
    deduper = _LeadingDeduper("foo")

    assert deduper.feed("foobar") == "foobar"


# --- 原生 FIM 行解析 ---


def test_parse_fim_line_reads_text_choice() -> None:
    line = 'data: {"choices": [{"text": "print(1)"}]}'

    assert _parse_fim_line(line) == ("print(1)", None)


def test_parse_fim_line_reads_delta_content() -> None:
    line = 'data: {"choices": [{"delta": {"content": "hi"}}]}'

    assert _parse_fim_line(line) == ("hi", None)


def test_parse_fim_line_maps_usage_tokens() -> None:
    line = (
        'data: {"choices": [{"text": "ok"}], '
        '"usage": {"prompt_tokens": 12, "completion_tokens": 7}}'
    )

    text, usage = _parse_fim_line(line) or ("", None)
    assert text == "ok"
    assert usage == {"input_tokens": 12, "output_tokens": 7}


@pytest.mark.parametrize(
    "line",
    [
        "data: [DONE]",
        "event: done",
        "",
        "data: not-json",
    ],
)
def test_parse_fim_line_ignores_non_payload_lines(line: str) -> None:
    assert _parse_fim_line(line) is None


# --- 偏好落盘 ---


def test_prefs_store_defaults_when_file_missing(tmp_path: Path) -> None:
    store = CompletionPrefsStore(tmp_path / "completion-prefs.json")

    prefs = store.prefs()
    assert prefs == CompletionPrefs()
    assert prefs.enabled is True
    assert prefs.model == FOLLOW_AGENT
    assert prefs.debounce_ms == DEFAULT_DEBOUNCE_MS
    assert prefs.max_lines == DEFAULT_MAX_LINES
    assert store.active_agent().is_empty


def test_prefs_store_partial_update_keeps_other_fields(tmp_path: Path) -> None:
    store = CompletionPrefsStore(tmp_path / "completion-prefs.json")
    store.update_prefs(model="deepseek-chat", max_lines=16)

    updated = store.update_prefs(enabled=False)

    assert updated.enabled is False
    assert updated.model == "deepseek-chat"
    assert updated.max_lines == 16
    assert store.prefs() == updated


def test_prefs_store_clamps_out_of_range_values(tmp_path: Path) -> None:
    store = CompletionPrefsStore(tmp_path / "completion-prefs.json")
    store.update_prefs(debounce_ms=5, max_lines=999)

    prefs = store.prefs()
    assert prefs.debounce_ms == 100
    assert prefs.max_lines == 64


def test_prefs_store_survives_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "completion-prefs.json"
    path.write_text("{ not json", encoding="utf-8")
    store = CompletionPrefsStore(path)

    assert store.prefs() == CompletionPrefs()
    assert store.active_agent().is_empty


def test_prefs_store_writes_atomically_and_keeps_both_sections(tmp_path: Path) -> None:
    path = tmp_path / "completion-prefs.json"
    store = CompletionPrefsStore(path)
    store.set_active_agent(ActiveAgentSelection(agent_id="codex", model="deepseek-chat"))

    snapshot = store.snapshot()

    assert snapshot["active_agent"] == {
        "agent_id": "codex",
        "model": "deepseek-chat",
        "key_id": None,
    }
    assert snapshot["model"] == FOLLOW_AGENT
    # 临时文件必须已被 os.replace 掉
    assert not path.with_name(f"{path.name}.tmp").exists()
    assert json.loads(path.read_text(encoding="utf-8"))["active_agent"]["agent_id"] == "codex"


# --- 埋点统计落盘 ---


def test_stats_store_counts_requests_results_and_accepts(tmp_path: Path) -> None:
    path = tmp_path / "completion-stats.json"
    store = CompletionStatsStore(path)

    store.record_request(model="stub-model")
    store.record_result(mode="chat", model="stub-model", first_token_ms=120, total_ms=480, chars=12)
    store.record_request(model="stub-model")
    store.record_result(
        mode="chat", model="stub-model", first_token_ms=None, total_ms=90, chars=0, empty=True
    )
    store.record_request(model="stub-model")
    store.record_failure("provider_error", model="stub-model")
    store.record_request(model="stub-model")
    store.record_abort(model="stub-model")
    store.record_accept(accepted=True, mode="chat", model="stub-model", language_id="python")

    snapshot = store.snapshot()
    totals = snapshot["totals"]
    assert totals["requests"] == 4
    assert totals["successes"] == 2
    assert totals["failures"] == 1
    assert totals["aborts"] == 1
    assert totals["empty"] == 1
    # 接受率分母是「真正展示过的非空补全」，不是请求数
    assert totals["delivered"] == 1
    assert totals["accepted"] == 1
    assert totals["acceptance_rate"] == 1.0
    # 首字延迟只统计拿到首字的样本
    assert totals["avg_first_token_ms"] == 120
    assert totals["avg_total_ms"] == 285

    assert snapshot["by_model"]["stub-model"]["requests"] == 4
    assert snapshot["by_mode"]["chat"]["successes"] == 2
    assert snapshot["days"][0]["date"] == date.today().isoformat()
    assert snapshot["days"][0]["requests"] == 4
    assert snapshot["last"] == {
        "at": snapshot["last"]["at"],
        "ok": False,
        # 失败发生在 `done` 之前，此刻还拿不到 mode
        "mode": None,
        "model": "stub-model",
        "code": "provider_error",
    }
    assert snapshot["last_accept"]["language_id"] == "python"

    # 换一个实例从磁盘重读，数据必须还在（进程重启后不归零）
    reloaded = CompletionStatsStore(path).snapshot()
    assert reloaded["totals"]["requests"] == 4
    assert reloaded["totals"]["accepted"] == 1


def test_stats_store_ignores_rejected_completions(tmp_path: Path) -> None:
    store = CompletionStatsStore(tmp_path / "completion-stats.json")

    store.record_accept(accepted=False, mode="chat")

    snapshot = store.snapshot()
    assert snapshot["totals"]["accepted"] == 0
    assert snapshot["last_accept"]["accepted"] is False


def test_stats_store_survives_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "completion-stats.json"
    path.write_text("{ not json", encoding="utf-8")
    store = CompletionStatsStore(path)

    assert store.snapshot()["totals"]["requests"] == 0

    store.record_request()
    assert store.snapshot()["totals"]["requests"] == 1


def test_stats_store_drops_days_beyond_retention(tmp_path: Path) -> None:
    path = tmp_path / "completion-stats.json"
    stale_days = {f"2020-01-{index:02d}": {} for index in range(1, 21)}
    path.write_text(json.dumps({"days": stale_days}), encoding="utf-8")
    store = CompletionStatsStore(path)

    store.record_request()

    snapshot = store.snapshot()
    assert len(snapshot["days"]) == MAX_DAYS
    assert all(bucket["date"] != "2020-01-01" for bucket in snapshot["days"])
    assert snapshot["days"][0]["date"] == date.today().isoformat()


# --- 三层上下文 ---


def _line_block(count: int) -> str:
    return "\n".join(f"line{index}" for index in range(count))


def test_context_l0_uses_text_window_around_caret(container) -> None:
    text = _line_block(300)
    caret = text.index("line200")
    payload = CompletionContextRequest(path="pkg/a.py", caret=caret, text=text)

    snapshot = asyncio.run(container.completion.build_context(payload))

    l0 = snapshot["l0"]
    assert l0["caret_line"] == 200
    assert l0["start_line"] == 200 - L0_BEFORE_LINES
    assert l0["end_line"] == 200 + L0_AFTER_LINES + 1
    assert l0["text"].splitlines()[0] == f"line{200 - L0_BEFORE_LINES}"
    assert l0["text"].splitlines()[-1] == f"line{200 + L0_AFTER_LINES}"
    # 没有工作区：L1 / L2 必须是空值而不是报错
    assert snapshot["l1"] == []
    assert snapshot["l2"] == {"spec": "", "stack": "", "recent": ""}
    assert snapshot["cache_key"]


def test_context_api_includes_neighbour_files_and_spec(
    apply_client: TestClient, workspace_root: Path
) -> None:
    pkg = workspace_root / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("def current():\n    return 1\n", encoding="utf-8")
    (pkg / "b.py").write_text(
        "import os\n\n\ndef helper(value):\n    return value\n", encoding="utf-8"
    )
    specs = workspace_root / ".flux" / "specs"
    specs.mkdir(parents=True)
    (specs / "api.md").write_text("# API\n所有接口必须返回 FastAPI 响应模型。", encoding="utf-8")

    response = apply_client.post(
        "/api/v1/ide/completion/context",
        json={
            "path": "pkg/a.py",
            "text": "def current():\n    return 1\n",
            "caret": 20,
            "language_id": "python",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    data = body["data"]
    assert data["l0"]["language_id"] == "python"
    assert [item["path"] for item in data["l1"]] == ["pkg/b.py"]
    assert "def helper" in data["l1"][0]["excerpt"]
    assert "FastAPI 响应模型" in data["l2"]["spec"]
    assert body["metadata"]["l1_files"] == 1
    assert body["metadata"]["cache_key"] == data["cache_key"]


def test_context_api_requires_path(client: TestClient) -> None:
    response = client.post("/api/v1/ide/completion/context", json={"path": ""})

    assert response.status_code == 422


def _context_request(client: TestClient, path: str, text: str) -> list[str]:
    response = client.post(
        "/api/v1/ide/completion/context",
        json={"path": path, "text": text, "caret": 0, "language_id": "python"},
    )

    assert response.status_code == 200, response.text
    return [item["path"] for item in response.json()["data"]["l1"]]


def test_context_l1_ranks_imported_and_mentioned_files_first(
    apply_client: TestClient, workspace_root: Path
) -> None:
    pkg = workspace_root / "pkg"
    pkg.mkdir()
    text = "import zeta_module\n\nomega = zeta_module.load()\n"
    (pkg / "main.py").write_text(text, encoding="utf-8")
    (pkg / "aaa_helper.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (pkg / "zeta_module.py").write_text("def load():\n    return {}\n", encoding="utf-8")
    (pkg / "omega.py").write_text("def omega_value():\n    return 1\n", encoding="utf-8")

    paths = _context_request(apply_client, "pkg/main.py", text)

    # 被 import 的文件排最前，其次才是正文里提到过的；纯粹同目录的陪跑文件垫底
    assert paths[0] == "pkg/zeta_module.py"
    assert paths.index("pkg/omega.py") < paths.index("pkg/aaa_helper.py")


def test_context_l1_prefers_recently_modified_files(
    apply_client: TestClient, workspace_root: Path
) -> None:
    pkg = workspace_root / "pkg"
    pkg.mkdir()
    text = "x = 1\n"
    (pkg / "main.py").write_text(text, encoding="utf-8")
    old = pkg / "aaa_old.py"
    old.write_text("def old():\n    return 1\n", encoding="utf-8")
    (pkg / "zzz_new.py").write_text("def new():\n    return 2\n", encoding="utf-8")
    stale = time.time() - 30 * 86_400
    os.utime(old, (stale, stale))

    paths = _context_request(apply_client, "pkg/main.py", text)

    assert paths[0] == "pkg/zzz_new.py"
    assert paths[1] == "pkg/aaa_old.py"


# --- 接口：偏好 / 当前 Agent ---


def test_completion_prefs_api_roundtrip(client: TestClient) -> None:
    response = client.get("/api/v1/ide/completion/prefs")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["enabled"] is True
    assert data["model"] == FOLLOW_AGENT
    assert data["debounce_ms"] == DEFAULT_DEBOUNCE_MS
    assert data["max_lines"] == DEFAULT_MAX_LINES
    assert data["active_agent"]["agent_id"] is None
    # 没配任何密钥：展示接口不能整体失败，只能把解析错误放进 resolved
    assert data["resolved"]["model"] is None
    assert data["resolved"]["code"] == "configuration_error"

    saved = client.put(
        "/api/v1/ide/completion/prefs",
        json={"enabled": False, "model": "deepseek-chat", "debounce_ms": 800, "max_lines": 12},
    )

    assert saved.status_code == 200, saved.text
    assert saved.json()["metadata"]["saved"] is True
    saved_data = saved.json()["data"]
    assert saved_data["enabled"] is False
    assert saved_data["model"] == "deepseek-chat"
    assert saved_data["debounce_ms"] == 800
    assert saved_data["max_lines"] == 12

    reloaded = client.get("/api/v1/ide/completion/prefs").json()["data"]
    assert reloaded["enabled"] is False
    assert reloaded["model"] == "deepseek-chat"


def test_completion_prefs_api_partial_update_keeps_model(client: TestClient) -> None:
    client.put(
        "/api/v1/ide/completion/prefs",
        json={"model": "deepseek-chat", "max_lines": 12},
    )

    saved = client.put("/api/v1/ide/completion/prefs", json={"enabled": False})

    assert saved.status_code == 200, saved.text
    data = saved.json()["data"]
    assert data["enabled"] is False
    # IDE 只切开关时，不能把桌面端 / Web 端已经选好的模型与行数重置回默认
    assert data["model"] == "deepseek-chat"
    assert data["max_lines"] == 12


def test_completion_prefs_api_rejects_out_of_range(client: TestClient) -> None:
    response = client.put(
        "/api/v1/ide/completion/prefs",
        json={"enabled": True, "model": "follow-agent", "debounce_ms": 50, "max_lines": 8},
    )

    assert response.status_code == 422


def test_active_agent_api_saves_and_clears(client: TestClient) -> None:
    agent_id = create_agent(client, name="completion-agent")

    saved = client.put("/api/v1/agents/active", json={"agent_id": agent_id})

    assert saved.status_code == 200, saved.text
    data = saved.json()["data"]
    assert data["active_agent"]["agent_id"] == agent_id
    # describe_active 不返回补全偏好字段
    assert "debounce_ms" not in data
    assert data["active_agent"]["resolved"]["id"] == agent_id

    client.put("/api/v1/agents/active", json={})
    cleared = client.get("/api/v1/agents/active").json()["data"]
    assert cleared["active_agent"]["agent_id"] is None


def test_active_agent_api_rejects_unknown_agent(client: TestClient) -> None:
    response = client.put(
        "/api/v1/agents/active", json={"agent_id": "00000000-0000-0000-0000-000000000000"}
    )

    assert response.status_code == 404
    assert response.json()["success"] is False


# --- 接口：FIM SSE ---


def _stub_completion(client: TestClient) -> SimpleNamespace:
    """把容器里的补全门面替换成离线桩：不发网络请求，直接吐事件。"""

    container = client.app.state.container
    target = SimpleNamespace(
        model="stub-model",
        provider="deepseek",
        key_id=None,
        channel=None,
        source="test",
    )

    async def fake_resolve(**_: object) -> SimpleNamespace:
        return target

    async def fake_stream(request, resolved):
        assert resolved is target
        yield ("delta", {"text": "print("})
        yield ("delta", {"text": "1)\n"})
        yield (
            "done",
            {
                "mode": "chat",
                "model": target.model,
                "provider": target.provider,
                "key_id": None,
                "usage": None,
                "truncated": False,
                # 真实链路里这些是 `CompletionService.stream` 包装后补上的
                "first_token_ms": 90,
                "total_ms": 210,
                "chars": 9,
                "empty": False,
            },
        )

    container.completion.resolve = fake_resolve
    container.completion.stream = fake_stream
    return target


def test_fim_streams_sse_events(client: TestClient) -> None:
    _stub_completion(client)

    response = client.post(
        "/api/v1/models/fim",
        json={"prefix": "print", "suffix": "\n", "path": "a.py", "language_id": "python"},
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    assert "event: delta" in response.text
    assert "event: done" in response.text
    assert '"text": "print("' in response.text
    assert '"model": "stub-model"' in response.text


def test_fim_returns_503_without_any_channel(client: TestClient) -> None:
    response = client.post("/api/v1/models/fim", json={"prefix": "x", "path": "a.py"})

    assert response.status_code == 503
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "configuration_error"


# --- 补全埋点：done 事件字段 / 接口计数 ---


async def _collect(stream) -> list[tuple[str, dict]]:
    return [event async for event in stream]


def test_completion_service_done_event_carries_latency_metrics(container) -> None:
    async def fake_impl(request, target):
        yield ("delta", {"text": "abc"})
        yield ("delta", {"text": "de"})
        yield ("done", {"mode": "chat", "model": "stub-model"})

    service = container.completion
    service._stream_impl = fake_impl
    target = SimpleNamespace(model="stub-model")

    events = asyncio.run(_collect(service.stream(FimRequest(prefix="x", path="a.py"), target)))

    done = events[-1][1]
    assert done["chars"] == 5
    assert done["empty"] is False
    assert isinstance(done["first_token_ms"], int)
    assert done["first_token_ms"] >= 0
    assert isinstance(done["total_ms"], int)
    assert done["total_ms"] >= 0


def test_completion_service_marks_empty_completion(container) -> None:
    async def fake_impl(request, target):
        yield ("done", {"mode": "chat", "model": "stub-model"})

    service = container.completion
    service._stream_impl = fake_impl
    target = SimpleNamespace(model="stub-model")

    events = asyncio.run(_collect(service.stream(FimRequest(prefix="x", path="a.py"), target)))

    done = events[-1][1]
    assert done["chars"] == 0
    assert done["empty"] is True
    # 一个 delta 都没有，就不该有首字延迟样本
    assert done["first_token_ms"] is None


def test_completion_service_chat_budget_has_reasoning_floor() -> None:
    # reasoning 模型会先花 token 思考：chat 路径必须留足预算，否则 content 全空
    request = FimRequest(prefix="x", path="a.py")

    assert CompletionService._max_tokens(request, floor=CHAT_MIN_MAX_TOKENS) == 1024
    # 原生 FIM 不需要放这么多
    assert CompletionService._max_tokens(request) == 512
    # 调用方显式给值时不覆盖
    assert CompletionService._max_tokens(FimRequest(prefix="x", max_tokens=128)) == 128


def test_completion_stats_api_starts_empty(client: TestClient) -> None:
    response = client.get("/api/v1/ide/completion/stats")

    assert response.status_code == 200, response.text
    body = response.json()
    totals = body["data"]["totals"]
    assert totals["requests"] == 0
    assert totals["acceptance_rate"] == 0.0
    assert totals["avg_first_token_ms"] is None
    assert body["data"]["days"] == []
    assert body["data"]["by_model"] == {}
    assert body["metadata"]["requests"] == 0


def test_completion_stats_api_records_delivery_and_acceptance(client: TestClient) -> None:
    _stub_completion(client)

    stream = client.post(
        "/api/v1/models/fim",
        json={"prefix": "print", "suffix": "\n", "path": "a.py", "language_id": "python"},
    )
    assert stream.status_code == 200, stream.text
    assert "event: done" in stream.text

    accepted = client.post(
        "/api/v1/ide/completion/accepted",
        json={
            "accepted": True,
            "mode": "chat",
            "model": "stub-model",
            "language_id": "python",
            "completion_mode": "line",
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["data"]["accepted"] is True

    body = client.get("/api/v1/ide/completion/stats").json()
    data = body["data"]
    totals = data["totals"]
    assert totals["requests"] == 1
    assert totals["successes"] == 1
    assert totals["delivered"] == 1
    assert totals["accepted"] == 1
    assert totals["acceptance_rate"] == 1.0
    assert totals["avg_first_token_ms"] == 90
    assert totals["avg_total_ms"] == 210
    assert data["by_model"]["stub-model"]["delivered"] == 1
    assert data["by_mode"]["chat"]["successes"] == 1
    assert data["last"]["chars"] == 9
    assert data["last_accept"]["language_id"] == "python"
    # 通道（mode）与交互模式（completion_mode）分开记录
    assert data["last_accept"]["mode"] == "chat"
    assert data["last_accept"]["completion_mode"] == "line"
    assert data["days"][0]["requests"] == 1
    assert body["metadata"] == {
        "requests": 1,
        "acceptance_rate": 1.0,
        "avg_first_token_ms": 90,
    }


def test_completion_stats_api_counts_failures(client: TestClient) -> None:
    _stub_completion(client)
    container = client.app.state.container

    async def fake_stream(request, resolved):
        yield ("error", {"code": "provider_error", "message": "boom"})

    container.completion.stream = fake_stream

    response = client.post(
        "/api/v1/models/fim",
        json={"prefix": "print", "path": "a.py", "language_id": "python"},
    )
    assert response.status_code == 200, response.text
    assert "event: error" in response.text

    totals = client.get("/api/v1/ide/completion/stats").json()["data"]["totals"]
    assert totals["requests"] == 1
    assert totals["failures"] == 1
    assert totals["successes"] == 0


def test_completion_accepted_api_rejects_overlong_fields(client: TestClient) -> None:
    response = client.post(
        "/api/v1/ide/completion/accepted",
        json={"accepted": True, "mode": "x" * 32},
    )

    assert response.status_code == 422


def test_completion_accepted_api_keeps_mode_buckets_on_channel(client: TestClient) -> None:
    # 只给了交互模式、没有通道：不能把 line 混进 by_mode（by_mode 是 fim/chat）
    response = client.post(
        "/api/v1/ide/completion/accepted",
        json={"accepted": True, "completion_mode": "line"},
    )
    assert response.status_code == 200, response.text

    data = client.get("/api/v1/ide/completion/stats").json()["data"]
    assert data["by_mode"] == {}
    assert data["last_accept"]["completion_mode"] == "line"
    assert data["last_accept"]["mode"] is None
