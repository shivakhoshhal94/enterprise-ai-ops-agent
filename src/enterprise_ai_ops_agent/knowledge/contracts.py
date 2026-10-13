"""Source-aware chunks and ranked retrieval results; no vectors in public output."""

from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator

DIMENSIONS = 1536
MAX_CHUNK_CHARS = 1200


class KnowledgeError(RuntimeError):
    """Safe knowledge-layer failure without credentials/provider details."""


class KnowledgeChunk(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    chunk_id: UUID
    document_id: str
    source_name: str
    source_path: str
    title: str
    section: str
    chunk_index: int = Field(ge=0)
    content: str = Field(min_length=1, max_length=MAX_CHUNK_CHARS)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=50)
    @field_validator("query")
    @classmethod
    def nonblank(cls, value):
        if not value.strip(): raise ValueError("Query must be nonblank.")
        return value


class KnowledgeHit(KnowledgeChunk):
    rank: int = Field(ge=1)
    distance: float = Field(ge=0, le=2, allow_inf_nan=False)
    similarity: float = Field(ge=-1, le=1, allow_inf_nan=False)


class KnowledgeSearchResult(BaseModel):
    query: str
    embedding_model: str
    hits: list[KnowledgeHit]
    relevance_policy: str = "Application-owned demo documentation, not official Olist policies. Ranked cosine distances only; confidence is not calibrated. Policy text is not authoritative for transactional numbers."


class IngestionSummary(BaseModel):
    documents: int
    chunks: int
    embedded: int
    reused: int
    inserted: int
    removed: int
