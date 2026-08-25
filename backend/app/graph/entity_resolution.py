"""Cross-document entity resolution: similarity-based candidate lookup,
then -- only for genuinely ambiguous cross-document candidates -- a single
batched LLM confirmation call per parent chunk, so two entities that merely
share a name (e.g. two unrelated "John Smith"s) aren't silently fused into
one graph node just because a name matched.
"""

from __future__ import annotations

import json

from openai import OpenAI

from app.core.config import Settings
from app.graph.neo4j_client import Neo4jGraphStore
from app.schemas.models import ExtractionResult, GraphNode

RESOLUTION_PROMPT = """You are resolving whether newly mentioned entities are the \
same real-world entity as existing ones already recorded in a knowledge graph.

New mention -- the passage these entities were just extracted from:
\"\"\"
{parent_text}
\"\"\"

For each candidate below, decide whether the newly mentioned entity is the SAME
real-world entity as the existing one, using the new mention's context and what
is already known about the existing entity. Two different people/organizations
that merely happen to share a name are NOT the same -- only confirm a match if
the context actually supports it.

Candidates:
{candidates_block}

Return ONLY valid JSON matching this shape, with no extra commentary:
{{
  "decisions": [
    {{"entity_name": "...", "same_as_existing": true/false, "reasoning": "..."}}
  ]
}}
"""


class EntityResolver:
    def __init__(self, graph_store: Neo4jGraphStore, settings: Settings) -> None:
        self._graph = graph_store
        self._client = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
        self._model = settings.llm_model
        self._fuzzy_match_threshold = settings.entity_fuzzy_match_threshold

    def resolve(self, extraction: ExtractionResult, document_id: str, parent_text: str) -> ExtractionResult:
        """Rewrites provisional node ids in extraction.nodes/relationships to
        reuse existing node ids where confirmed, leaves them as-is otherwise."""
        if not extraction.nodes:
            return extraction

        normalized_names = list({node.normalized_name for node in extraction.nodes if node.normalized_name})
        candidates = self._graph.find_candidates_by_similarity(normalized_names, self._fuzzy_match_threshold)
        if not candidates:
            return extraction

        rename_map: dict[str, str] = {}
        ambiguous: list[tuple[GraphNode, str]] = []

        for node in extraction.nodes:
            match = candidates.get(node.normalized_name)
            if not match:
                continue
            existing_node_id, existing_source_parent_ids = match
            # Same-document repeat mention: the old document-scoped id scheme
            # already merged this for free, so re-confirming it via an LLM
            # call would be spending a call on something that isn't actually
            # ambiguous. Without this, ingesting a single document that
            # mentions an entity across multiple parent chunks would newly
            # cost an LLM call per repeat, which it didn't before.
            if any(pid.startswith(f"{document_id}:") for pid in existing_source_parent_ids):
                rename_map[node.node_id] = existing_node_id
            else:
                ambiguous.append((node, existing_node_id))

        if ambiguous:
            decisions = self._confirm_via_llm(parent_text, ambiguous)
            for node, existing_node_id in ambiguous:
                if decisions.get(node.name, False):
                    rename_map[node.node_id] = existing_node_id

        if not rename_map:
            return extraction

        for node in extraction.nodes:
            if node.node_id in rename_map:
                node.node_id = rename_map[node.node_id]

        for rel in extraction.relationships:
            if rel.source_node_id in rename_map:
                rel.source_node_id = rename_map[rel.source_node_id]
            if rel.target_node_id in rename_map:
                rel.target_node_id = rename_map[rel.target_node_id]

        return extraction

    def _confirm_via_llm(self, parent_text: str, ambiguous: list[tuple[GraphNode, str]]) -> dict[str, bool]:
        candidates_block = "\n".join(
            f'- "{node.name}" (type: {node.type}). Already known about the existing entity: '
            f"{'; '.join(self._graph.get_relationship_summary(existing_id)) or '(nothing else known yet)'}"
            for node, existing_id in ambiguous
        )
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": "You output only valid JSON. No markdown fences."},
                {
                    "role": "user",
                    "content": RESOLUTION_PROMPT.format(parent_text=parent_text, candidates_block=candidates_block),
                },
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content or "{}"
        return self._parse_decisions(raw)

    @staticmethod
    def _parse_decisions(raw: str) -> dict[str, bool]:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}

        decisions: dict[str, bool] = {}
        for raw_decision in payload.get("decisions", []) or []:
            if not isinstance(raw_decision, dict):
                continue
            entity_name = str(raw_decision.get("entity_name", "")).strip()
            if not entity_name:
                continue
            # Safe default on a malformed/missing same_as_existing: False. A
            # missed merge (two nodes stay separate) is a smaller problem
            # than a wrongful one (two unrelated entities silently fused),
            # matching this codebase's existing defensive-parsing style
            # (see GraphRagPipeline._parse_grade in app/rag/pipeline.py).
            decisions[entity_name] = bool(raw_decision.get("same_as_existing", False))
        return decisions


def build_entity_resolver(graph_store: Neo4jGraphStore, settings: Settings) -> EntityResolver:
    return EntityResolver(graph_store, settings)
