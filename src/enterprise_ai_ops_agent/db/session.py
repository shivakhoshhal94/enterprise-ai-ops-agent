"""Explicit engine and session construction for future database callers."""

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.engine import URL
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import Session, sessionmaker

from enterprise_ai_ops_agent.config import Settings

ANALYTICS_ROLE_NAME = "enterprise_ai_ops_reader"


def engine_options(connect_timeout: int = 5) -> dict:
    """Small per-owner pools; connection acquisition and statements are bounded."""
    return {"pool_pre_ping": True, "pool_size": 2, "max_overflow": 0, "pool_timeout": 2,
            "hide_parameters": True,
            "connect_args": {"connect_timeout": connect_timeout, "options": "-cstatement_timeout=5000"}}


def parse_analytics_url(settings: Settings) -> URL:
    """Require the dedicated login; never fall back to administrative configuration."""
    secret = settings.analytics_database_url
    if secret is None or not secret.get_secret_value().strip():
        raise ValueError("Set ANALYTICS_DATABASE_URL before using guarded analytics.")
    try:
        url = make_url(secret.get_secret_value())
    except ArgumentError:
        raise ValueError("ANALYTICS_DATABASE_URL must be a valid PostgreSQL URL.") from None
    if (url.drivername != "postgresql+psycopg" or not url.database or not url.host
            or url.username != ANALYTICS_ROLE_NAME or not url.password):
        raise ValueError("ANALYTICS_DATABASE_URL requires the dedicated reader login, password, host, database and psycopg driver.")
    return url


def create_analytics_engine(settings: Settings, *, connect_timeout: int = 5) -> Engine:
    """Lazy analytics-only engine; optional until explicitly invoked."""
    return create_engine(parse_analytics_url(settings), **engine_options(connect_timeout))


def create_db_engine(settings: Settings, *, connect_timeout: int = 5) -> Engine:
    """Build an engine lazily; no connection opens until it is used."""
    if settings.database_url is None or not settings.database_url.get_secret_value().strip():
        raise ValueError("Set DATABASE_URL before using the database layer.")

    url = make_url(settings.database_url.get_secret_value())
    if url.drivername != "postgresql+psycopg":
        raise ValueError("DATABASE_URL must use the postgresql+psycopg driver.")
    if not url.database:
        raise ValueError("DATABASE_URL must include a database name.")
    return create_engine(url, **engine_options(connect_timeout))


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a factory; callers own session contexts and transactions."""
    return sessionmaker(bind=engine)
