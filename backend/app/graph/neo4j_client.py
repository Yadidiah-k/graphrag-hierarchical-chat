"""Neo4j-backed graph store.

Schema:
    (:Entity {node_id, name, type, normalized_name})
    (:Entity)-[:REL {relation_type, evidence}]->(:Entity)

Provenance is stored on both nodes and relationships as `source_child_ids`
and `source_parent_ids` arrays, so a subgraph result can always be traced
back to the vector chunks it came from.
"""

from __future__ import annotations

from neo4j import GraphDatabase

from app.core.config import Settings
from app.schemas.models import ExtractionResult, SubgraphEdge, SubgraphNode


class Neo4jGraphStore:
    def __init__(self, settings: Settings) -> None:
        self._driver = GraphDatabase.driver(
            settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
        )
        self._hop_depth = settings.graph_hop_depth
        self.ensure_constraints()

    def close(self) -> None:
        self._driver.close()

    def ensure_constraints(self) -> None:
        with self._driver.session() as session:
            session.run(
                "CREATE CONSTRAINT entity_node_id IF NOT EXISTS "
                "FOR (e:Entity) REQUIRE e.node_id IS UNIQUE"
            )
            session.run(
                "CREATE INDEX entity_normalized_name IF NOT EXISTS "
                "FOR (e:Entity) ON (e.normalized_name)"
            )

    def write_extraction(self, result: ExtractionResult) -> None:
        with self._driver.session() as session:
            session.execute_write(self._write_extraction_tx, result)

    @staticmethod
    def _write_extraction_tx(tx, result: ExtractionResult) -> None:
        for node in result.nodes:
            tx.run(
                """
                MERGE (e:Entity {node_id: $node_id})
                ON CREATE SET e.name = $name, e.type = $type,
                              e.normalized_name = $normalized_name,
                              e.source_child_ids = $source_child_ids,
                              e.source_parent_ids = $source_parent_ids
                ON MATCH SET e.source_child_ids = apoc.coll.toSet(
                                coalesce(e.source_child_ids, []) + $source_child_ids),
                             e.source_parent_ids = apoc.coll.toSet(
                                coalesce(e.source_parent_ids, []) + $source_parent_ids)
                """,
                node_id=node.node_id,
                name=node.name,
                type=node.type,
                normalized_name=node.normalized_name,
                source_child_ids=node.source_child_ids,
                source_parent_ids=node.source_parent_ids,
            )

        for rel in result.relationships:
            tx.run(
                """
                MATCH (s:Entity {node_id: $source_id})
                MATCH (t:Entity {node_id: $target_id})
                MERGE (s)-[r:REL {relation_type: $relation_type, evidence: $evidence}]->(t)
                ON CREATE SET r.source_child_ids = $source_child_ids
                """,
                source_id=rel.source_node_id,
                target_id=rel.target_node_id,
                relation_type=rel.relation_type,
                evidence=rel.evidence,
                source_child_ids=rel.source_child_ids,
            )

    def neighborhood(self, node_ids: list[str], hop_depth: int | None = None) -> tuple[list[SubgraphNode], list[SubgraphEdge]]:
        """N-hop traversal outward from a seed set of node ids."""
        if not node_ids:
            return [], []
        depth = hop_depth or self._hop_depth

        query = f"""
        MATCH (start:Entity)
        WHERE start.node_id IN $node_ids
        CALL apoc.path.subgraphAll(start, {{maxLevel: {int(depth)}}})
        YIELD nodes, relationships
        RETURN nodes, relationships
        """
        nodes: dict[str, SubgraphNode] = {}
        edges: list[SubgraphEdge] = []

        with self._driver.session() as session:
            records = session.run(query, node_ids=node_ids)
            for record in records:
                for n in record["nodes"]:
                    nodes[n["node_id"]] = SubgraphNode(
                        id=n["node_id"], label=n["name"], type=n["type"]
                    )
                for r in record["relationships"]:
                    edges.append(
                        SubgraphEdge(
                            source=r.start_node["node_id"],
                            target=r.end_node["node_id"],
                            relation=r["relation_type"],
                            evidence=r.get("evidence", ""),
                        )
                    )

        return list(nodes.values()), edges

    def find_candidates_by_normalized_name(self, normalized_names: list[str]) -> dict[str, tuple[str, list[str]]]:
        """Returns {normalized_name: (existing_node_id, existing_source_parent_ids)}
        for each name that has an existing match -- at most one match per name.
        source_parent_ids is returned so the caller can auto-confirm same-document
        repeats without an LLM call (see EntityResolver.resolve)."""
        if not normalized_names:
            return {}

        query = """
        MATCH (e:Entity)
        WHERE e.normalized_name IN $normalized_names
        RETURN e.normalized_name AS normalized_name, e.node_id AS node_id,
               e.source_parent_ids AS source_parent_ids
        """
        candidates: dict[str, tuple[str, list[str]]] = {}
        with self._driver.session() as session:
            for record in session.run(query, normalized_names=normalized_names):
                name = record["normalized_name"]
                if name in candidates:
                    continue
                candidates[name] = (record["node_id"], record["source_parent_ids"] or [])
        return candidates

    def get_relationship_summary(self, node_id: str, limit: int = 5) -> list[str]:
        """Up to `limit` relationship triples involving this node, as strings
        like 'Acme Corp -[HAS_CEO]-> Jane Smith', for LLM confirmation context."""
        query = """
        MATCH (e:Entity {node_id: $node_id})-[r:REL]-(other:Entity)
        RETURN e.name AS name, r.relation_type AS relation_type,
               other.name AS other_name, startNode(r) = e AS outgoing
        LIMIT $limit
        """
        summaries: list[str] = []
        with self._driver.session() as session:
            for record in session.run(query, node_id=node_id, limit=limit):
                if record["outgoing"]:
                    summaries.append(f"{record['name']} -[{record['relation_type']}]-> {record['other_name']}")
                else:
                    summaries.append(f"{record['other_name']} -[{record['relation_type']}]-> {record['name']}")
        return summaries

    def find_node_ids_by_name_fragment(self, text: str, limit: int = 20) -> list[str]:
        """Cheap entity-linking fallback: match extracted node names that
        appear as substrings of the retrieved text. Good enough for a
        take-home; production would use a real entity-linking step."""
        query = "MATCH (e:Entity) RETURN e.node_id AS node_id, e.name AS name LIMIT 5000"
        matches: list[str] = []
        with self._driver.session() as session:
            for record in session.run(query):
                if record["name"].lower() in text.lower():
                    matches.append(record["node_id"])
                    if len(matches) >= limit:
                        break
        return matches


def build_graph_store(settings: Settings) -> Neo4jGraphStore:
    return Neo4jGraphStore(settings)
