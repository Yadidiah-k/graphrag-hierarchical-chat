# Cross-document entity resolution (exact-match candidates + LLM confirmation)

Status: approved, ready for implementation
Date: 2026-08-25

## Context

`IMPROVEMENTS.md`'s last open item. Graph node ids are currently namespaced
per document (`f"{document_id}:{name}"` in `app/graph/extraction.py`), so
"Acme Corp" mentioned in two different ingested documents becomes two
disconnected Neo4j nodes -- the graph never actually connects information
about the same real-world entity across a corpus, which undercuts the core
GraphRAG pitch. This combines two of the options discussed in that review:
drop the per-document node id scheme (exact normalized-name matching), and
add an LLM confirmation step before merging two exact-name matches, so two
different real-world entities that happen to share a name (e.g. two
unrelated people both named "John Smith" across two documents) don't get
silently fused into one node.

## Goals

- Node ids are no longer document-scoped. A newly extracted entity whose
  normalized name has no existing match in the graph gets a fresh globally
  unique id.
- When a normalized-name match *does* exist, one batched LLM call per
  parent chunk (not per entity) confirms or denies each ambiguous entity
  against its candidate before deciding whether to merge.
- Confirmed matches reuse the existing node id (provenance --
  `source_child_ids`/`source_parent_ids` -- accumulates via the graph
  store's existing `MERGE ... ON MATCH SET` union logic, which needs no
  changes).
- Confirmed-different entities get their own new id despite the name
  collision.

## Non-goals

- No fuzzy/similarity-based candidate search ("Acme Corp" vs. "Acme
  Corporation" still won't be found as candidates for each other -- this is
  a real, deliberate scope boundary, not an oversight: it needs a
  similarity-search mechanism (embeddings or fuzzy string matching) that
  wasn't part of what was decided here. Document this explicitly in the
  README as an open gap.
- No handling of more than one existing "confirmed different" entity
  sharing a normalized name -- candidate lookup returns at most one match
  per name. Real multi-candidate disambiguation is out of scope.
- `hierarchical_chunker.py` is untouched (not relevant to this change).

## Design

### Node id generation changes (`app/graph/extraction.py`)

`_parse` currently builds `node_id = f"{document_id}:{name.lower().replace(' ', '_')}"`.
Change to a globally-scoped, document-independent id:
```python
normalized_name = re.sub(r"\s+", "_", name.strip().lower())
node_id = f"{normalized_name}:{uuid.uuid4().hex[:8]}"
```
This is a **provisional** id -- `EntityResolver` (below) may rewrite it to
an existing node's id before the graph write happens. Add a `normalized_name`
field to `GraphNode` (`app/schemas/models.py`) so it flows through to the
Neo4j write and can be indexed for candidate lookup.

The `document_id` parameter on `GraphExtractor.extract`/`_parse` becomes
unused for id generation specifically -- fine to leave the parameter as-is
(avoids signature churn in `ingestion.py`) or remove it if it's cleaner;
implementer's call, not a meaningful design decision either way.

**Existing test to update, not just leave alone**: `test_extraction_parsing.py`'s
`test_node_id_is_namespaced_by_document_and_lowercased` pins the *old*
behavior (`"doc-1:acme_corp"`). This behavior is deliberately changing --
rewrite that test to assert the new scheme (normalized-name prefix, no
`document_id`), don't just delete it.

### Neo4j: candidate lookup + relationship-context retrieval (`app/graph/neo4j_client.py`)

Add `normalized_name` as an indexed Entity property (set on node creation,
alongside the existing `ensure_constraints()`):
```cypher
CREATE INDEX entity_normalized_name IF NOT EXISTS FOR (e:Entity) ON (e.normalized_name)
```

Two new methods on `Neo4jGraphStore`:
```python
def find_candidates_by_normalized_name(self, normalized_names: list[str]) -> dict[str, tuple[str, list[str]]]:
    """Returns {normalized_name: (existing_node_id, existing_source_parent_ids)}
    for each name that has an existing match -- at most one match per name.
    source_parent_ids is returned so the caller can auto-confirm same-document
    repeats without an LLM call (see below)."""

def get_relationship_summary(self, node_id: str, limit: int = 5) -> list[str]:
    """Up to `limit` relationship triples involving this node, as strings
    like 'Acme Corp -[HAS_CEO]-> Jane Smith', for LLM confirmation context.
    Reuse/adapt the existing neighborhood()-style traversal rather than
    writing new Cypher from scratch if it fits."""
```

### New module: `app/graph/entity_resolution.py`

```python
class EntityResolver:
    def __init__(self, graph_store: Neo4jGraphStore, settings: Settings): ...

    def resolve(self, extraction: ExtractionResult, document_id: str, parent_text: str) -> ExtractionResult:
        """Rewrites provisional node ids in extraction.nodes/relationships
        to reuse existing node ids where confirmed, leaves them as-is
        otherwise. Called once per parent chunk, between extraction and the
        graph write."""
```

Flow inside `resolve()`:
1. Batch-lookup all of `extraction.nodes`' normalized names via
   `find_candidates_by_normalized_name`.
2. For each candidate found: **auto-confirm without an LLM call** if the
   existing entity's `source_parent_ids` already contains an id prefixed
   with this ingestion's `document_id` -- this is a same-document repeat
   mention (e.g. "Acme Corp" mentioned in both parent chunk 1 and parent
   chunk 2 of the *same* document), which the old document-scoped id
   already merged for free. Don't spend an LLM call re-confirming something
   that's not actually ambiguous. This matters: without it, ingesting a
   single document that mentions an entity in multiple parent chunks would
   newly cost an LLM call per repeat, which it didn't before.
3. For genuinely cross-document candidates (the ones auto-confirm doesn't
   cover): one batched LLM call, all of them together, giving the model
   `parent_text` (the new mention's context) and, per candidate,
   `get_relationship_summary(existing_node_id)` (what's already known about
   it). Structured output (`response_format=json_object` + manual parse,
   same pattern as everywhere else in this codebase):
   ```json
   {"decisions": [{"entity_name": "...", "same_as_existing": true, "reasoning": "..."}]}
   ```
   **Safe default on malformed/missing response**: `same_as_existing=False`
   -- a missed merge is a smaller problem than a wrongful one. State this
   explicitly as a deliberate choice, matching this codebase's existing
   defensive-parsing style (see `_parse_grade`'s pattern in `rag/pipeline.py`
   for the style to follow).
4. Build a rename map (`provisional_id -> existing_node_id`) from
   auto-confirmed + LLM-confirmed matches. Rewrite matched nodes' `node_id`
   field in place (don't drop them from `extraction.nodes` -- they still
   need to be written via `MERGE` so their `source_child_ids`/
   `source_parent_ids` pick up this document's provenance) and rewrite any
   relationship's `source_node_id`/`target_node_id` that references a
   renamed id.

### Wiring into ingestion (`app/services/ingestion.py`)

In the existing per-parent extraction loop, between `extract()` and
`write_extraction()`:
```python
extraction = self._extractor.extract(parent_id=..., document_id=document_id, child_ids=child_ids, text=parent.text)
extraction = self._resolver.resolve(extraction, document_id, parent.text)
if extraction.nodes:
    self._graph.write_extraction(extraction)
    ...
```

`main.py` needs to construct an `EntityResolver` and pass it into
`IngestionService`.

## Files touched

New: `backend/app/graph/entity_resolution.py`.

Modified: `backend/app/graph/extraction.py` (node id generation),
`backend/app/graph/neo4j_client.py` (index + two new methods),
`backend/app/schemas/models.py` (`GraphNode.normalized_name`),
`backend/app/services/ingestion.py` (wiring), `backend/app/main.py`
(construct + wire `EntityResolver`),
`backend/tests/test_extraction_parsing.py` (update the one test pinning
the old id scheme).

## Verification plan

Given today's OpenRouter account-wide daily rate limit was already hit
twice (see `PROGRESS.md`), real-model verification of the LLM confirmation
step may be blocked depending on when this runs -- **check the quota
reset time first** (`PROGRESS.md` has the last known `X-RateLimit-Reset`
value). If still exhausted, do not skip verification entirely -- fall back
to what doesn't need it and say plainly what couldn't be checked, the same
honest pattern used for the few-shot/CoT pass earlier today.

What to verify regardless of quota:
1. `pytest backend/tests/` -- including the updated
   `test_extraction_parsing.py` test, plus new unit tests for
   `EntityResolver`'s parsing logic (malformed/missing LLM response ->
   `same_as_existing=False` default) using synthetic data, no LLM call.
2. Direct store-level test against real Postgres+Neo4j (docker), bypassing
   OpenAI: manually construct two `ExtractionResult`s with the same entity
   name under two different `document_id`s, call `resolve()` with a
   stubbed/mocked LLM response (both "same" and "different" cases), confirm
   via Neo4j query that the resulting graph has one node (same case) or two
   (different case), and that relationships correctly reference the
   resolved id.
3. If the OpenRouter quota allows: a real two-document ingest test (e.g.
   two short documents both mentioning "Acme Corp") through the actual
   live API, confirming a real LLM confirmation call happens and the graph
   ends up connected across both documents. If quota is exhausted, state
   this plainly in `PROGRESS.md` rather than skipping the section.

## Commit plan

1. Schema change (`GraphNode.normalized_name`) + `extraction.py` node id
   generation + the updated test
2. `neo4j_client.py` (index + two new methods)
3. `entity_resolution.py` (new module)
4. `ingestion.py` + `main.py` wiring
5. `PROGRESS.md` update

No Claude co-author trailer on any commit.
