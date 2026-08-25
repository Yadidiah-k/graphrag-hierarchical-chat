"""Chat over ingested documents, streamed via Server-Sent Events.

The RAG pipeline's retrieval/graph steps and its token generator are all
synchronous (blocking network calls under the hood). To keep the event
loop free for other requests, the blocking steps run in a thread
(asyncio.to_thread) and the token generator is bridged into an async
iterator via Starlette's iterate_in_threadpool.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from starlette.concurrency import iterate_in_threadpool

from app.api.deps import get_rag_pipeline
from app.rag.pipeline import GraphRagPipeline
from app.schemas.models import ChatEventType, ChatRequest, ChatStreamEvent

router = APIRouter()


def _sse(event: ChatStreamEvent) -> str:
    return f"data: {event.model_dump_json()}\n\n"


@router.post("/chat")
async def chat(
    payload: ChatRequest,
    pipeline: GraphRagPipeline = Depends(get_rag_pipeline),
) -> StreamingResponse:
    async def event_source() -> AsyncIterator[str]:
        try:
            token_iter, citations, triples = await asyncio.to_thread(
                pipeline.answer_stream, payload.query, payload.document_id, payload.top_k
            )
            yield _sse(ChatStreamEvent(type=ChatEventType.citations, data=citations))
            yield _sse(ChatStreamEvent(type=ChatEventType.triples, data=triples))
            async for token in iterate_in_threadpool(token_iter):
                yield _sse(ChatStreamEvent(type=ChatEventType.token, data=token))
            yield _sse(ChatStreamEvent(type=ChatEventType.done, data=None))
        except Exception as exc:
            yield _sse(ChatStreamEvent(type=ChatEventType.error, data=str(exc)))

    return StreamingResponse(event_source(), media_type="text/event-stream")
