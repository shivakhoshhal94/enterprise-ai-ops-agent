"""Lazy read-only adapter to the existing knowledge service; no retrieval duplication."""

from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.ai.knowledge_contracts import KnowledgeEvidence, KnowledgeToolResult
from enterprise_ai_ops_agent.knowledge.contracts import KnowledgeError
from enterprise_ai_ops_agent.knowledge.service import search_company_knowledge
from enterprise_ai_ops_agent.knowledge.session import create_knowledge_engine

ANSWER_TOP_K = 3


def retrieve_evidence(query: str, *, settings: Settings, client) -> KnowledgeToolResult:
    engine = None
    try:
        # Only the dedicated URL is accepted. Never reuse the caller's business engine.
        engine = create_knowledge_engine(settings)
        result = search_company_knowledge(
            query, top_k=ANSWER_TOP_K, engine=engine, client=client, settings=settings,
        )
        return KnowledgeToolResult(evidence=[
            KnowledgeEvidence(document=hit.document_id, title=hit.title, section=hit.section,
                              content=hit.content, rank=hit.rank, distance=hit.distance)
            for hit in result.hits
        ])
    except Exception:
        raise KnowledgeError("Company knowledge service is unavailable; check its configuration and access.") from None
    finally:
        if engine is not None:
            engine.dispose()
