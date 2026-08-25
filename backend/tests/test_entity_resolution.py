"""Unit tests for EntityResolver -- pure logic with a fake graph store and a
monkeypatched LLM confirmation call, so these run without docker, Neo4j, or
a real API key (a dummy key is enough since the OpenAI client is only ever
constructed here, never actually called over the network in this file).

Covers the two behaviors the design doc calls out explicitly:
- same-document repeat mentions auto-confirm without spending an LLM call
- a malformed/missing LLM response defaults every decision to
  same_as_existing=False (a missed merge, not a wrongful one)
"""

from __future__ import annotations

import json

from app.core.config import Settings
from app.graph.entity_resolution import EntityResolver
from app.schemas.models import ExtractionResult, GraphNode, GraphRelationship


class FakeGraphStore:
    """Duck-typed stand-in for Neo4jGraphStore's two EntityResolver-facing methods."""

    def __init__(self, candidates: dict, relationship_summaries: dict | None = None) -> None:
        self._candidates = candidates
        self._summaries = relationship_summaries or {}

    def find_candidates_by_normalized_name(self, normalized_names: list[str]) -> dict:
        return {name: self._candidates[name] for name in normalized_names if name in self._candidates}

    def get_relationship_summary(self, node_id: str, limit: int = 5) -> list[str]:
        return self._summaries.get(node_id, [])


def _resolver(graph_store) -> EntityResolver:
    settings = Settings(openai_api_key="test-key")
    return EntityResolver(graph_store, settings)


def _extraction_with_one_node(node_id: str, name: str, normalized_name: str) -> ExtractionResult:
    return ExtractionResult(
        nodes=[
            GraphNode(
                node_id=node_id,
                name=name,
                type="Organization",
                normalized_name=normalized_name,
                source_child_ids=["doc-2:p0:aaaaaaaa:c0:bbbbbbbb"],
                source_parent_ids=["doc-2:p0:aaaaaaaa"],
            )
        ],
        relationships=[],
    )


class TestParseDecisions:
    def test_well_formed_json(self) -> None:
        raw = json.dumps(
            {"decisions": [{"entity_name": "Acme Corp", "same_as_existing": True, "reasoning": "same entity"}]}
        )
        result = EntityResolver._parse_decisions(raw)
        assert result == {"Acme Corp": True}

    def test_malformed_json_returns_empty_dict(self) -> None:
        result = EntityResolver._parse_decisions("not json {{{")
        assert result == {}

    def test_missing_decisions_key_returns_empty_dict(self) -> None:
        result = EntityResolver._parse_decisions("{}")
        assert result == {}

    def test_missing_same_as_existing_defaults_to_false(self) -> None:
        raw = json.dumps({"decisions": [{"entity_name": "Acme Corp"}]})
        result = EntityResolver._parse_decisions(raw)
        assert result == {"Acme Corp": False}

    def test_non_boolean_same_as_existing_falsy_value_defaults_to_false(self) -> None:
        raw = json.dumps({"decisions": [{"entity_name": "Acme Corp", "same_as_existing": None}]})
        result = EntityResolver._parse_decisions(raw)
        assert result == {"Acme Corp": False}

    def test_entry_missing_entity_name_is_skipped(self) -> None:
        raw = json.dumps(
            {
                "decisions": [
                    {"same_as_existing": True},
                    {"entity_name": "Acme Corp", "same_as_existing": True},
                ]
            }
        )
        result = EntityResolver._parse_decisions(raw)
        assert result == {"Acme Corp": True}

    def test_non_dict_entry_is_skipped(self) -> None:
        raw = json.dumps({"decisions": ["not a dict"]})
        result = EntityResolver._parse_decisions(raw)
        assert result == {}


class TestResolveNoCandidates:
    def test_no_matching_candidate_leaves_node_id_unchanged(self) -> None:
        extraction = _extraction_with_one_node("acme_corp:11112222", "Acme Corp", "acme_corp")
        resolver = _resolver(FakeGraphStore(candidates={}))
        result = resolver.resolve(extraction, document_id="doc-2", parent_text="Acme Corp did things.")
        assert result.nodes[0].node_id == "acme_corp:11112222"

    def test_empty_extraction_is_returned_unchanged(self) -> None:
        extraction = ExtractionResult(nodes=[], relationships=[])
        resolver = _resolver(FakeGraphStore(candidates={}))
        result = resolver.resolve(extraction, document_id="doc-2", parent_text="text")
        assert result.nodes == []
        assert result.relationships == []


class TestResolveAutoConfirm:
    def test_same_document_repeat_mention_auto_confirms_without_llm_call(self) -> None:
        extraction = _extraction_with_one_node("acme_corp:33334444", "Acme Corp", "acme_corp")
        candidates = {"acme_corp": ("acme_corp:00001111", ["doc-2:p0:aaaaaaaa"])}
        resolver = _resolver(FakeGraphStore(candidates=candidates))

        def _fail_if_called(*args, **kwargs):
            raise AssertionError("LLM confirmation should not be called for a same-document repeat")

        resolver._confirm_via_llm = _fail_if_called  # type: ignore[method-assign]

        result = resolver.resolve(extraction, document_id="doc-2", parent_text="Acme Corp did more things.")
        assert result.nodes[0].node_id == "acme_corp:00001111"

    def test_relationships_referencing_renamed_node_ids_are_rewritten(self) -> None:
        node_a = GraphNode(
            node_id="acme_corp:33334444",
            name="Acme Corp",
            type="Organization",
            normalized_name="acme_corp",
            source_parent_ids=["doc-2:p0:aaaaaaaa"],
        )
        node_b = GraphNode(
            node_id="startup_inc:55556666",
            name="Startup Inc",
            type="Organization",
            normalized_name="startup_inc",
            source_parent_ids=["doc-2:p0:aaaaaaaa"],
        )
        relationship = GraphRelationship(
            source_node_id="acme_corp:33334444",
            target_node_id="startup_inc:55556666",
            relation_type="ACQUIRED",
            evidence="Acme Corp acquired Startup Inc",
        )
        extraction = ExtractionResult(nodes=[node_a, node_b], relationships=[relationship])
        candidates = {"acme_corp": ("acme_corp:00001111", ["doc-2:p0:aaaaaaaa"])}
        resolver = _resolver(FakeGraphStore(candidates=candidates))

        result = resolver.resolve(extraction, document_id="doc-2", parent_text="text")

        assert result.nodes[0].node_id == "acme_corp:00001111"
        assert result.nodes[1].node_id == "startup_inc:55556666"
        assert result.relationships[0].source_node_id == "acme_corp:00001111"
        assert result.relationships[0].target_node_id == "startup_inc:55556666"


class TestResolveCrossDocumentLlmConfirmation:
    def test_cross_document_candidate_confirmed_same_reuses_existing_id(self) -> None:
        extraction = _extraction_with_one_node("acme_corp:77778888", "Acme Corp", "acme_corp")
        candidates = {"acme_corp": ("acme_corp:00001111", ["doc-1:p0:aaaaaaaa"])}
        resolver = _resolver(FakeGraphStore(candidates=candidates))
        resolver._confirm_via_llm = lambda parent_text, ambiguous: {"Acme Corp": True}  # type: ignore[method-assign]

        result = resolver.resolve(extraction, document_id="doc-2", parent_text="Acme Corp announced earnings.")
        assert result.nodes[0].node_id == "acme_corp:00001111"

    def test_cross_document_candidate_confirmed_different_keeps_new_id(self) -> None:
        extraction = _extraction_with_one_node("john_smith:77778888", "John Smith", "john_smith")
        candidates = {"john_smith": ("john_smith:00001111", ["doc-1:p0:aaaaaaaa"])}
        resolver = _resolver(FakeGraphStore(candidates=candidates))
        resolver._confirm_via_llm = lambda parent_text, ambiguous: {"John Smith": False}  # type: ignore[method-assign]

        result = resolver.resolve(extraction, document_id="doc-2", parent_text="A different John Smith spoke today.")
        assert result.nodes[0].node_id == "john_smith:77778888"

    def test_malformed_llm_response_defaults_to_keeping_new_id(self) -> None:
        """End-to-end through resolve(): if _confirm_via_llm's parsing ever
        returns an empty dict (the malformed-JSON case), a candidate with no
        entry defaults to not-confirmed, so the provisional id survives."""
        extraction = _extraction_with_one_node("acme_corp:99990000", "Acme Corp", "acme_corp")
        candidates = {"acme_corp": ("acme_corp:00001111", ["doc-1:p0:aaaaaaaa"])}
        resolver = _resolver(FakeGraphStore(candidates=candidates))
        resolver._confirm_via_llm = lambda parent_text, ambiguous: EntityResolver._parse_decisions("not json {{{")

        result = resolver.resolve(extraction, document_id="doc-2", parent_text="text")
        assert result.nodes[0].node_id == "acme_corp:99990000"
