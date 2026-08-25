"""Provider-agnostic embedding interface.

Application code depends on `EmbeddingProvider`, not on the OpenAI SDK
directly, so the backend can swap providers without touching chunking,
vector storage, or retrieval code.
"""

from __future__ import annotations

from typing import Protocol

from openai import OpenAI

from app.core.config import Settings


class EmbeddingProvider(Protocol):
    dimension: int

    def embed_texts(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class OpenAIEmbeddingProvider:
    def __init__(self, settings: Settings) -> None:
        self._client = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
        self._model = settings.embedding_model
        self.dimension = settings.embedding_dimension

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = self._client.embeddings.create(model=self._model, input=texts)
        return [item.embedding for item in response.data]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_texts([text])[0]


def build_embedding_provider(settings: Settings) -> EmbeddingProvider:
    return OpenAIEmbeddingProvider(settings)
