"""Explicit official saver setup and runtime; no application import/startup DDL."""

from contextlib import contextmanager, suppress
from hashlib import blake2b

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection, Error
from psycopg.rows import dict_row
from sqlalchemy.exc import SQLAlchemyError

from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.db.session import ANALYTICS_ROLE_NAME, create_db_engine
from enterprise_ai_ops_agent.workflows.contracts import WorkflowError
from enterprise_ai_ops_agent.workflows.service import OrderReviewWorkflow

CHECKPOINT_SCHEMA = "workflow_checkpoints"


@contextmanager
def _resources(settings: Settings):
    engine = None
    try:
        engine = create_db_engine(settings)
        if engine.url.username == ANALYTICS_ROLE_NAME:
            raise WorkflowError("Workflow persistence requires application administrative configuration, not the analytics reader.")
        # Driver-specific SQLAlchemy prefix is not a libpq PostgreSQL URI.
        uri = engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
        with Connection.connect(
            uri, autocommit=True, prepare_threshold=0, row_factory=dict_row,
            connect_timeout=5, options=f"-csearch_path={CHECKPOINT_SCHEMA} -cstatement_timeout=5000 -clock_timeout=3000",
        ) as connection:
            yield engine, connection, PostgresSaver(connection)
    except WorkflowError:
        raise
    except (ValueError, Error, SQLAlchemyError):
        raise WorkflowError("Workflow database operation failed; check DATABASE_URL and explicit checkpointer setup.") from None
    finally:
        if engine is not None:
            engine.dispose()


def initialize_checkpointer(settings: Settings) -> None:
    """Explicit setup only. LangGraph owns all checkpoint tables/migrations."""
    with _resources(settings) as (_, connection, saver):
        with connection.cursor() as cursor:
            cursor.execute("CREATE SCHEMA IF NOT EXISTS workflow_checkpoints")
            cursor.execute("REVOKE ALL ON SCHEMA workflow_checkpoints FROM PUBLIC")
        saver.setup()
        with connection.cursor() as cursor:
            cursor.execute("""SELECT EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname=%s
            ) AS present""", (ANALYTICS_ROLE_NAME,))
            if cursor.fetchone()["present"]:
                cursor.execute("SELECT has_schema_privilege(%s,'workflow_checkpoints','USAGE') AS allowed", (ANALYTICS_ROLE_NAME,))
                if cursor.fetchone()["allowed"]:
                    raise WorkflowError("Analytics reader unexpectedly has checkpoint access; review privileges.")


@contextmanager
def postgres_workflow(settings: Settings):
    """Open resources lazily; reconstruct the graph without running setup."""
    with _resources(settings) as (engine, connection, saver):
        @contextmanager
        def workflow_lock(workflow_id: str):
            # Serialize decisions across separate CLI processes. Session locks are
            # released automatically on connection loss, including process crashes.
            key = int.from_bytes(blake2b(workflow_id.encode(), digest_size=8).digest(), "big", signed=True)
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(%s)", (key,))
            try:
                yield
            finally:
                with suppress(Error):
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT pg_advisory_unlock(%s)", (key,))
        yield OrderReviewWorkflow(engine=engine, checkpointer=saver, lock=workflow_lock)
