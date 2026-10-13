"""Knowledge metadata remains separate from Olist and operations metadata."""

from datetime import datetime
from uuid import UUID
from pgvector.sqlalchemy import Vector
from sqlalchemy import CheckConstraint, DateTime, Integer, String, Text, Uuid, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from enterprise_ai_ops_agent.knowledge.contracts import DIMENSIONS


class KnowledgeBase(DeclarativeBase):
    pass


class DocumentChunk(KnowledgeBase):
    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_document_chunk_position"),
        CheckConstraint("embedding_dimensions = 1536", name="ck_embedding_dimensions"),
        CheckConstraint("chunk_index >= 0", name="ck_chunk_index"),
        {"schema": "knowledge"},
    )
    chunk_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    document_id: Mapped[str] = mapped_column(Text, index=True)
    source_name: Mapped[str] = mapped_column(Text)
    source_path: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    section: Mapped[str] = mapped_column(Text)
    chunk_index: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    embedding_model: Mapped[str] = mapped_column(Text)
    embedding_dimensions: Mapped[int] = mapped_column(Integer)
    embedding: Mapped[list[float]] = mapped_column(Vector(DIMENSIONS))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
