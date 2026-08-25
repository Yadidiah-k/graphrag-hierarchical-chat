"""Multi-hop GraphRAG pipeline built as a LangGraph state machine.

Nodes:
    retrieve_vector  -> child-chunk vector search
    expand_parents   -> child hits -> full parent chunk text
    link_entities    -> cheap entity linking over retrieved parent text
    traverse_graph   -> N-hop Neo4j neighborhood from linked entities
    generate_answer  -> build a grounded prompt and stream the LLM response

The graph is intentionally a straight-line pipeline (not branching/looping)
for this scope, but using LangGraph gives durable, inspectable state at each
step and a natural place to add branching (e.g. re-retrieval on low
confidence) later.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TypedDict

from langgraph.graph import END, StateGraph
from openai import OpenAI

from app.core.config import Settings
from app.graph.neo4j_client import Neo4jGraphStore
from app.schemas.models import CitationChunk, CitationTriple
from app.services.embeddings import EmbeddingProvider
from app.services.parent_store import ParentChunkStore
from app.vectorstore.postgres_store import PostgresVectorStore

GENERATION_PROMPT = """You answer questions using ONLY the context provided below.
If the context does not contain the answer, say so plainly instead of guessing.

Retrieved document context:
{parent_context}

Relevant relationships from the knowledge graph:
{graph_context}

Question: {question}

Answer using only the information above. Where relevant, refer to entities
and relationships by name.
"""


class RagState(TypedDict, total=False):
    query: str
    document_id: str | None
    top_k: int
    vector_hits: list  # list[VectorHit]
    parent_texts: dict[str, str]
    citations: list[CitationChunk]
    linked_node_ids: list[str]
    graph_nodes: list
    graph_edges: list
    triples: list[CitationTriple]
    answer: str


class GraphRagPipeline:
    def __init__(
        self,
        settings: Settings,
        embedding_provider: EmbeddingProvider,
        vector_store: PostgresVectorStore,
        parent_store: ParentChunkStore,
        graph_store: Neo4jGraphStore,
    ) -> None:
        self._settings = settings
        self._embeddings = embedding_provider
        self._vectors = vector_store
        self._parents = parent_store
        self._graph = graph_store
        self._llm = OpenAI(api_key=settings.openai_api_key)
        self._app = self._build_graph()

    # ------------------------------------------------------------------
    # Graph nodes
    # ------------------------------------------------------------------

    def _retrieve_vector(self, state: RagState) -> RagState:
        query_vector = self._embeddings.embed_query(state["query"])
        hits = self._vectors.search(
            query_vector=query_vector,
            top_k=state.get("top_k") or self._settings.top_k_vector,
            document_id=state.get("document_id"),
        )
        return {"vector_hits": hits}

    def _expand_parents(self, state: RagState) -> RagState:
        hits = state.get("vector_hits", [])
        parent_ids = list({h.parent_id for h in hits})
        parents = self._parents.get_many(parent_ids)

        parent_texts = {pid: p.text for pid, p in parents.items()}
        citations = [
            CitationChunk(
                parent_id=h.parent_id,
                document_id=h.document_id,
                text=parents[h.parent_id].text if h.parent_id in parents else h.text,
                score=h.score,
            )
            for h in hits
        ]
        return {"parent_texts": parent_texts, "citations": citations}

    def _link_entities(self, state: RagState) -> RagState:
        combined_text = "\n".join(state.get("parent_texts", {}).values())
        node_ids = self._graph.find_node_ids_by_name_fragment(combined_text)
        return {"linked_node_ids": node_ids}

    def _traverse_graph(self, state: RagState) -> RagState:
        node_ids = state.get("linked_node_ids", [])
        nodes, edges = self._graph.neighborhood(node_ids)
        triples = [
            CitationTriple(
                source=self._label_for(nodes, e.source),
                relation=e.relation,
                target=self._label_for(nodes, e.target),
                evidence=e.evidence,
            )
            for e in edges
        ]
        return {"graph_nodes": nodes, "graph_edges": edges, "triples": triples}

    def _generate_answer(self, state: RagState) -> RagState:
        parent_context = "\n\n---\n\n".join(state.get("parent_texts", {}).values()) or "(no matching text found)"
        graph_context = (
            "\n".join(f"({t.source})-[{t.relation}]->({t.target}) :: {t.evidence}" for t in state.get("triples", []))
            or "(no graph relationships found)"
        )
        prompt = GENERATION_PROMPT.format(
            parent_context=parent_context,
            graph_context=graph_context,
            question=state["query"],
        )
        response = self._llm.chat.completions.create(
            model=self._settings.llm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
        )
        return {"answer": response.choices[0].message.content or ""}

    @staticmethod
    def _label_for(nodes, node_id: str) -> str:
        for n in nodes:
            if n.id == node_id:
                return n.label
        return node_id

    # ------------------------------------------------------------------
    # Graph wiring
    # ------------------------------------------------------------------

    def _build_graph(self):
        graph = StateGraph(RagState)
        graph.add_node("retrieve_vector", self._retrieve_vector)
        graph.add_node("expand_parents", self._expand_parents)
        graph.add_node("link_entities", self._link_entities)
        graph.add_node("traverse_graph", self._traverse_graph)
        graph.add_node("generate_answer", self._generate_answer)

        graph.set_entry_point("retrieve_vector")
        graph.add_edge("retrieve_vector", "expand_parents")
        graph.add_edge("expand_parents", "link_entities")
        graph.add_edge("link_entities", "traverse_graph")
        graph.add_edge("traverse_graph", "generate_answer")
        graph.add_edge("generate_answer", END)

        return graph.compile()

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def answer(self, query: str, document_id: str | None = None, top_k: int | None = None) -> RagState:
        """Non-streaming entry point -- runs the full graph and returns final state."""
        initial: RagState = {"query": query, "document_id": document_id, "top_k": top_k or self._settings.top_k_vector}
        return self._app.invoke(initial)

    def answer_stream(
        self, query: str, document_id: str | None = None, top_k: int | None = None
    ) -> tuple[Iterator[str], list[CitationChunk], list[CitationTriple], list[str]]:
        """Runs retrieval/graph steps eagerly (cheap, needed for grounding),
        then streams only the generation step token-by-token for the API layer."""
        state: RagState = {"query": query, "document_id": document_id, "top_k": top_k or self._settings.top_k_vector}
        state = {**state, **self._retrieve_vector(state)}
        state = {**state, **self._expand_parents(state)}
        state = {**state, **self._link_entities(state)}
        state = {**state, **self._traverse_graph(state)}

        parent_context = "\n\n---\n\n".join(state.get("parent_texts", {}).values()) or "(no matching text found)"
        graph_context = (
            "\n".join(f"({t.source})-[{t.relation}]->({t.target}) :: {t.evidence}" for t in state.get("triples", []))
            or "(no graph relationships found)"
        )
        prompt = GENERATION_PROMPT.format(
            parent_context=parent_context,
            graph_context=graph_context,
            question=query,
        )

        def token_stream() -> Iterator[str]:
            stream = self._llm.chat.completions.create(
                model=self._settings.llm_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                stream=True,
            )
            for chunk in stream:
                delta = chunk.choices[0].delta.content
                if delta:
                    yield delta

        return (
            token_stream(),
            state.get("citations", []),
            state.get("triples", []),
            state.get("linked_node_ids", []),
        )


def build_rag_pipeline(
    settings: Settings,
    embedding_provider: EmbeddingProvider,
    vector_store: PostgresVectorStore,
    parent_store: ParentChunkStore,
    graph_store: Neo4jGraphStore,
) -> GraphRagPipeline:
    return GraphRagPipeline(settings, embedding_provider, vector_store, parent_store, graph_store)
