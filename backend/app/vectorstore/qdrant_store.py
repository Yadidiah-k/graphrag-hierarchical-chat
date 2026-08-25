"""Vector store boundary backed by Qdrant.

Stores child chunks (small, precise) with payload pointing back to their
parent chunk id, document id, and text, so a vector hit on a child chunk can
be expanded to full parent context at retrieval time.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from app.core.config import Settings


@dataclass
class VectorHit:
    child_id: str
    parent_id: str
    document_id: str
    text: str
    score: float


class QdrantVectorStore:
    def __init__(self, settings: Settings, dimension: int) -> None:
        self._client = QdrantClient(url=settings.qdrant_url)
        self._collection = settings.qdrant_collection
        self._dimension = dimension
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        existing = {c.name for c in self._client.get_collections().collections}
        if self._collection not in existing:
            self._client.create_collection(
                collection_name=self._collection,
                vectors_config=qmodels.VectorParams(
                    size=self._dimension, distance=qmodels.Distance.COSINE
                ),
            )

    def upsert_children(
        self,
        child_ids: list[str],
        vectors: list[list[float]],
        payloads: list[dict],
    ) -> None:
        if not (len(child_ids) == len(vectors) == len(payloads)):
            raise ValueError("child_ids, vectors, and payloads must be the same length")

        points = [
            qmodels.PointStruct(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, child_id)),
                vector=vector,
                payload={**payload, "child_id": child_id},
            )
            for child_id, vector, payload in zip(child_ids, vectors, payloads)
        ]
        if points:
            self._client.upsert(collection_name=self._collection, points=points)

    def get_children_by_ids(self, child_ids: list[str]) -> dict[str, dict]:
        if not child_ids:
            return {}
        point_ids = [str(uuid.uuid5(uuid.NAMESPACE_URL, cid)) for cid in child_ids]
        points = self._client.retrieve(collection_name=self._collection, ids=point_ids)
        return {p.payload["child_id"]: p.payload for p in points}

    def search(
        self,
        query_vector: list[float],
        top_k: int = 8,
        document_id: str | None = None,
    ) -> list[VectorHit]:
        query_filter = None
        if document_id:
            query_filter = qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="document_id", match=qmodels.MatchValue(value=document_id)
                    )
                ]
            )

        results = self._client.search(
            collection_name=self._collection,
            query_vector=query_vector,
            limit=top_k,
            query_filter=query_filter,
        )
        return [
            VectorHit(
                child_id=r.payload["child_id"],
                parent_id=r.payload["parent_id"],
                document_id=r.payload["document_id"],
                text=r.payload["text"],
                score=r.score,
            )
            for r in results
        ]


def build_vector_store(settings: Settings, dimension: int) -> QdrantVectorStore:
    return QdrantVectorStore(settings, dimension)
