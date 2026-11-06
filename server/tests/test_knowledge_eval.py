"""知识库评测集骨架。

金标集分两步走：先固定「问什么」和「命中里必须出现什么」，等真实检索结果稳定后
再补 expectation 的 source_id / chunk seq。这里只校验骨架本身可加载、字段完整，
真正的跑分在接入评测脚本后由 `run_eval` 逐条执行。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_EVAL_PATH = Path(__file__).with_name("data") / "knowledge_eval.jsonl"
_SOURCES = {None, "conversation", "doc", "brain"}


def load_eval_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for line in _EVAL_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            cases.append(json.loads(line))
    return cases


def test_eval_skeleton_is_valid() -> None:
    cases = load_eval_cases()

    assert cases
    assert len({case["id"] for case in cases}) == len(cases)
    for case in cases:
        assert isinstance(case["query"], str) and case["query"].strip()
        assert case["source_type"] in _SOURCES
        assert isinstance(case["expect_any"], list) and case["expect_any"]
        assert all(isinstance(item, str) and item.strip() for item in case["expect_any"])
        assert isinstance(case.get("notes", ""), str)
