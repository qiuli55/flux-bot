"""OpenAI 兼容的 Embedding 客户端。

只要 `/embeddings` 兼容 OpenAI 协议即可（OpenAI、DeepSeek、vLLM、Ollama 等）。
任何失败都返回空列表，由检索层降级为纯词法打分——索引不能因为向量服务抖动
就整体不可用。
"""

from __future__ import annotations

import httpx

from flux.logging import get_logger

logger = get_logger(__name__)


class EmbeddingClient:
    """批量调用 `/embeddings`；未配置密钥时 `enabled=False`。"""

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str,
        model: str,
        dimensions: int | None = None,
        timeout: float = 30.0,
        batch_size: int = 32,
    ) -> None:
        self._api_key = (api_key or "").strip()
        self._base_url = base_url.rstrip("/")
        self._model = model.strip()
        self._dimensions = dimensions
        self._timeout = timeout
        self._batch_size = max(1, batch_size)
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._api_key and self._model)

    @property
    def model(self) -> str | None:
        return self._model if self.enabled else None

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """返回与输入等长的向量列表；失败或未启用时返回空列表。"""
        if not texts or not self.enabled:
            return []
        try:
            return await self._embed_all(texts)
        except Exception as exc:  # noqa: BLE001 - 向量失败必须降级而不是中断索引
            logger.warning("知识库 embedding 调用失败，本次降级为词法检索：%s", exc)
            return []

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _embed_all(self, texts: list[str]) -> list[list[float]]:
        client = await self._get_client()
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            payload: dict = {"model": self._model, "input": batch}
            if self._dimensions:
                payload["dimensions"] = self._dimensions
            response = await client.post(
                f"{self._base_url}/embeddings",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
            response.raise_for_status()
            data = response.json().get("data") or []
            if len(data) != len(batch):
                raise ValueError(f"embedding 返回条数不符：期望 {len(batch)}，实际 {len(data)}")
            for item in data:
                vector = item.get("embedding")
                if not isinstance(vector, list) or not vector:
                    raise ValueError("embedding 返回了空向量")
                vectors.append([float(value) for value in vector])
        return vectors

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client
