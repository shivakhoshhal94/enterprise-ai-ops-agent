"""Explicit official embeddings calls with strict shape/finite-vector validation."""

import math
from enterprise_ai_ops_agent.observability import timed
from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.knowledge.contracts import DIMENSIONS, KnowledgeError


def embed_texts(client, texts: list[str], settings: Settings) -> list[list[float]]:
    if not settings.openai_embedding_model.strip() or settings.openai_embedding_dimensions!=DIMENSIONS:
        raise KnowledgeError("Embedding model and schema dimensions must be configured consistently.")
    if not texts or any(not isinstance(value,str) or not value.strip() or len(value)>2000 for value in texts):
        raise KnowledgeError("Embedding inputs must be nonblank bounded text.")
    try:
        with timed("provider"):
            response=client.embeddings.create(model=settings.openai_embedding_model,dimensions=DIMENSIONS,
                                              input=texts,encoding_format="float")
        if response.model!=settings.openai_embedding_model:
            raise ValueError("Unexpected embedding model")
        items=sorted(response.data,key=lambda item:item.index)
        if [item.index for item in items]!=list(range(len(texts))): raise ValueError("Invalid embedding indexes")
        vectors=[list(item.embedding) for item in items]
        for vector in vectors:
            if (len(vector)!=DIMENSIONS or any(isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) for x in vector)
                    or sum(x*x for x in vector)<=0): raise ValueError("Invalid embedding vector")
        return vectors
    except Exception:
        raise KnowledgeError("Embedding request failed or returned incompatible vectors.") from None
