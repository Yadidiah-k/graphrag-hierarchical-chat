"""Agentic multi-hop GraphRAG pipeline built as a LangGraph state machine.

Nodes:
    validate_query    -> structured check for gibberish, tone, verbosity
                          preference, and a history-resolved rewritten query
    retrieve_vector    -> child-chunk vector search
    expand_parents     -> child hits -> full parent chunk text
    link_entities      -> cheap entity linking over retrieved parent text
    traverse_graph     -> N-hop Neo4j neighborhood from linked entities
    grade_context      -> structured sufficiency check; insufficient context
                          triggers a bounded retry with widened top_k/hop_depth
    generate_answer    -> build a grounded prompt and stream the LLM response
    generate_rationale -> structured explanation of which chunks/relationships
                          were actually used, given the final answer text
    reject_response    -> no LLM call; surfaces validate_query's suggested_reply

`answer()` runs the fully compiled LangGraph (`self._app`), including the
`add_conditional_edges` branches (gibberish short-circuit, grade_context's
retry cycle back into retrieve_vector) via `.invoke()`.

`answer_stream()` -- what `/chat` actually calls -- keeps the pre-existing
pattern of manually chaining node functions in Python rather than driving
the compiled graph, because it needs to hand back a live token generator for
`generate_answer` instead of a finished state dict. The bounded retry is a
plain Python loop here, not the graph engine's cycle machinery. It does not
run `generate_rationale`: that needs the full answer text, which only exists
once the caller has consumed the token stream, so it is exposed as a
separate `generate_rationale()` method for the caller to invoke afterward.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TypedDict

from langgraph.graph import END, StateGraph
from openai import OpenAI

from app.core.config import Settings
from app.graph.neo4j_client import Neo4jGraphStore
from app.schemas.models import (
    ChatMessage,
    CitationChunk,
    CitationTriple,
    ContextGrade,
    QueryValidation,
    Rationale,
)
from app.services.embeddings import EmbeddingProvider
from app.services.parent_store import ParentChunkStore
from app.vectorstore.postgres_store import PostgresVectorStore

VALIDATION_PROMPT = """You are the query-understanding step of a RAG system.

Conversation history (most recent last):
{history}

Current user query: "{query}"

Tasks:
1. Decide if the query is gibberish / nonsensical / not an actual question
   or request (is_gibberish).
2. Infer the tone of the query (e.g. "neutral", "frustrated", "curious",
   "urgent").
3. Infer a verbosity preference: "brief" if the user seems to want a short,
   direct answer, "detailed" otherwise.
4. Rewrite the query into a fully self-contained standalone question,
   resolving any references to the conversation history (e.g. "what about
   their 2023 revenue?" -> "What was Acme Corp's 2023 revenue?"). If there is
   no history or nothing to resolve, rewritten_query is just the original
   query, cleaned up.
5. Only if is_gibberish is true, write a short, friendly suggested_reply
   asking the user to clarify or rephrase. Otherwise omit it.

Return ONLY valid JSON matching this shape, with no extra commentary:
{{
  "is_gibberish": true/false,
  "tone": "...",
  "verbosity_preference": "brief" or "detailed",
  "rewritten_query": "...",
  "suggested_reply": "..." or null
}}
"""

GRADE_PROMPT = """You grade whether retrieved context is sufficient to answer a question.

Question: {question}

Retrieved document context:
{parent_context}

Relevant relationships from the knowledge graph:
{graph_context}

Decide whether this context is sufficient to answer the question accurately
and completely, a relevance score from 0.0 to 1.0, and -- if insufficient --
what information is missing.

Return ONLY valid JSON matching this shape, with no extra commentary:
{{
  "is_sufficient": true/false,
  "relevance_score": 0.0-1.0,
  "missing_info": "..." or null
}}
"""

GENERATION_PROMPT = """You answer questions using ONLY the context provided below.
If the context does not contain the answer, say so plainly instead of guessing.

Conversation so far:
{history_block}

Style: {style_instruction}

Retrieved document context:
{parent_context}

Relevant relationships from the knowledge graph:
{graph_context}

Question: {question}

{hedge_instruction}

Answer using only the information above. Where relevant, refer to entities
and relationships by name.
"""

RATIONALE_PROMPT = """You explain which retrieved evidence was actually used to produce an answer.

Question: {query}

Answer given:
{answer}

Available citations (parent_id -> text):
{citations}

Available graph relationships:
{triples}

Identify which citations (by parent_id) and which relationships were
actually drawn on to produce the answer above. Leave out anything that
wasn't actually used.

Return ONLY valid JSON matching this shape, with no extra commentary:
{{
  "explanation": "...",
  "chunks_used": ["parent_id", ...],
  "relationships_used": ["source -> relation -> target", ...]
}}
"""


class RagState(TypedDict, total=False):
    query: str
    history: list[ChatMessage]
    document_id: str | None
    section_title_filter: str | None
    content_type_filter: str | None
    top_k: int
    hop_depth: int
    retry_count: int
    should_retry: bool
    is_gibberish: bool
    tone: str
    verbosity_preference: str
    rewritten_query: str
    suggested_reply: str | None
    vector_hits: list  # list[VectorHit]
    parent_texts: dict[str, str]
    citations: list[CitationChunk]
    linked_node_ids: list[str]
    graph_nodes: list
    graph_edges: list
    triples: list[CitationTriple]
    is_sufficient: bool
    relevance_score: float
    missing_info: str | None
    answer: str
    rationale: Rationale


@dataclass
class StreamResult:
    token_iter: Iterator[str]
    citations: list[CitationChunk]
    triples: list[CitationTriple]
    linked_node_ids: list[str]
    is_gibberish: bool
    rewritten_query: str


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
        self._llm = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
        self._app = self._build_graph()

    # ------------------------------------------------------------------
    # Graph nodes
    # ------------------------------------------------------------------

    def _validate_query(self, state: RagState) -> RagState:
        history = state.get("history", [])
        response = self._llm.chat.completions.create(
            model=self._settings.llm_model,
            messages=[
                {"role": "system", "content": "You output only valid JSON. No markdown fences."},
                {
                    "role": "user",
                    "content": VALIDATION_PROMPT.format(
                        history=self._format_history(history),
                        query=state["query"],
                    ),
                },
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content or "{}"
        validation = self._parse_validation(raw, state["query"])
        return {
            "is_gibberish": validation.is_gibberish,
            "tone": validation.tone,
            "verbosity_preference": validation.verbosity_preference,
            "rewritten_query": validation.rewritten_query,
            "suggested_reply": validation.suggested_reply,
        }

    def _reject_response(self, state: RagState) -> RagState:
        return {"answer": state.get("suggested_reply") or "I couldn't understand that query. Could you rephrase it?"}

    def _retrieve_vector(self, state: RagState) -> RagState:
        query_vector = self._embeddings.embed_query(state.get("rewritten_query") or state["query"])
        hits = self._vectors.search(
            query_vector=query_vector,
            top_k=state.get("top_k") or self._settings.top_k_vector,
            document_id=state.get("document_id"),
            section_title=state.get("section_title_filter"),
            content_type=state.get("content_type_filter"),
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
        nodes, edges = self._graph.neighborhood(node_ids, hop_depth=state.get("hop_depth"))
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

    def _grade_context(self, state: RagState) -> RagState:
        parent_context, graph_context = self._render_contexts(state)
        response = self._llm.chat.completions.create(
            model=self._settings.llm_model,
            messages=[
                {"role": "system", "content": "You output only valid JSON. No markdown fences."},
                {
                    "role": "user",
                    "content": GRADE_PROMPT.format(
                        question=state.get("rewritten_query") or state["query"],
                        parent_context=parent_context,
                        graph_context=graph_context,
                    ),
                },
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content or "{}"
        grade = self._parse_grade(raw)

        retry_count = state.get("retry_count", 0)
        should_retry = not grade.is_sufficient and retry_count < self._settings.max_context_retries
        updates: RagState = {
            "is_sufficient": grade.is_sufficient,
            "relevance_score": grade.relevance_score,
            "missing_info": grade.missing_info,
            "should_retry": should_retry,
        }
        if should_retry:
            current_top_k = state.get("top_k") or self._settings.top_k_vector
            current_hop_depth = state.get("hop_depth") or self._settings.graph_hop_depth
            updates["retry_count"] = retry_count + 1
            updates["top_k"] = max(1, round(current_top_k * self._settings.retry_top_k_multiplier))
            updates["hop_depth"] = current_hop_depth + self._settings.retry_hop_depth_increment
        else:
            updates["retry_count"] = retry_count
        return updates

    def _generate_answer(self, state: RagState) -> RagState:
        prompt = self._build_generation_prompt(state)
        response = self._llm.chat.completions.create(
            model=self._settings.llm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
        )
        return {"answer": response.choices[0].message.content or ""}

    def _generate_rationale_node(self, state: RagState) -> RagState:
        rationale = self.generate_rationale(
            query=state.get("rewritten_query") or state["query"],
            answer=state.get("answer", ""),
            citations=state.get("citations", []),
            triples=state.get("triples", []),
        )
        return {"rationale": rationale}

    @staticmethod
    def _label_for(nodes, node_id: str) -> str:
        for n in nodes:
            if n.id == node_id:
                return n.label
        return node_id

    # ------------------------------------------------------------------
    # Prompt / parsing helpers shared by nodes and the streaming path
    # ------------------------------------------------------------------

    @staticmethod
    def _render_contexts(state: RagState) -> tuple[str, str]:
        parent_context = "\n\n---\n\n".join(state.get("parent_texts", {}).values()) or "(no matching text found)"
        graph_context = (
            "\n".join(f"({t.source})-[{t.relation}]->({t.target}) :: {t.evidence}" for t in state.get("triples", []))
            or "(no graph relationships found)"
        )
        return parent_context, graph_context

    @staticmethod
    def _format_history(history: list[ChatMessage], limit: int = 6) -> str:
        if not history:
            return "(no prior conversation)"
        recent = history[-limit:]
        return "\n".join(f"{m.role}: {m.content}" for m in recent)

    @staticmethod
    def _style_instruction(tone: str | None, verbosity_preference: str | None) -> str:
        tone = tone or "neutral"
        length_hint = (
            "Keep the answer brief and to the point."
            if verbosity_preference == "brief"
            else "Provide a thorough, detailed answer."
        )
        return f"Match a {tone} tone. {length_hint}"

    def _build_generation_prompt(self, state: RagState) -> str:
        parent_context, graph_context = self._render_contexts(state)
        style_instruction = self._style_instruction(state.get("tone"), state.get("verbosity_preference"))
        history_block = self._format_history(state.get("history", []))

        hedge_instruction = "(none)"
        if state.get("is_sufficient") is False and state.get("retry_count", 0) >= self._settings.max_context_retries:
            hedge_instruction = (
                "The retrieved context may still be incomplete after exhausting retrieval retries "
                f"({state.get('missing_info') or 'unspecified gaps'}). Hedge rather than answering "
                "confidently from incomplete context -- acknowledge the gap explicitly."
            )

        return GENERATION_PROMPT.format(
            history_block=history_block,
            style_instruction=style_instruction,
            parent_context=parent_context,
            graph_context=graph_context,
            question=state.get("rewritten_query") or state["query"],
            hedge_instruction=hedge_instruction,
        )

    @staticmethod
    def _parse_validation(raw: str, original_query: str) -> QueryValidation:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}

        is_gibberish = bool(payload.get("is_gibberish", False))
        tone = str(payload.get("tone") or "neutral").strip() or "neutral"
        verbosity_raw = str(payload.get("verbosity_preference") or "detailed").strip().lower()
        verbosity_preference = verbosity_raw if verbosity_raw in ("brief", "detailed") else "detailed"
        rewritten_query = str(payload.get("rewritten_query") or original_query).strip() or original_query
        suggested_reply_raw = payload.get("suggested_reply")
        suggested_reply = str(suggested_reply_raw).strip() if suggested_reply_raw else None

        return QueryValidation(
            is_gibberish=is_gibberish,
            tone=tone,
            verbosity_preference=verbosity_preference,
            rewritten_query=rewritten_query,
            suggested_reply=suggested_reply,
        )

    @staticmethod
    def _parse_grade(raw: str) -> ContextGrade:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}

        is_sufficient = bool(payload.get("is_sufficient", True))
        try:
            relevance_score = float(payload.get("relevance_score", 0.5))
        except (TypeError, ValueError):
            relevance_score = 0.5
        relevance_score = max(0.0, min(1.0, relevance_score))
        missing_info_raw = payload.get("missing_info")
        missing_info = str(missing_info_raw).strip() if missing_info_raw else None

        return ContextGrade(is_sufficient=is_sufficient, relevance_score=relevance_score, missing_info=missing_info)

    @staticmethod
    def _parse_rationale(raw: str) -> Rationale:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}

        explanation = str(payload.get("explanation") or "").strip()
        chunks_used = [str(c).strip() for c in payload.get("chunks_used", []) or [] if str(c).strip()]
        relationships_used = [
            str(r).strip() for r in payload.get("relationships_used", []) or [] if str(r).strip()
        ]

        return Rationale(explanation=explanation, chunks_used=chunks_used, relationships_used=relationships_used)

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    @staticmethod
    def _route_after_validate(state: RagState) -> str:
        return "reject_response" if state.get("is_gibberish") else "retrieve_vector"

    @staticmethod
    def _route_after_grade(state: RagState) -> str:
        return "retrieve_vector" if state.get("should_retry") else "generate_answer"

    # ------------------------------------------------------------------
    # Graph wiring
    # ------------------------------------------------------------------

    def _build_graph(self):
        graph = StateGraph(RagState)
        graph.add_node("validate_query", self._validate_query)
        graph.add_node("reject_response", self._reject_response)
        graph.add_node("retrieve_vector", self._retrieve_vector)
        graph.add_node("expand_parents", self._expand_parents)
        graph.add_node("link_entities", self._link_entities)
        graph.add_node("traverse_graph", self._traverse_graph)
        graph.add_node("grade_context", self._grade_context)
        graph.add_node("generate_answer", self._generate_answer)
        graph.add_node("generate_rationale", self._generate_rationale_node)

        graph.set_entry_point("validate_query")
        graph.add_conditional_edges(
            "validate_query",
            self._route_after_validate,
            {"reject_response": "reject_response", "retrieve_vector": "retrieve_vector"},
        )
        graph.add_edge("reject_response", END)
        graph.add_edge("retrieve_vector", "expand_parents")
        graph.add_edge("expand_parents", "link_entities")
        graph.add_edge("link_entities", "traverse_graph")
        graph.add_edge("traverse_graph", "grade_context")
        graph.add_conditional_edges(
            "grade_context",
            self._route_after_grade,
            {"retrieve_vector": "retrieve_vector", "generate_answer": "generate_answer"},
        )
        graph.add_edge("generate_answer", "generate_rationale")
        graph.add_edge("generate_rationale", END)

        return graph.compile()

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def answer(
        self,
        query: str,
        document_id: str | None = None,
        top_k: int | None = None,
        history: list[ChatMessage] | None = None,
        section_title_filter: str | None = None,
        content_type_filter: str | None = None,
    ) -> RagState:
        """Non-streaming entry point -- runs the full compiled graph and returns final state."""
        initial: RagState = {
            "query": query,
            "document_id": document_id,
            "section_title_filter": section_title_filter,
            "content_type_filter": content_type_filter,
            "top_k": top_k or self._settings.top_k_vector,
            "hop_depth": self._settings.graph_hop_depth,
            "retry_count": 0,
            "history": history or [],
        }
        return self._app.invoke(initial)

    def answer_stream(
        self,
        query: str,
        document_id: str | None = None,
        top_k: int | None = None,
        history: list[ChatMessage] | None = None,
        section_title_filter: str | None = None,
        content_type_filter: str | None = None,
    ) -> StreamResult:
        """Manually chains the same nodes the compiled graph uses, up through
        grade_context (bounded retry as a plain Python loop), then returns a
        live token generator for generate_answer -- there's no streaming
        benefit to driving this through the graph engine, and this entry
        point already deviated from `.invoke()` before this pipeline had
        conditional edges at all."""
        state: RagState = {
            "query": query,
            "document_id": document_id,
            "section_title_filter": section_title_filter,
            "content_type_filter": content_type_filter,
            "top_k": top_k or self._settings.top_k_vector,
            "hop_depth": self._settings.graph_hop_depth,
            "retry_count": 0,
            "history": history or [],
        }
        state = {**state, **self._validate_query(state)}

        if state.get("is_gibberish"):
            suggested_reply = state.get("suggested_reply") or "I couldn't understand that query. Could you rephrase it?"

            def reject_stream() -> Iterator[str]:
                yield suggested_reply

            return StreamResult(
                token_iter=reject_stream(),
                citations=[],
                triples=[],
                linked_node_ids=[],
                is_gibberish=True,
                rewritten_query=state.get("rewritten_query", query),
            )

        state = {**state, **self._retrieve_vector(state)}
        state = {**state, **self._expand_parents(state)}
        state = {**state, **self._link_entities(state)}
        state = {**state, **self._traverse_graph(state)}
        state = {**state, **self._grade_context(state)}

        while state.get("should_retry"):
            state = {**state, **self._retrieve_vector(state)}
            state = {**state, **self._expand_parents(state)}
            state = {**state, **self._link_entities(state)}
            state = {**state, **self._traverse_graph(state)}
            state = {**state, **self._grade_context(state)}

        prompt = self._build_generation_prompt(state)

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

        return StreamResult(
            token_iter=token_stream(),
            citations=state.get("citations", []),
            triples=state.get("triples", []),
            linked_node_ids=state.get("linked_node_ids", []),
            is_gibberish=False,
            rewritten_query=state.get("rewritten_query", query),
        )

    def generate_rationale(
        self,
        query: str,
        answer: str,
        citations: list[CitationChunk],
        triples: list[CitationTriple],
    ) -> Rationale:
        """Called by the streaming path (chat.py) after the token stream has
        been fully consumed -- the rationale needs the complete answer text,
        which doesn't exist until then. Also used internally by the compiled
        graph's generate_rationale node for the non-streaming answer()."""
        citations_block = "\n".join(f"[{c.parent_id}] {c.text}" for c in citations) or "(no citations)"
        triples_block = (
            "\n".join(f"({t.source})-[{t.relation}]->({t.target}) :: {t.evidence}" for t in triples)
            or "(no relationships)"
        )
        response = self._llm.chat.completions.create(
            model=self._settings.llm_model,
            messages=[
                {"role": "system", "content": "You output only valid JSON. No markdown fences."},
                {
                    "role": "user",
                    "content": RATIONALE_PROMPT.format(
                        query=query,
                        answer=answer,
                        citations=citations_block,
                        triples=triples_block,
                    ),
                },
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content or "{}"
        return self._parse_rationale(raw)


def build_rag_pipeline(
    settings: Settings,
    embedding_provider: EmbeddingProvider,
    vector_store: PostgresVectorStore,
    parent_store: ParentChunkStore,
    graph_store: Neo4jGraphStore,
) -> GraphRagPipeline:
    return GraphRagPipeline(settings, embedding_provider, vector_store, parent_store, graph_store)
