# Fuzzy/similarity-based entity candidate matching

Status: approved, ready for implementation
Date: 2026-08-25

## Context

The last explicit scope boundary from the cross-document entity resolution
work: candidate lookup is exact normalized-name match only, so "Acme Corp"
and "Acme Corporation" are never found as candidates for each other at all
-- not merged, not even considered. This closes that gap.

## Design

**Extend candidate finding, don't touch confirmation.** `EntityResolver`'s
existing flow (candidate lookup -> same-document auto-confirm check ->
batched LLM confirmation for genuinely ambiguous cross-document candidates)
is precision-safe already -- the LLM confirmation step is what prevents
false merges, and it doesn't care whether a candidate arrived via exact or
fuzzy matching. So this change is scoped entirely to *finding more
candidates*, not to how they get confirmed. `EntityResolver.resolve()`'s
logic, the LLM confirmation prompt, and the safe-default-on-malformed-
response behavior are all unchanged.

**Mechanism: APOC string similarity, not a new embedding index.** Neo4j's
APOC library is already enabled in this project (used for
`apoc.path.subgraphAll`, `apoc.coll.toSet`). It has
`apoc.text.sorensenDiceSimilarity(a, b)`, returning 0.0-1.0. Use this
instead of standing up a new entity-embedding pipeline -- no new
infrastructure, no new embedding calls, reuses what's already running.
This is a deliberate choice over the embedding-based option discussed
earlier: cheaper, and the actual precision safeguard (LLM confirmation)
already exists regardless of which candidate-finding mechanism feeds it.

Replace `Neo4jGraphStore.find_candidates_by_normalized_name`'s exact
`WHERE e.normalized_name IN $names` with a similarity-threshold query,
keeping the same method signature/return shape
(`dict[str, tuple[str, list[str]]]`, at most one candidate per name --
this cap is unchanged from the original design, not being revisited here):

```cypher
UNWIND $names AS target_name
MATCH (e:Entity)
WITH target_name, e, apoc.text.sorensenDiceSimilarity(e.normalized_name, target_name) AS similarity
WHERE similarity >= $threshold
WITH target_name, e, similarity
ORDER BY similarity DESC
WITH target_name, collect(e)[0] AS best_match, collect(similarity)[0] AS best_similarity
RETURN target_name, best_match.node_id AS node_id, best_match.source_parent_ids AS source_parent_ids
```

An exact match is similarity 1.0, so this subsumes the old exact-match
behavior -- no separate code path needed for "exact vs. fuzzy."

**Threshold**: add `entity_fuzzy_match_threshold: float = Field(default=0.82)`
to `Settings`, same pattern as the existing tunable parameters
(`retry_top_k_multiplier`, `max_context_retries`). Document in
`.env.example`'s comments (or just the Settings docstring/field -- doesn't
need its own `.env` entry given it's a rarely-tuned constant, matching how
`retry_hop_depth_increment` etc. aren't in `.env.example` either). Pick
0.82 as a reasonable starting point for catching close variants ("Acme
Corp" vs. "Acme Corporation") without matching unrelated short names --
note in a comment that this is a rough, untuned starting value, not a
carefully calibrated one.

**Known limitation, worth a one-line comment where the threshold is
defined**: short normalized names (e.g. two-letter abbreviations) can
produce misleadingly high Dice-similarity scores against unrelated short
strings. Not fixed here -- the LLM confirmation step still catches genuine
false positives, just at the cost of an extra LLM call for spurious
short-name candidates. Fine to leave as a known rough edge, not a
blocker.

## Files touched

Modified only: `backend/app/graph/neo4j_client.py` (candidate query),
`backend/app/core/config.py` (threshold setting),
`backend/app/graph/entity_resolution.py` (pass the threshold through to
the renamed/updated call -- method name can stay
`find_candidates_by_normalized_name` or be renamed to something like
`find_candidates_by_similarity`; implementer's call, not a meaningful
design decision), `backend/tests/test_entity_resolution.py` (extend the
fake graph store / existing tests to cover a fuzzy-match case).

## Verification plan

1. Unit tests (no LLM, no docker): extend `test_entity_resolution.py`'s
   fake graph store to accept a similarity score for "Acme Corp" vs. "Acme
   Corporation" above threshold, and a clearly-unrelated pair below
   threshold, confirming `resolve()` treats the near-miss as a candidate
   (routes it to same-document-check / LLM confirmation) and the unrelated
   pair as no-candidate (untouched).
2. Real Neo4j check (docker, `neo4j` service only, no OpenAI key needed):
   create two Entity nodes, "Acme Corp" and "Acme Corporation", run the new
   Cypher directly, confirm it returns a match with similarity above
   threshold; run it again against a clearly unrelated pair (e.g. "Acme
   Corp" vs. "Bramblewood Foods") and confirm no match.
3. If real-model verification is feasible (check whichever OpenRouter key
   currently has quota before attempting -- don't guess, don't burn a call
   probing a key already known exhausted): a real two-document ingest,
   "Acme Corp" in one, "Acme Corporation" in the other, confirming they
   now get flagged as candidates and go through LLM confirmation (check
   the resulting graph state, same technique as the earlier "Silverline
   Capital" / "Marcus Reed" verification in `PROGRESS.md`). If no quota is
   available, say so plainly and rely on items 1-2.

## Commit plan

1. `config.py` threshold setting
2. `neo4j_client.py` fuzzy candidate query
3. `entity_resolution.py` wiring + test updates
4. `PROGRESS.md` / `IMPROVEMENTS.md` / `README.md` updates

No Claude co-author trailer on any commit.
