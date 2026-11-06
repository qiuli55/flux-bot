"""Supervisor（助理）P4：线程摘要、项目进度与检索。

P4 的验收标准：

1. 线程内工单全部收尾后只生成一份摘要，重复读取项目页不会重复写；
2. 摘要渲染进 Project Brain 的 progress 分区，并出现在项目上下文里；
3. 项目页把项目信息、阶段计数、工单、报告、摘要与时间线一次给全；
4. 检索覆盖工单 / 报告 / 摘要，`%` 与 `_` 按字面量处理，空关键字拒绝。
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

PREFIX = "/api/v1/supervisor"
API = "/api/v1"


# --- 工具 ---


def _create_project(client: TestClient, *, name: str = "P4 项目") -> str:
    response = client.post(f"{API}/projects", json={"name": name, "repository": "qiuli55/flux"})
    assert response.status_code == 200, response.text
    return response.json()["data"]["id"]


def _event(project_id: str, **overrides) -> dict:
    payload = {
        "source_key": "feishu",
        "external_id": "om_p4",
        "event_type": "message",
        "actor_id": "ou_owner",
        "actor_platform": "feishu",
        "thread_key": "feishu:chat_p4",
        "content": "把登录页拆成三个组件",
        "project_id": project_id,
    }
    payload.update(overrides)
    return payload


def _ingest(client: TestClient, project_id: str, **overrides) -> dict:
    response = client.post(f"{PREFIX}/events", json=_event(project_id, **overrides))
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _plan(client: TestClient, trigger_id: str) -> dict:
    response = client.post(f"{PREFIX}/observations/{trigger_id}/plan")
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _stop(client: TestClient, ticket_id: str, note: str) -> dict:
    response = client.post(f"{PREFIX}/tickets/{ticket_id}/stop", json={"note": note})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _project_page(client: TestClient, project_id: str) -> dict:
    response = client.get(f"{PREFIX}/projects/{project_id}")
    assert response.status_code == 200, response.text
    return response.json()


def _memory(client: TestClient, project_id: str) -> dict:
    response = client.get(f"{API}/projects/{project_id}/memory")
    assert response.status_code == 200, response.text
    return response.json()["data"]


# --- 摘要与项目进度 ---


def test_closed_thread_writes_digest_and_progress(client: TestClient) -> None:
    project_id = _create_project(client)
    trigger = _ingest(client, project_id)
    ticket_id = _plan(client, trigger["id"])["tickets"][0]["id"]
    _stop(client, ticket_id, "登录页改版已取消")

    page = _project_page(client, project_id)
    assert page["data"]["project"]["id"] == project_id
    assert page["data"]["counts"]["stopped"] == 1
    assert [item["id"] for item in page["data"]["tickets"]] == [ticket_id]
    assert page["data"]["reports"][0]["status"] == "cancelled"
    assert page["data"]["reports"][0]["summary"] == "登录页改版已取消"

    digests = page["data"]["digests"]
    assert len(digests) == 1
    digest = digests[0]
    assert digest["thread_id"] == page["data"]["tickets"][0]["thread_id"]
    assert digest["project_id"] == project_id
    assert digest["ticket_ids"] == [ticket_id]
    assert "feishu:chat_p4" in digest["summary"]
    assert "登录页改版已取消" in digest["summary"]
    assert digest["decisions"][0]["stage"] == "stopped"
    assert digest["decisions"][0]["report_status"] == "cancelled"

    # 摘要只写一次：再读两遍项目页也不会多出第二条
    assert len(_project_page(client, project_id)["data"]["digests"]) == 1
    assert len(_project_page(client, project_id)["data"]["digests"]) == 1

    # 摘要已渲染进 Brain 的 progress 分区
    progress = _memory(client, project_id)["progress"]
    assert len(progress) == 1
    assert "项目进度" in progress[0]["content"]
    assert "登录页改版已取消" in progress[0]["content"]

    context = client.get(f"{API}/projects/{project_id}/memory/context").json()["data"]["text"]
    assert "Project Progress" in context
    assert "登录页改版已取消" in context


def test_open_thread_has_no_digest(client: TestClient) -> None:
    project_id = _create_project(client)
    trigger = _ingest(client, project_id)
    _plan(client, trigger["id"])

    page = _project_page(client, project_id)
    assert page["data"]["digests"] == []
    assert page["data"]["counts"]["planned"] == 1
    assert _memory(client, project_id)["progress"] == []


def test_project_page_rejects_unknown_project(client: TestClient) -> None:
    response = client.get(f"{PREFIX}/projects/{uuid.uuid4()}")
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "not_found"


# --- 检索 ---


def test_search_hits_ticket_report_and_digest(client: TestClient) -> None:
    project_id = _create_project(client)
    trigger = _ingest(client, project_id, content="登录页拆成三个组件")
    ticket_id = _plan(client, trigger["id"])["tickets"][0]["id"]
    _stop(client, ticket_id, "登录页拆完，剩余走下一个迭代")
    _project_page(client, project_id)  # 触发摘要落库

    response = client.get(f"{PREFIX}/search", params={"q": "登录页"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["metadata"]["count"] == len(body["data"])
    assert {hit["kind"] for hit in body["data"]} == {"ticket", "report", "digest"}
    assert all("登录页" in (hit["snippet"] or hit["title"]) for hit in body["data"])

    # 摘要独有的统计行只有摘要能命中（报告里没有这句）
    digest_hits = client.get(f"{PREFIX}/search", params={"q": "张工单"}).json()["data"]
    assert [hit["kind"] for hit in digest_hits] == ["digest"]


def test_search_escapes_like_wildcards(client: TestClient) -> None:
    project_id = _create_project(client)
    trigger = _ingest(client, project_id, content="折扣 50% 活动页")
    ticket_id = _plan(client, trigger["id"])["tickets"][0]["id"]
    _stop(client, ticket_id, "活动页已下线")

    percent = client.get(f"{PREFIX}/search", params={"q": "50%"}).json()["data"]
    assert percent and all(hit["kind"] in {"ticket", "report"} for hit in percent)

    # 若 `_` 没被转义会匹配任意字符、把全部记录都捞出来
    underscore = client.get(f"{PREFIX}/search", params={"q": "_"}).json()
    assert underscore["data"] == []
    assert underscore["metadata"]["count"] == 0


def test_search_rejects_blank_keyword(client: TestClient) -> None:
    assert client.get(f"{PREFIX}/search", params={"q": ""}).status_code == 422
    blank = client.get(f"{PREFIX}/search", params={"q": "   "})
    assert blank.status_code == 422, blank.text
    assert blank.json()["code"] == "validation_error"
