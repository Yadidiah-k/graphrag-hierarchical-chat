"""Async document ingestion.

POST /ingest returns immediately with status=queued and runs the actual
chunk -> embed -> vector-store -> extract -> graph-write pipeline as a
background task (offloaded to a thread by Starlette, since the
underlying clients are all synchronous). Callers poll GET /ingest/{id}
for status and final counts.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

from app.api.deps import get_ingestion_service
from app.schemas.models import IngestJobStatus, IngestRequest, IngestResponse
from app.services.ingestion import IngestionService

router = APIRouter()
logger = logging.getLogger("graphrag.api.ingest")


def _run_ingestion(
    ingestion_service: IngestionService,
    jobs: dict[str, IngestResponse],
    document_id: str,
    text: str,
) -> None:
    jobs[document_id] = IngestResponse(document_id=document_id, status=IngestJobStatus.running)
    try:
        jobs[document_id] = ingestion_service.ingest(document_id, text)
    except Exception:
        logger.exception("ingestion failed", extra={"document_id": document_id})
        jobs[document_id] = IngestResponse(document_id=document_id, status=IngestJobStatus.failed)


@router.post("/ingest", response_model=IngestResponse, status_code=202)
async def ingest_document(
    payload: IngestRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    ingestion_service: IngestionService = Depends(get_ingestion_service),
) -> IngestResponse:
    if not payload.text.strip():
        raise HTTPException(status_code=400, detail="text must not be empty")

    jobs: dict[str, IngestResponse] = request.app.state.ingest_jobs
    jobs[payload.document_id] = IngestResponse(document_id=payload.document_id, status=IngestJobStatus.queued)
    background_tasks.add_task(_run_ingestion, ingestion_service, jobs, payload.document_id, payload.text)
    return jobs[payload.document_id]


@router.get("/ingest/{document_id}", response_model=IngestResponse)
async def get_ingest_status(document_id: str, request: Request) -> IngestResponse:
    job = request.app.state.ingest_jobs.get(document_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown document_id")
    return job
