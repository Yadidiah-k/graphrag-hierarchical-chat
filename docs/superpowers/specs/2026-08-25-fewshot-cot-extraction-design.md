# Few-shot + CoT for graph extraction

Status: approved, ready for implementation
Date: 2026-08-25

## Context

`IMPROVEMENTS.md` Priority 4, decided in favor of over DSPy after research
found no suitable existing labeled dataset (DocRED/REBEL both use a fixed,
closed relation taxonomy against our open-ended LLM-generated relation
types, and are Wikipedia-domain rather than business-document domain --
see the Priority 4 entry for the full finding). Hand-written examples were
needed either way, so this skips DSPy's compile-step machinery entirely.

Small, contained, single-file change.

## Design

**CoT, embedded in the same JSON call, not a second call**: add a
`reasoning` field as the *first* key in the requested JSON shape, so the
model writes brief reasoning before committing to the structured
extraction -- within the existing single `response_format=json_object`
call. Matches this project's established pattern of folding new
LLM-driven capability into an existing call rather than adding a second
one (query rewriting into `validate_query`, chunk classification into this
same extraction call originally).

`reasoning` is **not** added to `ExtractionResult` or persisted anywhere --
it's a prompting technique to improve the quality of the fields we already
capture, not new data needed downstream. `_parse` requires no code changes
at all: it already selectively reads known keys via `payload.get(...)` and
ignores unrecognized ones, so an extra `reasoning` key in the LLM's JSON
response is silently ignored by existing logic. The just-written
`test_extraction_parsing.py` suite needs no changes either -- its synthetic
payloads don't include `reasoning` and don't need to.

**Two hand-written few-shot examples**, in-domain (business/acquisition
text, matching what this project actually ingests, not generic examples):
1. Rich signal -- multiple entities, multiple relationships, demonstrates
   the full expected shape.
2. Weak signal -- a passage with no concrete named entities or explicit
   relationships (vague company-history prose), demonstrating restraint:
   empty `nodes`/`relationships` rather than inventing a generic "the
   company" node or a relationship not actually stated. This is the more
   important of the two examples -- it's the failure mode (hallucinated
   entities/relationships) the current prompt has no example guarding
   against.

Both examples use a different company/scenario than the existing
VERIFICATION.md/eval-harness fixture (Acme Corp/Startup Inc), so the model
isn't just pattern-matching a literal example back at test time.

**Cost/latency trade-off, worth stating explicitly**: this prompt is sent
once per parent chunk on every ingestion, so the added examples increase
every extraction call's input token count. Two short examples were chosen
specifically to bound this -- not the "10-20 examples" some few-shot setups
use.

## Files touched

Modified only: `backend/app/graph/extraction.py` (prompt text only, no
signature/logic changes).

## Verification plan

1. `pytest backend/tests/test_extraction_parsing.py` still passes unchanged
   (confirms the `_parse` compatibility claim above is actually true, not
   just asserted).
2. Real-model check against the live stack (OpenRouter config already in
   `.env`): re-run ingestion against the VERIFICATION.md fixture document
   and a second, different short passage that has *no* concrete named
   entities (to specifically exercise the restraint behavior the new
   example demonstrates) -- confirm the extractor does not invent
   entities/relationships for it. Keep this targeted (2 calls, not a full
   eval-harness re-run) given today's OpenRouter free-tier daily rate limit
   was already hit once.
3. Note honestly in `PROGRESS.md`: this improves prompt quality but there's
   no automated before/after quality metric in this pass (the eval harness
   tests chat answer quality, not extraction quality directly) -- the
   verification is a targeted spot-check, not a regression-tested
   improvement.

## Commit plan

One commit: the prompt change, described precisely (what changed and why,
not just "improved prompt").
