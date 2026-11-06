"""混合检索：BM25 词法 + 向量余弦，再用 MMR 去重。

为什么不只做向量：项目里的专有名词、文件名、错误码靠精确词面召回更稳；
为什么不只做词法：用户换个说法问「上次那个部署失败」时，向量才接得住。
两路都拿不到分时返回空，而不是拿无关块凑数。
"""

from __future__ import annotations

import math
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from flux.core.knowledge.embedding import EmbeddingClient
from flux.core.knowledge.repository import KnowledgeRepository
from flux.core.knowledge.text import snippet, tokenize
from flux.logging import get_logger
from flux.models.knowledge import KnowledgeChunk

logger = get_logger(__name__)

_LEXICAL_WEIGHT = 0.65
_VECTOR_WEIGHT = 0.35
_MMR_LAMBDA = 0.7
_MAX_QUERY_TERMS = 12


@dataclass(frozen=True)
class KnowledgeHit:
    """一条检索命中：原文块 + 归一化得分 + 展示用片段。"""

    chunk: KnowledgeChunk
    score: float
    snippet: str


class KnowledgeRetriever:
    """知识库检索入口，供 `assistant.search` 调用。"""

    def __init__(
        self,
        *,
        repository: KnowledgeRepository,
        embedder: EmbeddingClient,
        candidate_limit: int = 200,
    ) -> None:
        self._repo = repository
        self._embedder = embedder
        self._candidate_limit = max(10, candidate_limit)

    async def search(
        self,
        query: str,
        *,
        limit: int = 8,
        source_type: str | None = None,
        project_id: uuid.UUID | None = None,
        task_id: uuid.UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[KnowledgeHit]:
        query = (query or "").strip()
        if not query:
            return []
        limit = max(1, min(int(limit), 50))
        terms = _query_terms(query)
        if not terms:
            return []
        candidates = await self._repo.search_candidates(
            terms,
            limit=self._candidate_limit,
            source_type=source_type,
            project_id=project_id,
            task_id=task_id,
            since=since,
            until=until,
        )
        if not candidates:
            return []

        token_counts = [
            Counter(tokenize(f"{chunk.title or ''}\n{chunk.content}")) for chunk in candidates
        ]
        lexical = _normalize(_bm25(terms, token_counts))
        vector = await self._vector_scores(query, candidates)

        scores: list[float] = []
        for index, lexical_score in enumerate(lexical):
            if vector[index] is None:
                scores.append(lexical_score)
            else:
                scores.append(_LEXICAL_WEIGHT * lexical_score + _VECTOR_WEIGHT * vector[index])
        order = _mmr(scores, token_counts, limit)
        hits = [
            KnowledgeHit(
                chunk=candidates[index],
                score=round(scores[index], 6),
                snippet=snippet(candidates[index].content, terms),
            )
            for index in order
            if scores[index] > 0
        ]
        return hits

    async def _vector_scores(
        self, query: str, candidates: list[KnowledgeChunk]
    ) -> list[float | None]:
        """返回与候选等长的余弦分；未启用或该块没有向量时为 None。"""
        if not self._embedder.enabled:
            return [None] * len(candidates)
        if not any(chunk.embedding for chunk in candidates):
            return [None] * len(candidates)
        vectors = await self._embedder.embed([query])
        if len(vectors) != 1:
            return [None] * len(candidates)
        query_vector = vectors[0]
        result: list[float | None] = []
        for chunk in candidates:
            if not chunk.embedding:
                result.append(None)
                continue
            result.append(max(0.0, _dense_cosine(query_vector, chunk.embedding)))
        return result


def _query_terms(query: str) -> list[str]:
    """查询词元：去重、丢掉单字噪声，最多 12 个（长查询只取前 12 个）。"""
    seen: list[str] = []
    for token in tokenize(query):
        if len(token) < 2:
            continue
        if token not in seen:
            seen.append(token)
        if len(seen) >= _MAX_QUERY_TERMS:
            break
    return seen


def _bm25(terms: list[str], documents: list[Counter[str]], k1: float = 1.5, b: float = 0.75):
    total = len(documents)
    if total == 0:
        return []
    lengths = [sum(counts.values()) for counts in documents]
    average = sum(lengths) / total or 1.0
    document_frequency = Counter()
    for counts in documents:
        for term in counts:
            document_frequency[term] += 1
    scores: list[float] = []
    for counts, length in zip(documents, lengths, strict=True):
        score = 0.0
        for term in terms:
            frequency = counts.get(term, 0)
            if frequency == 0:
                continue
            df = document_frequency[term]
            idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
            denominator = frequency + k1 * (1 - b + b * length / average)
            score += idf * frequency * (k1 + 1) / denominator
        scores.append(score)
    return scores


def _normalize(scores: list[float]) -> list[float]:
    if not scores:
        return []
    peak = max(scores)
    if peak <= 0:
        return [0.0] * len(scores)
    return [score / peak for score in scores]


def _counter_cosine(left: Counter[str], right: Counter[str]) -> float:
    if not left or not right:
        return 0.0
    shared = set(left) & set(right)
    numerator = sum(left[term] * right[term] for term in shared)
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return numerator / (left_norm * right_norm)


def _dense_cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return numerator / (left_norm * right_norm)


def _mmr(scores: list[float], token_counts: list[Counter[str]], limit: int) -> list[int]:
    """最大边际相关：在得分和多样性之间折中，避免整段对话霸榜。"""
    selected: list[int] = []
    remaining = set(range(len(scores)))
    while remaining and len(selected) < limit:
        best_index = -1
        best_value = float("-inf")
        for index in remaining:
            redundancy = 0.0
            for chosen in selected:
                redundancy = max(
                    redundancy, _counter_cosine(token_counts[index], token_counts[chosen])
                )
            value = _MMR_LAMBDA * scores[index] - (1 - _MMR_LAMBDA) * redundancy
            if value > best_value:
                best_value = value
                best_index = index
        selected.append(best_index)
        remaining.discard(best_index)
    return selected
