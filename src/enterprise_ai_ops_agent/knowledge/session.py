"""Knowledge-only runtime login; administrative configuration is never a fallback."""

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.knowledge.contracts import KnowledgeError
from enterprise_ai_ops_agent.db.session import engine_options

KNOWLEDGE_ROLE = "enterprise_ai_ops_knowledge_reader"


def parse_knowledge_url(settings: Settings):
    try:
        if settings.knowledge_database_url is None: raise ValueError()
        url=make_url(settings.knowledge_database_url.get_secret_value())
        if (url.drivername!="postgresql+psycopg" or url.username!=KNOWLEDGE_ROLE
                or not url.password or not url.host or not url.database): raise ValueError()
        return url
    except (ValueError, ArgumentError):
        raise KnowledgeError("KNOWLEDGE_DATABASE_URL requires the dedicated knowledge reader login and PostgreSQL configuration.") from None


def create_knowledge_engine(settings: Settings, *, connect_timeout: int = 5):
    return create_engine(parse_knowledge_url(settings), **engine_options(connect_timeout))
