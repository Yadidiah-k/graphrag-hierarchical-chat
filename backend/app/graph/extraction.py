"""Extract typed entities and relationships from a parent chunk using
LLM structured output.

Model output is untrusted external input: it is requested as JSON and
validated against `ExtractionResult` before anything is written to Neo4j.
"""

from __future__ import annotations

import json
import re
import uuid

from openai import OpenAI

from app.core.config import Settings
from app.schemas.models import ExtractionResult, GraphNode, GraphRelationship

EXTRACTION_PROMPT = """You extract a knowledge graph from a passage of text.

Rules:
- First, in a "reasoning" field, briefly (1-3 sentences) think through what
  entities and relationships the text actually supports before committing
  to the structured fields below. If the text has no concrete named
  entities or explicit relationships, say so -- do not force an extraction.
- Only extract entities and relationships explicitly supported by the text.
- Do not invent facts that are not stated or clearly implied. A vague
  reference like "the company" is not a named entity -- do not turn it into
  a node.
- Use short, stable, human-readable entity names (e.g. "Acme Corp", not "the company").
- relation_type must be an UPPER_SNAKE_CASE verb phrase (e.g. ACQUIRED, PARTNERED_WITH, EMPLOYS).
- Every relationship must include a short "evidence" string quoting or closely
  paraphrasing the supporting text.
- Also classify the passage itself: a short human-readable section_title
  (e.g. "Q3 Revenue", "Executive Summary"; null if none is apparent), and a
  content_type of exactly one of "prose", "table", "list", "other".

Return ONLY valid JSON matching this shape, with no extra commentary:
{{
  "reasoning": "...",
  "nodes": [{{"name": "...", "type": "..."}}],
  "relationships": [
    {{"source": "...", "target": "...", "relation_type": "...", "evidence": "..."}}
  ],
  "section_title": "..." or null,
  "content_type": "prose" or "table" or "list" or "other"
}}

Example 1 -- rich signal:
Text: "Meridian Logistics, a Chicago-based freight company, announced today
that it has entered a strategic partnership with RouteWise Analytics to
integrate real-time route optimization into its fleet management platform.
The partnership was announced by Meridian's Chief Operating Officer, Elena
Vargas."
Output:
{{
  "reasoning": "The text explicitly states Meridian Logistics partnered with RouteWise Analytics, and that Elena Vargas, described as Meridian's COO, announced it. Both relationships are directly supported.",
  "nodes": [
    {{"name": "Meridian Logistics", "type": "Organization"}},
    {{"name": "RouteWise Analytics", "type": "Organization"}},
    {{"name": "Elena Vargas", "type": "Person"}}
  ],
  "relationships": [
    {{"source": "Meridian Logistics", "target": "RouteWise Analytics", "relation_type": "PARTNERED_WITH", "evidence": "entered a strategic partnership with RouteWise Analytics"}},
    {{"source": "Elena Vargas", "target": "Meridian Logistics", "relation_type": "IS_COO_OF", "evidence": "Meridian's Chief Operating Officer, Elena Vargas"}}
  ],
  "section_title": "Partnership Announcement",
  "content_type": "prose"
}}

Example 2 -- weak signal, correct restraint:
Text: "Founded in 1987, the company has grown steadily over the decades,
weathering multiple economic downturns while maintaining a reputation for
reliability. Employees often describe the culture as collaborative and
mission-driven."
Output:
{{
  "reasoning": "This passage describes company history and culture in vague terms. No specific named entities or concrete relationships are stated -- 'the company' is not a proper name, so there is nothing to extract.",
  "nodes": [],
  "relationships": [],
  "section_title": "Company History",
  "content_type": "prose"
}}

Now extract from this text:
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
            normalized_name = re.sub(r"\s+", "_", name.strip().lower())
            # Provisional id: globally scoped by normalized name rather than
            # document-scoped, so the same real-world entity mentioned across
            # documents can be merged. EntityResolver (app/graph/entity_resolution.py)
            # may rewrite this to an existing node's id before the graph write.
            node_id = f"{normalized_name}:{uuid.uuid4().hex[:8]}"
            name_to_id[name] = node_id
            nodes.append(
                GraphNode(
                    node_id=node_id,
                    name=name,
                    type=node_type,
                    normalized_name=normalized_name,
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
