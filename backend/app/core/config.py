"""Typed application settings loaded from environment variables."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- LLM provider ---
    openai_api_key: str = Field(default="")
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

    # --- App ---
    environment: str = Field(default="local")
    log_level: str = Field(default="INFO")


@lru_cache
def get_settings() -> Settings:
    return Settings()
