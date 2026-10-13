"""Explicit idempotent knowledge-only reader provisioning; no analytics changes."""

from psycopg import ClientCursor, Error, sql
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.db.session import create_db_engine
from enterprise_ai_ops_agent.knowledge.contracts import KnowledgeError
from enterprise_ai_ops_agent.knowledge.session import KNOWLEDGE_ROLE, parse_knowledge_url


def inspect_knowledge_reader(connection) -> dict:
    role={'role':KNOWLEDGE_ROLE}
    flags=connection.execute(text('SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls,rolinherit FROM pg_roles WHERE rolname=:role'),role).mappings().one()
    unexpected=connection.execute(text("""SELECT n.nspname||'.'||c.relname AS target
      FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE c.relkind IN ('r','p','v','m','f') AND n.nspname NOT LIKE 'pg_%' AND n.nspname<>'information_schema'
      AND (has_table_privilege(:role,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER,MAINTAIN')
           OR has_any_column_privilege(:role,c.oid,'SELECT,INSERT,UPDATE,REFERENCES'))
      AND NOT (n.nspname='knowledge' AND c.relname='document_chunks')"""),role).scalars().all()
    permissions={priv:connection.execute(text("SELECT has_table_privilege(:role,'knowledge.document_chunks',:priv) OR has_any_column_privilege(:role,'knowledge.document_chunks',:priv)"),{**role,'priv':priv}).scalar_one()
                 for priv in ('SELECT','INSERT','UPDATE','REFERENCES')}
    for priv in ('DELETE','TRUNCATE','TRIGGER','MAINTAIN'):
        permissions[priv]=connection.execute(text("SELECT has_table_privilege(:role,'knowledge.document_chunks',:priv)"),{**role,'priv':priv}).scalar_one()
    schemas={name:connection.execute(text("SELECT has_schema_privilege(:role,:schema,'USAGE')"),{**role,'schema':name}).scalar_one() for name in ('knowledge','operations','workflow_checkpoints')}
    creates=connection.execute(text("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' AND has_schema_privilege(:role,oid,'CREATE')) OR has_database_privilege(:role,current_database(),'CREATE')"),role).scalar_one()
    memberships=connection.execute(text('SELECT EXISTS(SELECT 1 FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname=:role))'),role).scalar_one()
    owned=connection.execute(text("SELECT EXISTS(SELECT 1 FROM pg_shdepend d JOIN pg_roles r ON r.oid=d.refobjid WHERE r.rolname=:role AND d.refclassid='pg_authid'::regclass AND d.deptype='o')"),role).scalar_one()
    compliant=(not any(flags.values()) and not unexpected and not creates and not memberships and not owned
               and schemas=={'knowledge':True,'operations':False,'workflow_checkpoints':False}
               and permissions=={name:name=='SELECT' for name in permissions})
    return {'role':KNOWLEDGE_ROLE,'permissions':permissions,'schemas':schemas,'unexpected_access':unexpected,
            'administrative_flags':dict(flags),'compliant':bool(compliant)}


def provision_knowledge_reader(settings: Settings) -> dict:
    reader=parse_knowledge_url(settings)
    engine=None
    try:
        engine=create_db_engine(settings)
        admin=engine.url
        host=lambda value:'localhost' if value in ('localhost','127.0.0.1') else value
        if (reader.database!=admin.database or host(reader.host)!=host(admin.host)
                or (reader.port or 5432)!=(admin.port or 5432) or admin.username==KNOWLEDGE_ROLE):
            raise KnowledgeError("Knowledge provisioning requires administrator and reader URLs for the same database.")
        with engine.begin() as connection:
            exists=connection.execute(text('SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:role)'),{'role':KNOWLEDGE_ROLE}).scalar_one()
            if exists:
                unsafe=connection.execute(text("""SELECT EXISTS(SELECT 1 FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname=:role))
                  OR EXISTS(SELECT 1 FROM pg_shdepend d JOIN pg_roles r ON r.oid=d.refobjid WHERE r.rolname=:role AND d.refclassid='pg_authid'::regclass AND d.deptype='o')"""),{'role':KNOWLEDGE_ROLE}).scalar_one()
                if unsafe: raise KnowledgeError("Existing knowledge role owns objects or has memberships; review it manually.")
            with ClientCursor(connection.connection.driver_connection) as cursor:
                role=sql.Identifier(KNOWLEDGE_ROLE)
                cursor.execute(sql.SQL('{} ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD %s').format(sql.SQL('ALTER' if exists else 'CREATE'),role),(reader.password,))
                cursor.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM {}').format(sql.Identifier(reader.database),role))
                cursor.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(reader.database),role))
                for schema in ('public','knowledge','operations','workflow_checkpoints'):
                    cursor.execute(sql.SQL('REVOKE ALL ON SCHEMA {} FROM {}').format(sql.Identifier(schema),role))
                    cursor.execute(sql.SQL('REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM {}').format(sql.Identifier(schema),role))
                    cursor.execute(sql.SQL('REVOKE ALL ON ALL SEQUENCES IN SCHEMA {} FROM {}').format(sql.Identifier(schema),role))
                cursor.execute(sql.SQL('GRANT USAGE ON SCHEMA knowledge TO {}').format(role))
                cursor.execute(sql.SQL('GRANT SELECT ON knowledge.document_chunks TO {}').format(role))
            report=inspect_knowledge_reader(connection)
            if not report['compliant']: raise KnowledgeError("Knowledge reader effective privileges do not meet the intended boundary; provisioning rolled back.")
        return report
    except KnowledgeError: raise
    except (ValueError,Error,SQLAlchemyError):
        raise KnowledgeError("Knowledge reader provisioning failed; check local administrative configuration.") from None
    finally:
        if engine is not None: engine.dispose()
