"""Explicit operations/knowledge migrations; historical and checkpoint tables are unmanaged."""

from alembic import context
from alembic.util import CommandError
from sqlalchemy.exc import SQLAlchemyError

from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.db.session import create_db_engine
from enterprise_ai_ops_agent.operations.models import OperationsBase
from enterprise_ai_ops_agent.knowledge.models import KnowledgeBase


def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table":
        return obj.schema in ("operations", "knowledge")
    return True


def configure(**kwargs):
    context.configure(
        target_metadata=[OperationsBase.metadata, KnowledgeBase.metadata], include_schemas=True,
        include_object=include_object, version_table_schema="public", **kwargs,
    )


def run_migrations():
    if context.is_offline_mode():
        configure(dialect_name="postgresql", literal_binds=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = None
    try:
        engine = create_db_engine(Settings())
        with engine.connect() as connection:
            configure(connection=connection)
            with context.begin_transaction():
                context.run_migrations()
    except (ValueError, SQLAlchemyError):
        raise CommandError("Migration failed: check DATABASE_URL, database availability and permissions.") from None
    finally:
        if engine is not None:
            engine.dispose()


run_migrations()
