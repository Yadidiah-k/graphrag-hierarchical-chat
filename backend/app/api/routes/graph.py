"""Subgraph lookup for the frontend's graph visualizer."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_graph_store
from app.graph.neo4j_client import Neo4jGraphStore
from app.schemas.models import SubgraphResponse

router = APIRouter()


@router.get("/graph/subgraph", response_model=SubgraphResponse)
async def get_subgraph(
    query: str | None = None,
    node_ids: list[str] | None = Query(default=None),
    hop_depth: int | None = None,
    graph_store: Neo4jGraphStore = Depends(get_graph_store),
) -> SubgraphResponse:
    if not query and not node_ids:
        raise HTTPException(status_code=400, detail="provide 'query' or 'node_ids'")

    seed_ids = node_ids or await asyncio.to_thread(graph_store.find_node_ids_by_name_fragment, query)
    nodes, edges = await asyncio.to_thread(graph_store.neighborhood, seed_ids, hop_depth)
    return SubgraphResponse(nodes=nodes, edges=edges)
