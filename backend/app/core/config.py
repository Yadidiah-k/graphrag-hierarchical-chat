"""Typed application settings loaded from environment variables."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- LLM provider ---
    # base_url left unset targets api.openai.com; pointing it at an
    # OpenAI-compatible endpoint (e.g. https://openrouter.ai/api/v1) lets
    # openai_api_key hold that provider's key instead, with no code changes.
    openai_api_key: str = Field(default="")
    openai_base_url: str | None = Field(default=None)
    llm_model: str = Field(default="gpt-4o-mini")
    embedding_model: str = Field(default="text-embedding-3-small")
    embedding_dimension: int = Field(default=1536)

    # --- Neo4j ---
    neo4j_uri: str = Field(default="bolt://neo4j:7687")
    neo4j_user: str = Field(default="neo4j")
    neo4j_password: str = Field(default="testpassword")

    # --- Postgres ---
    database_url: str = Field(
        default="postgresql+psycopg://graphrag:graphrag@postgres:5432/graphrag"
    )

    # --- Chunking ---
    parent_chunk_tokens: int = Field(default=1000)
    child_chunk_tokens: int = Field(default=200)
    chunk_overlap_tokens: int = Field(default=50)

    # --- Retrieval ---
    top_k_vector: int = Field(default=8)
    graph_hop_depth: int = Field(default=2)

    # --- Agentic retry (bounded widen-and-retry on insufficient context) ---
    max_context_retries: int = Field(default=1)
    retry_top_k_multiplier: float = Field(default=1.5)
    retry_hop_depth_increment: int = Field(default=1)

    # --- Entity resolution ---
    # Minimum apoc.text.sorensenDiceSimilarity score (0.0-1.0) for an
    # existing entity to be surfaced as a fuzzy-match candidate. The initial
    # 0.82 default turned out not to clear the motivating case this feature
    # was built for -- real apoc.text.sorensenDiceSimilarity('acme_corp',
    # 'acme_corporation') is 0.6956, found empirically while verifying this
    # against real Neo4j. 0.65 is set instead, confirmed via the same
    # real-Neo4j check to actually catch that pair. Still a rough value, not
    # carefully tuned across many name-variant styles, and short normalized
    # names (e.g. two-letter abbreviations) can produce misleadingly high
    # scores against unrelated short strings regardless of threshold; the
    # LLM confirmation step is what catches those, at the cost of an extra
    # call.
    entity_fuzzy_match_threshold: float = Field(default=0.65)

    # --- App ---
    environment: str = Field(default="local")
    log_level: str = Field(default="INFO")


@lru_cache
def get_settings() -> Settings:
    return Settings()
