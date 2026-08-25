"""Chat over ingested documents, streamed via Server-Sent Events.

The RAG pipeline's retrieval/graph steps and its token generator are all
synchronous (blocking network calls under the hood). To keep the event
loop free for other requests, the blocking steps run in a thread
(asyncio.to_thread) and the token generator is bridged into an async
iterator via Starlette's iterate_in_threadpool. generate_rationale runs
after the token stream is fully consumed (it needs the complete answer
text) and is skipped, along with the query_logs write, when the query was
rejected as gibberish -- matching the existing "log only completed turns"
scope. Latency is measured around the whole request with
time.perf_counter().

generate_rationale + the query_logs write are wrapped in their own
try/except, separate from the outer one: by the time either runs, the
answer has already streamed successfully to the user. A rationale
failure (e.g. a transient rate limit hit specifically on that call) is
logged and silently skipped rather than surfaced as a chat "error" event
-- the alternative was a real, observed bug: an already-correct answer
getting a raw exception repr appended to it, making a working turn look
broken.
"""

from __future__ import annotations

import asyncio
import logging
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
logger = logging.getLogger("graphrag.api.chat")


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
            result = await asyncio.to_thread(
                pipeline.answer_stream,
                payload.query,
                payload.document_id,
                payload.top_k,
                payload.history,
                payload.section_title_filter,
                payload.content_type_filter,
            )
            yield _sse(ChatStreamEvent(type=ChatEventType.citations, data=result.citations))
            yield _sse(ChatStreamEvent(type=ChatEventType.triples, data=result.triples))
            answer_parts: list[str] = []
            async for token in iterate_in_threadpool(result.token_iter):
                answer_parts.append(token)
                yield _sse(ChatStreamEvent(type=ChatEventType.token, data=token))
            yield _sse(ChatStreamEvent(type=ChatEventType.done, data=None))

            if result.is_gibberish:
                return

            answer_text = "".join(answer_parts)
            try:
                rationale = await asyncio.to_thread(
                    pipeline.generate_rationale,
                    result.rewritten_query,
                    answer_text,
                    result.citations,
                    result.triples,
                )
                yield _sse(ChatStreamEvent(type=ChatEventType.rationale, data=rationale))

                latency_ms = int((time.perf_counter() - started_at) * 1000)
                await asyncio.to_thread(
                    query_log_store.record,
                    query_text=payload.query,
                    document_id_filter=payload.document_id,
                    top_k=payload.top_k or settings.top_k_vector,
                    citations=result.citations,
                    linked_node_ids=result.linked_node_ids,
                    graph_triples=result.triples,
                    answer_text=answer_text,
                    rationale_text=rationale.explanation,
                    latency_ms=latency_ms,
                )
            except Exception:
                logger.exception(
                    "rationale generation or query_logs write failed -- answer already "
                    "streamed successfully, not surfacing this as a chat error",
                    extra={"query": payload.query},
                )
        except Exception as exc:
            yield _sse(ChatStreamEvent(type=ChatEventType.error, data=str(exc)))

    return StreamingResponse(event_source(), media_type="text/event-stream")
