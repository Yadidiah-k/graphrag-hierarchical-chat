"""Audit log for completed /chat calls.

Writes one row per successful chat turn (query, what was retrieved from
vector + graph, what was answered, latency) -- both a debugging trail and
the storage foundation the Priority 3 evals/LangSmith work will build on.
Only successful generations are logged; a failed chat turn already surfaces
via the SSE error event and normal app logs.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.models import QueryLog
from app.db.session import get_sessionmaker
from app.schemas.models import CitationChunk, CitationTriple


class QueryLogStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def record(
        self,
        query_text: str,
        document_id_filter: str | None,
        top_k: int,
        citations: list[CitationChunk],
        linked_node_ids: list[str],
        graph_triples: list[CitationTriple],
        answer_text: str,
        latency_ms: int,
    ) -> None:
        row = QueryLog(
            id=str(uuid.uuid4()),
            query_text=query_text,
            document_id_filter=document_id_filter,
            top_k=top_k,
            citations=[c.model_dump() for c in citations],
            linked_node_ids=list(linked_node_ids),
            graph_triples=[t.model_dump() for t in graph_triples],
            answer_text=answer_text,
            latency_ms=latency_ms,
        )
        with self._session_factory() as session:
            session.add(row)
            session.commit()


def build_query_log_store(settings: Settings) -> QueryLogStore:
    return QueryLogStore(get_sessionmaker(settings))
