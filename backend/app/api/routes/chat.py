"""Chat over ingested documents, streamed via Server-Sent Events.

The RAG pipeline's retrieval/graph steps and its token generator are all
synchronous (blocking network calls under the hood). To keep the event
loop free for other requests, the blocking steps run in a thread
(asyncio.to_thread) and the token generator is bridged into an async
iterator via Starlette's iterate_in_threadpool. Every successful call is
recorded to query_logs once the stream finishes, latency measured around
the whole request with time.perf_counter().
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import iterate_in_threadpool

from app.api.deps import get_rag_pipeline
from app.core.config import get_settings
from app.rag.pipeline import GraphRagPipeline
from app.schemas.models import ChatEventType, ChatRequest, ChatStreamEvent

router = APIRouter()


def _sse(event: ChatStreamEvent) -> str:
    return f"data: {event.model_dump_json()}\n\n"


@router.post("/chat")
async def chat(
    payload: ChatRequest,
    request: Request,
    pipeline: GraphRagPipeline = Depends(get_rag_pipeline),
) -> StreamingResponse:
    settings = get_settings()
    query_log_store = request.app.state.query_log_store

    async def event_source() -> AsyncIterator[str]:
        started_at = time.perf_counter()
        try:
            token_iter, citations, triples, linked_node_ids = await asyncio.to_thread(
                pipeline.answer_stream, payload.query, payload.document_id, payload.top_k
            )
            yield _sse(ChatStreamEvent(type=ChatEventType.citations, data=citations))
            yield _sse(ChatStreamEvent(type=ChatEventType.triples, data=triples))
            answer_parts: list[str] = []
            async for token in iterate_in_threadpool(token_iter):
                answer_parts.append(token)
                yield _sse(ChatStreamEvent(type=ChatEventType.token, data=token))
            yield _sse(ChatStreamEvent(type=ChatEventType.done, data=None))

            latency_ms = int((time.perf_counter() - started_at) * 1000)
            await asyncio.to_thread(
                query_log_store.record,
                query_text=payload.query,
                document_id_filter=payload.document_id,
                top_k=payload.top_k or settings.top_k_vector,
                citations=citations,
                linked_node_ids=linked_node_ids,
                graph_triples=triples,
                answer_text="".join(answer_parts),
                latency_ms=latency_ms,
            )
        except Exception as exc:
            yield _sse(ChatStreamEvent(type=ChatEventType.error, data=str(exc)))

    return StreamingResponse(event_source(), media_type="text/event-stream")
