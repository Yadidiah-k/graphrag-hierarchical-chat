# Progress (in-progress build, not final)

## Done
- `app/core/config.py` - typed settings
- `app/schemas/models.py` - all API/domain Pydantic contracts
- `app/chunking/hierarchical_chunker.py` - parent/child chunker
- `app/services/embeddings.py` - embedding provider interface (OpenAI-backed)
- `app/services/parent_store.py` - SQLite parent chunk storage
- `app/vectorstore/qdrant_store.py` - Qdrant vector store for child chunks
- `app/graph/extraction.py` - LLM structured entity/relationship extraction
- `app/graph/neo4j_client.py` - Neo4j writer + N-hop traversal
- `app/services/ingestion.py` - orchestrates the full ingest pipeline
- `app/rag/pipeline.py` - LangGraph retrieval pipeline (vector search -> parent
  expansion -> entity linking -> graph traversal -> generation, streaming +
  non-streaming entry points)

## Not started yet
- `app/api/routes/` - ingest, chat (SSE), graph/subgraph, health endpoints
- `app/main.py` - FastAPI app factory wiring dependencies
- `backend/Dockerfile`
- `docker-compose.yml` (api, ui, neo4j, qdrant)
- `frontend/` - Streamlit chat + citations + graph visualizer
- `backend/tests/`
- `README.md` - architecture, setup, sample queries, trade-offs
- `requirements.txt` / `pyproject.toml`

## Notes
- Nothing has been run yet (no live Neo4j/Qdrant/OpenAI key available in this
  sandbox) - code is written but not execution-tested. Test locally once
  docker-compose is in place.
