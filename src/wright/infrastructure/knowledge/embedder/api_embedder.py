"""API-based dense vector embedder supporting OpenAI and SiliconFlow standards."""

from __future__ import annotations

import os
from collections.abc import Sequence

import httpx

from ....domain.gateway.embedder import IEmbedder
from ....domain.model.knowledge import KnowledgeUnavailable


class ApiEmbedder(IEmbedder):
    """Embedder using OpenAI-compatible /v1/embeddings endpoint."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.api_key = (
            api_key
            or os.environ.get("SILICONFLOW_API_KEY", "").strip()
            or os.environ.get("OPENAI_API_KEY", "").strip()
            or os.environ.get("LLM_API_KEY", "").strip()
        )
        if base_url:
            self.base_url = base_url.rstrip("/")
        elif os.environ.get("SILICONFLOW_API_KEY"):
            self.base_url = "https://api.siliconflow.cn/v1"
        elif os.environ.get("OPENAI_BASE_URL"):
            self.base_url = os.environ.get("OPENAI_BASE_URL", "").rstrip("/")
        else:
            self.base_url = "https://api.openai.com/v1"

        if model:
            self.model = model
        elif "siliconflow" in self.base_url:
            self.model = os.environ.get("SILICONFLOW_EMBEDDING_MODEL", "BAAI/bge-m3")
        else:
            self.model = os.environ.get("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")

        self.timeout = timeout

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        """Compute embeddings for a batch of text chunks."""
        if not texts:
            return []
        if not self.api_key:
            raise KnowledgeUnavailable(
                "Missing embedding API key. Set SILICONFLOW_API_KEY or OPENAI_API_KEY."
            )

        url = f"{self.base_url}/embeddings"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "input": list(texts),
        }

        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(url, json=payload, headers=headers)
                if resp.status_code != 200:
                    raise KnowledgeUnavailable(
                        f"Embedding API request failed (HTTP {resp.status_code}): {resp.text[:300]}"
                    )
                data = resp.json()
        except KnowledgeUnavailable:
            raise
        except Exception as exc:
            raise KnowledgeUnavailable(f"Embedding API request failed: {type(exc).__name__}: {exc}") from exc

        items = data.get("data", [])
        # Sort by index to maintain original order
        items_sorted = sorted(items, key=lambda x: x.get("index", 0))
        return [item.get("embedding", []) for item in items_sorted]

    def embed_query(self, text: str) -> list[float]:
        """Compute embedding for a single query."""
        results = self.embed_texts([text])
        if not results:
            raise KnowledgeUnavailable("Embedding API returned no vectors")
        return results[0]


__all__ = ["ApiEmbedder"]
