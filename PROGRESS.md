# Progress (in-progress build, not final)

## Done
- `requirements.txt` - pinned backend dependencies (fastapi, langgraph, neo4j, qdrant-client, ...)
- `app/core/config.py` - typed settings
- `app/schemas/models.py` - all API/domain Pydantic contracts
- `app/chunking/hierarchical_chunker.py` - parent/child chunker
- `app/services/embeddings.py` - embedding provider interface (OpenAI-backed)
- `app/services/parent_store.py` - SQLite parent chunk storage (configurable db path)
- `app/vectorstore/qdrant_store.py` - Qdrant vector store for child chunks
- `app/graph/extraction.py` - LLM structured entity/relationship extraction
- `app/graph/neo4j_client.py` - Neo4j writer + N-hop traversal
- `app/services/ingestion.py` - orchestrates the full ingest pipeline
- `app/rag/pipeline.py` - LangGraph retrieval pipeline (vector search -> parent
  expansion -> entity linking -> graph traversal -> generation, streaming +
  non-streaming entry points)
- `app/api/routes/health.py` - GET /api/v1/health
- `app/api/routes/ingest.py` - POST /api/v1/ingest (202 + background job),
  GET /api/v1/ingest/{document_id} (status polling)
- `app/api/routes/chat.py` - POST /api/v1/chat, SSE stream of citations / graph
  triples / tokens / done
- `app/api/routes/graph.py` - GET /api/v1/graph/subgraph
- `app/main.py` - FastAPI app factory + lifespan wiring all clients onto
  app.state

## Verified (smoke-tested, no live services required)
- Every `app.*` module imports cleanly.
- Chunker + SQLite parent store round-trip correctly on real text (parent
  chunks ~1000 tokens, child chunks ~200 tokens, matches config).
- Full app boots via `TestClient` with only the two live network calls
  mocked (Qdrant collection check, Neo4j constraint creation): health check
  returns 200, ingest input validation and 404s behave correctly, all 5
  routes resolve in the OpenAPI schema.
- NOT yet tested: an actual ingest -> chat round trip against real
  OpenAI/Qdrant/Neo4j (needs API key + docker-compose).

## Not started yet
- `backend/Dockerfile`
- `docker-compose.yml` (api, ui, neo4j, qdrant) - Neo4j needs
  `NEO4J_PLUGINS=["apoc"]`, since `neo4j_client.py` uses
  `apoc.path.subgraphAll` / `apoc.coll.toSet`
- `frontend/` - chat UI + citations + graph visualizer
- `backend/tests/`
- `README.md` - architecture, setup, sample queries, trade-offs

## Known design trade-offs to call out in the README
- Graph node ids are namespaced per document
  (`f"{document_id}:{entity_name}"` in `extraction.py`), so there is no
  cross-document entity resolution yet -- each document gets its own
  subgraph rather than one merged knowledge graph. Acceptable for this
  scope; would need a real entity-resolution step to fix.
- Entity linking at query time (`find_node_ids_by_name_fragment`) is a
  cheap substring match over all node names, not real NER/linking --
  called out in the code as a take-home shortcut.
