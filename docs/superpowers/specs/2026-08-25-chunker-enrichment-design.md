# Chunker enrichment: section/content-type classification + retrieval filters

Status: approved, ready for implementation
Date: 2026-08-25

## Context

`IMPROVEMENTS.md` Priority 2's remaining item: chunks currently carry no
structural metadata beyond `{document_id, parent_id, order, start_char,
end_char}`. This adds LLM-classified `section_title`/`content_type` per
parent chunk (inherited by its children) and wires it up as an actual
query-time retrieval filter, not just stored-and-unused metadata.

Assumes the Postgres migration and agentic pipeline specs are already
implemented and committed (both are, as of this spec).

## Goals

- Classify each parent chunk's `section_title` and `content_type`
  (`prose`/`table`/`list`/`other`) via LLM, folded into the existing
  per-parent extraction call rather than a second call.
- Persist that metadata into the `metadata` JSONB column both tables
  already have (added by the Postgres migration, unused until now).
- Add `section_title_filter`/`content_type_filter` to `/chat`, threaded
  through to the Postgres vector search's `WHERE` clause.
- Add matching filter controls to the frontend sidebar.

## Non-goals

- `hierarchical_chunker.py` itself does not change. It stays a pure,
  dependency-free text splitter -- classification is an ingestion-pipeline
  concern, not a chunking concern. This is deliberate: it's the one module
  in this codebase fully unit-testable with zero mocking, and that
  property is worth preserving.
- No per-child classification. Parents (~1000 tokens) are classified once;
  children inherit their parent's metadata. Classifying every child
  individually would be ~5x more LLM calls (children outnumber parents
  roughly 5:1 in the existing chunker's default sizing) for marginal
  precision gain -- a deliberate cost/latency trade-off, not an oversight.
  Document this in the README's trade-offs section when that gets written.
- `CitationChunk` / `VectorHit` do not gain a `metadata` field -- this
  pass is about filtering, not about surfacing section/content-type in
  citation display. That's a reasonable small follow-up, not bundled here.

## Design

### Classification: folded into the existing extraction call

`GraphExtractor.extract()` already makes one LLM call per parent chunk
(entity/relationship extraction, `app/graph/extraction.py`). Extend that
same prompt/response to also return `section_title` and `content_type`,
rather than adding a second per-parent LLM call. `ExtractionResult` grows
two fields that aren't strictly "extraction" in the entity/relationship
sense -- a minor naming tension, acceptable given the alternative (a
second call, doubling ingestion LLM cost) is worse.

```python
class ExtractionResult(BaseModel):
    nodes: list[GraphNode]
    relationships: list[GraphRelationship]
    section_title: str | None = None
    content_type: Literal["prose", "table", "list", "other"] = "prose"
```

Extend `EXTRACTION_PROMPT` to ask for these two fields in the same JSON
response shape, and `GraphExtractor._parse` to pull them out with the same
defensive-default parsing style already used for nodes/relationships
(malformed/missing -> `section_title=None`, `content_type="prose"`).

### Ingestion: metadata lands via UPDATE after extraction, not at initial insert

**Important: do not reorder `ingestion.py` to classify before saving
chunks.** The current order is `save_document -> chunk -> save_many
(parents) -> embed+upsert (children) -> loop parents: extract ->
write_graph`. Extraction is the most failure-prone step (external LLM
call, can hit rate limits/timeouts/parsing errors) and today a failure
there still leaves chunks and vectors durably saved -- only graph
enrichment is lost. Moving classification earlier so it lands in the
initial `INSERT` would mean an extraction failure blocks *even the
chunk/vector storage*, a strictly worse failure mode. Instead:

Keep the existing order exactly as-is. After each parent's `extract()`
call (already inside the existing loop), call two new store methods to
backfill the metadata:

```python
metadata = {"section_title": extraction.section_title, "content_type": extraction.content_type}
self._parents.update_metadata(parent.parent_id, metadata)
if child_ids:
    self._vectors.update_children_metadata(child_ids, metadata)
```

This is a plain `UPDATE ... WHERE`, not part of the original `INSERT` --
two extra small round-trips per parent, in exchange for not touching the
existing partial-success failure semantics.

### Storage layer

`app/services/parent_store.py` -- new method:
```python
def update_metadata(self, parent_id: str, metadata: dict) -> None:
    stmt = update(ParentChunkORM).where(ParentChunkORM.parent_id == parent_id).values(metadata_=metadata)
    with self._session_factory() as session:
        session.execute(stmt)
        session.commit()
```

`app/vectorstore/postgres_store.py` -- new method:
```python
def update_children_metadata(self, child_ids: list[str], metadata: dict) -> None:
    stmt = update(ChildChunkORM).where(ChildChunkORM.child_id.in_(child_ids)).values(metadata_=metadata)
    with self._session_factory() as session:
        session.execute(stmt)
        session.commit()
```

Note the `metadata_` attribute name in both (SQLAlchemy reserves
`metadata` on declarative models -- same gotcha the Postgres migration
already worked around; see `ParentChunkORM`/`ChildChunkORM` in
`app/db/models.py`).

`search()` gains two optional filter params, using JSONB `astext` for
equality on the metadata fields:
```python
def search(
    self,
    query_vector: list[float],
    top_k: int = 8,
    document_id: str | None = None,
    section_title: str | None = None,
    content_type: str | None = None,
) -> list[VectorHit]:
    ...
    if section_title:
        query = query.filter(ChildChunkORM.metadata_["section_title"].astext == section_title)
    if content_type:
        query = query.filter(ChildChunkORM.metadata_["content_type"].astext == content_type)
```

### Pipeline / API

`schemas/models.py`:
```python
class ChatRequest(BaseModel):
    query: str
    document_id: str | None = None
    top_k: int | None = None
    history: list[ChatMessage] = Field(default_factory=list)
    section_title_filter: str | None = None
    content_type_filter: Literal["prose", "table", "list", "other"] | None = None

class ParentChunk(BaseModel):
    ...  # existing fields unchanged
    metadata: dict = Field(default_factory=dict)

class ChildChunk(BaseModel):
    ...  # existing fields unchanged
    metadata: dict = Field(default_factory=dict)
```

`rag/pipeline.py`: `RagState` gains `section_title_filter`/
`content_type_filter`; `answer()` and `answer_stream()` both gain the two
params in their signatures, included in the initial state dict, and
`_retrieve_vector` passes them into `self._vectors.search(...)`.

`api/routes/chat.py`: passes `payload.section_title_filter` /
`payload.content_type_filter` through to `pipeline.answer_stream(...)`.

### Frontend

`frontend/app.py`, in the chat tab's sidebar, alongside the existing
`document_id` filter:
```python
content_type_filter = st.sidebar.selectbox(
    "Content type filter", options=[None, "prose", "table", "list", "other"],
    format_func=lambda v: v or "(any)",
)
section_title_filter = st.sidebar.text_input("Section title filter (optional)")
```
Passed into `stream_chat(...)` and the request payload the same way
`document_filter`/`top_k` already are.

## Files touched

Modified only, no new/deleted files: `backend/app/schemas/models.py`,
`backend/app/graph/extraction.py`, `backend/app/services/parent_store.py`,
`backend/app/vectorstore/postgres_store.py`,
`backend/app/services/ingestion.py`, `backend/app/rag/pipeline.py`,
`backend/app/api/routes/chat.py`, `frontend/app.py`.

## Verification plan

Same fake-`OPENAI_API_KEY` constraint as prior passes, and it bites in a
specific way here worth being upfront about: with a fake key, `POST
/api/v1/ingest` still fails at the *embedding* call (before the extraction
loop even runs, same failure point as always) -- so the real `/ingest` API
path never reaches `update_metadata`/`update_children_metadata` in this
verification pass. The only way to verify the metadata backfill and
filtered search actually work is a direct store-level test that bypasses
OpenAI entirely (construct `ExtractionResult`-shaped metadata directly,
skip the LLM call), same technique used to verify the Postgres migration's
stores. Be explicit about this rather than implying the API-level test
covers it:

1. Every `app.*` module imports cleanly.
2. Unit test `GraphExtractor._parse` against synthetic JSON with
   `section_title`/`content_type` present, missing, and malformed --
   confirm defaults (`None`/`"prose"`) apply correctly.
3. Direct store-level test against a real local Postgres (bypassing
   OpenAI): insert a parent + children, call `update_metadata` /
   `update_children_metadata` with synthetic classification data, confirm
   via raw SQL that `metadata` landed correctly, then call `search()` with
   `section_title`/`content_type` filters set and confirm only matching
   rows come back (and that omitting the filters still returns everything,
   i.e. the filters are genuinely optional).
4. `docker compose up` -- still boots, all services healthy.
5. `POST /api/v1/ingest` with the fake key -> still fails at the embedding
   call, same as every prior pass (proves this change didn't alter the
   existing failure point).
6. `GET /api/v1/graph/subgraph` -- still 200, unaffected.
7. Explicitly note in `PROGRESS.md`: classification accuracy/quality is
   untested against a real model, and the metadata backfill + filtered
   search are only verified at the store level, not through a real
   `/ingest` -> `/chat` round trip (blocked by the same fake-key
   limitation as every prior pass).

## Commit plan

1. Schema changes (`ParentChunk.metadata`, `ChildChunk.metadata`,
   `ExtractionResult` fields, `ChatRequest` filter fields)
2. `graph/extraction.py` prompt + parsing update
3. `services/parent_store.py` + `vectorstore/postgres_store.py`
   (`update_metadata`, `update_children_metadata`, `search()` filters)
4. `services/ingestion.py` wiring (backfill calls after each extraction)
5. `rag/pipeline.py` + `api/routes/chat.py` filter threading
6. `frontend/app.py` filter controls
7. `PROGRESS.md` update

No Claude co-author trailer on any commit.
