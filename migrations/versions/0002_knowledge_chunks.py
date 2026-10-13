"""Application-owned demo knowledge; historical/operational data remain untouched."""

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision="0002_knowledge_chunks"
down_revision="0001_order_review_cases"
branch_labels=None
depends_on=None


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
    op.execute("CREATE SCHEMA knowledge")
    op.execute("REVOKE ALL ON SCHEMA knowledge FROM PUBLIC")
    op.create_table('document_chunks',
        sa.Column('chunk_id',sa.Uuid(),nullable=False),
        sa.Column('document_id',sa.Text(),nullable=False),
        sa.Column('source_name',sa.Text(),nullable=False),
        sa.Column('source_path',sa.Text(),nullable=False),
        sa.Column('title',sa.Text(),nullable=False),
        sa.Column('section',sa.Text(),nullable=False),
        sa.Column('chunk_index',sa.Integer(),nullable=False),
        sa.Column('content',sa.Text(),nullable=False),
        sa.Column('content_hash',sa.String(64),nullable=False),
        sa.Column('embedding_model',sa.Text(),nullable=False),
        sa.Column('embedding_dimensions',sa.Integer(),nullable=False),
        sa.Column('embedding',Vector(1536),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.PrimaryKeyConstraint('chunk_id',name='pk_document_chunks'),
        sa.UniqueConstraint('document_id','chunk_index',name='uq_document_chunk_position'),
        sa.CheckConstraint('embedding_dimensions = 1536',name='ck_embedding_dimensions'),
        sa.CheckConstraint('chunk_index >= 0',name='ck_chunk_index'),schema='knowledge')
    op.create_index('ix_knowledge_document_chunks_document_id','document_chunks',['document_id'],schema='knowledge')


def downgrade():
    op.drop_table('document_chunks',schema='knowledge')
    op.execute('DROP SCHEMA knowledge')
    # The shared vector extension is deliberately retained for other users.
