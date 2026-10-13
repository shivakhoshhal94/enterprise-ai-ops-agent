"""Explicit corpus synchronization and read-only, parameterized pgvector retrieval."""

from datetime import datetime, timezone
from sqlalchemy import delete, insert, select, text, update
from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.knowledge.contracts import DIMENSIONS, IngestionSummary, KnowledgeChunk, KnowledgeError, KnowledgeHit, KnowledgeSearchResult, SearchRequest
from enterprise_ai_ops_agent.knowledge.embeddings import embed_texts
from enterprise_ai_ops_agent.knowledge.models import DocumentChunk
from enterprise_ai_ops_agent.knowledge.session import KNOWLEDGE_ROLE


def require_embedding_profile(rows, settings: Settings):
    if any(row['embedding_model']!=settings.openai_embedding_model or row['embedding_dimensions']!=DIMENSIONS for row in rows):
        raise KnowledgeError("Stored embedding profile differs; changing a populated corpus requires an explicit re-embedding plan.")


def synchronize_knowledge(engine, chunks: list[KnowledgeChunk], *, client, settings: Settings) -> IngestionSummary:
    """The explicit nonempty source root is authoritative; removed chunks are deleted."""
    if not chunks or len({item.chunk_id for item in chunks})!=len(chunks):
        raise KnowledgeError("Corpus must contain unique, nonempty chunks.")
    table=DocumentChunk.__table__
    try:
        with engine.begin() as connection:
            if engine.dialect.name=='postgresql':
                connection.execute(text('SELECT pg_advisory_xact_lock(907001)'))
            existing=list(connection.execute(select(table)).mappings())
            require_embedding_profile(existing,settings)
            cached={row['content_hash']:row['embedding'] for row in existing}
            missing={item.content_hash:item.content for item in chunks if item.content_hash not in cached}
            hashes=list(missing)
            for offset in range(0,len(hashes),64):
                batch=hashes[offset:offset+64]
                cached.update(zip(batch,embed_texts(client,[missing[key] for key in batch],settings),strict=True))
            wanted={item.chunk_id for item in chunks}
            stale={row['chunk_id'] for row in existing}-wanted
            if stale: connection.execute(delete(table).where(table.c.chunk_id.in_(stale)))
            by_id={row['chunk_id']:row for row in existing}
            inserted=[]
            for item in chunks:
                payload=item.model_dump()
                if item.chunk_id not in by_id:
                    inserted.append({**payload,'embedding':cached[item.content_hash],
                        'embedding_model':settings.openai_embedding_model,'embedding_dimensions':DIMENSIONS,
                        'created_at':datetime.now(timezone.utc)})
                elif any(by_id[item.chunk_id][key]!=value for key,value in payload.items()):
                    connection.execute(update(table).where(table.c.chunk_id==item.chunk_id).values(**payload))
            if inserted: connection.execute(insert(table),inserted)
        return IngestionSummary(documents=len({item.document_id for item in chunks}),chunks=len(chunks),
            embedded=len(missing),reused=len(chunks)-len(missing),inserted=len(inserted),removed=len(stale))
    except KnowledgeError: raise
    except Exception:
        raise KnowledgeError("Knowledge synchronization failed; database changes rolled back.") from None


def search_company_knowledge(query: str, *, engine, client, settings: Settings, top_k: int = 5) -> KnowledgeSearchResult:
    try: request=SearchRequest(query=query,top_k=top_k)
    except ValueError: raise KnowledgeError("Knowledge query must be nonblank/bounded and top_k between 1 and 50.") from None
    if engine.dialect.name=='postgresql' and engine.url.username!=KNOWLEDGE_ROLE:
        raise KnowledgeError("Runtime retrieval requires the dedicated knowledge reader, not an administrative engine.")
    table=DocumentChunk.__table__
    try:
        with engine.connect().execution_options(postgresql_readonly=True) as connection:
            if engine.dialect.name=='postgresql': connection.execute(text("SELECT set_config('statement_timeout','3000',true)"))
            profiles=list(connection.execute(select(table.c.embedding_model,table.c.embedding_dimensions).distinct()).mappings())
            if not profiles: raise KnowledgeError("Knowledge corpus is empty; run explicit ingestion first.")
            require_embedding_profile(profiles,settings)
            vector=embed_texts(client,[request.query],settings)[0]
            distance=table.c.embedding.cosine_distance(vector).label('distance')
            columns=[table.c[field] for field in KnowledgeChunk.model_fields]
            rows=connection.execute(select(*columns,distance).order_by(distance,table.c.chunk_id).limit(request.top_k)).mappings().all()
            hits=[KnowledgeHit(**dict(row),rank=index+1,similarity=1-float(row['distance'])) for index,row in enumerate(rows)]
        return KnowledgeSearchResult(query=request.query,embedding_model=settings.openai_embedding_model,hits=hits)
    except KnowledgeError: raise
    except Exception:
        raise KnowledgeError("Knowledge retrieval failed; check reader configuration and knowledge availability.") from None
