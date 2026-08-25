"""Extract typed entities and relationships from a parent chunk using
LLM structured output.

Model output is untrusted external input: it is requested as JSON and
validated against `ExtractionResult` before anything is written to Neo4j.
"""

from __future__ import annotations

import json

from openai import OpenAI

from app.core.config import Settings
from app.schemas.models import ExtractionResult, GraphNode, GraphRelationship

EXTRACTION_PROMPT = """You extract a knowledge graph from a passage of text.

Rules:
- Only extract entities and relationships explicitly supported by the text.
- Do not invent facts that are not stated or clearly implied.
- Use short, stable, human-readable entity names (e.g. "Acme Corp", not "the company").
- relation_type must be an UPPER_SNAKE_CASE verb phrase (e.g. ACQUIRED, PARTNERED_WITH, EMPLOYS).
- Every relationship must include a short "evidence" string quoting or closely
  paraphrasing the supporting text.
- Also classify the passage itself: a short human-readable section_title
  (e.g. "Q3 Revenue", "Executive Summary"; null if none is apparent), and a
  content_type of exactly one of "prose", "table", "list", "other".

Return ONLY valid JSON matching this shape, with no extra commentary:
{{
  "nodes": [{{"name": "...", "type": "..."}}],
  "relationships": [
    {{"source": "...", "target": "...", "relation_type": "...", "evidence": "..."}}
  ],
  "section_title": "..." or null,
  "content_type": "prose" or "table" or "list" or "other"
}}

Text:
\"\"\"
{text}
\"\"\"
"""


class GraphExtractor:
    def __init__(self, settings: Settings) -> None:
        self._client = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
        self._model = settings.llm_model

    def extract(self, parent_id: str, document_id: str, child_ids: list[str], text: str) -> ExtractionResult:
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": "You output only valid JSON. No markdown fences."},
                {"role": "user", "content": EXTRACTION_PROMPT.format(text=text)},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content or "{}"
        return self._parse(raw, parent_id, document_id, child_ids)

    @staticmethod
    def _parse(raw: str, parent_id: str, document_id: str, child_ids: list[str]) -> ExtractionResult:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return ExtractionResult(nodes=[], relationships=[], section_title=None, content_type="prose")

        name_to_id: dict[str, str] = {}
        nodes: list[GraphNode] = []
        for raw_node in payload.get("nodes", []):
            name = str(raw_node.get("name", "")).strip()
            node_type = str(raw_node.get("type", "Entity")).strip() or "Entity"
            if not name:
                continue
            node_id = f"{document_id}:{name.lower().replace(' ', '_')}"
            name_to_id[name] = node_id
            nodes.append(
                GraphNode(
                    node_id=node_id,
                    name=name,
                    type=node_type,
                    source_child_ids=child_ids,
                    source_parent_ids=[parent_id],
                )
            )

        relationships: list[GraphRelationship] = []
        for raw_rel in payload.get("relationships", []):
            source_name = str(raw_rel.get("source", "")).strip()
            target_name = str(raw_rel.get("target", "")).strip()
            relation_type = str(raw_rel.get("relation_type", "")).strip().upper().replace(" ", "_")
            evidence = str(raw_rel.get("evidence", "")).strip()

            source_id = name_to_id.get(source_name)
            target_id = name_to_id.get(target_name)
            if not (source_id and target_id and relation_type):
                # Reject relationships that reference entities we did not
                # also extract as nodes -- keeps the graph internally consistent.
                continue

            relationships.append(
                GraphRelationship(
                    source_node_id=source_id,
                    target_node_id=target_id,
                    relation_type=relation_type,
                    evidence=evidence,
                    source_child_ids=child_ids,
                )
            )

        section_title_raw = payload.get("section_title")
        section_title = str(section_title_raw).strip() if section_title_raw else None

        content_type_raw = str(payload.get("content_type", "")).strip().lower()
        content_type = content_type_raw if content_type_raw in ("prose", "table", "list", "other") else "prose"

        return ExtractionResult(
            nodes=nodes,
            relationships=relationships,
            section_title=section_title,
            content_type=content_type,
        )


def build_graph_extractor(settings: Settings) -> GraphExtractor:
    return GraphExtractor(settings)
