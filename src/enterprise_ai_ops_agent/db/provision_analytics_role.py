"""Explicit transactional admin provisioning and effective privilege inspection."""

import sys

from psycopg import ClientCursor, Error, sql
from pydantic import BaseModel
from sqlalchemy import Connection, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from enterprise_ai_ops_agent.analytics.validator import ALLOWED_TABLES
from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.db.session import ANALYTICS_ROLE_NAME, create_db_engine, parse_analytics_url

ROLE_OPTIONS = "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT"
TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER", "MAINTAIN")


class AnalyticsRoleError(RuntimeError):
    """Safe provisioning/inspection failure without SQL or credential details."""


class AnalyticsPrivileges(BaseModel):
    role: str
    attributes: dict[str, bool]
    database: dict[str, bool]
    schema_privileges: dict[str, bool]
    tables: dict[str, dict[str, bool]]
    owns_objects: bool
    memberships: list[str]
    unexpected_access: list[str]

    @property
    def compliant(self) -> bool:
        return (
            self.attributes.get("rolcanlogin") is True
            and all(self.attributes.get(name) is False for name in (
                "rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls", "rolinherit",
            ))
            # TEMPORARY may be inherited from PUBLIC; provisioning never changes PUBLIC.
            and self.database.get("CONNECT") is True and self.database.get("CREATE") is False
            and self.schema_privileges == {"USAGE": True, "CREATE": False}
            and set(self.tables) == set(ALLOWED_TABLES)
            and all(privileges == {name: name == "SELECT" for name in TABLE_PRIVILEGES}
                    for privileges in self.tables.values())
            and not self.owns_objects and not self.memberships and not self.unexpected_access
        )


def inspect_analytics_privileges(connection: Connection) -> AnalyticsPrivileges:
    """Inspect effective grants, including PUBLIC and column-level privileges."""
    parameters = {"role": ANALYTICS_ROLE_NAME}
    attributes = connection.execute(text("""
        SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolcanlogin, rolinherit
        FROM pg_roles WHERE rolname=:role
    """), parameters).mappings().one_or_none()
    if attributes is None:
        raise AnalyticsRoleError("Analytics role does not exist; run provisioning first.")
    owns_objects = connection.execute(text("""
        SELECT EXISTS (SELECT 1 FROM pg_shdepend d JOIN pg_roles r ON r.oid=d.refobjid
                       WHERE r.rolname=:role AND d.refclassid='pg_authid'::regclass AND d.deptype='o')
    """), parameters).scalar_one()
    memberships = connection.execute(text("""
        SELECT parent.rolname FROM pg_auth_members m JOIN pg_roles member ON member.oid=m.member
        JOIN pg_roles parent ON parent.oid=m.roleid WHERE member.rolname=:role ORDER BY parent.rolname
    """), parameters).scalars().all()
    database = {privilege: connection.execute(text(
        "SELECT has_database_privilege(:role,current_database(),:privilege)"
    ), {**parameters, "privilege": privilege}).scalar_one() for privilege in ("CONNECT", "CREATE", "TEMPORARY")}
    schema = {privilege: connection.execute(text(
        "SELECT has_schema_privilege(:role,'public',:privilege)"
    ), {**parameters, "privilege": privilege}).scalar_one() for privilege in ("USAGE", "CREATE")}
    rows = connection.execute(text("""
        SELECT n.nspname AS schema_name,c.relname AS table_name,p.privilege,
               has_table_privilege(:role,c.oid,p.privilege) AS allowed,
               CASE WHEN p.privilege IN ('SELECT','INSERT','UPDATE','REFERENCES')
                    THEN has_any_column_privilege(:role,c.oid,p.privilege) ELSE false END AS column_allowed
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        CROSS JOIN unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER','MAINTAIN']) p(privilege)
        WHERE c.relkind IN ('r','p','v','m','f') AND n.nspname NOT LIKE 'pg_%'
              AND n.nspname <> 'information_schema'
        ORDER BY n.nspname,c.relname,p.privilege
    """), parameters).mappings().all()
    tables: dict[str, dict[str, bool]] = {}
    unexpected: list[str] = []
    for row in rows:
        if row["schema_name"] == "public" and row["table_name"] in ALLOWED_TABLES:
            # SELECT must cover the whole table; write denial also checks column ACLs.
            effective = row["allowed"] if row["privilege"] == "SELECT" else row["allowed"] or row["column_allowed"]
            tables.setdefault(row["table_name"], {})[row["privilege"]] = bool(effective)
        elif row["allowed"] or row["column_allowed"]:
            unexpected.append(f'{row["schema_name"]}.{row["table_name"]}:{row["privilege"]}')
    return AnalyticsPrivileges(
        role=ANALYTICS_ROLE_NAME, attributes=dict(attributes), database=database, schema_privileges=schema,
        tables=tables, owns_objects=owns_objects, memberships=memberships, unexpected_access=unexpected,
    )


def provision_analytics_role(settings: Settings) -> AnalyticsPrivileges:
    """Create/reconcile one dedicated role; verify before committing any permission changes."""
    try:
        reader_url = parse_analytics_url(settings)
        if settings.database_url is None:
            raise ValueError
        admin_url = make_url(settings.database_url.get_secret_value())
        canonical_host = lambda host: "localhost" if host in ("localhost", "127.0.0.1") else host
        if (reader_url.database != "enterprise_ai_ops_agent" or admin_url.database != reader_url.database
                or canonical_host(admin_url.host) != canonical_host(reader_url.host)
                or (admin_url.port or 5432) != (reader_url.port or 5432)
                or admin_url.username == ANALYTICS_ROLE_NAME):
            raise ValueError
        engine = create_db_engine(settings)
    except (ValueError, SQLAlchemyError):
        raise AnalyticsRoleError("Provisioning requires administrative DATABASE_URL and a dedicated ANALYTICS_DATABASE_URL for the same project database.") from None
    try:
        with engine.begin() as connection:
            exists = connection.execute(text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:role)"), {"role": ANALYTICS_ROLE_NAME}).scalar_one()
            if exists:
                previous = inspect_analytics_privileges(connection)
                if previous.owns_objects or previous.memberships:
                    raise AnalyticsRoleError("Existing analytics role owns objects or has role memberships; review manually before provisioning.")
            # Utility DDL does not accept server-side password bind parameters.
            # ClientCursor adapts the value safely, while Identifier quotes object names.
            with ClientCursor(connection.connection.driver_connection) as cursor:
                role = sql.Identifier(ANALYTICS_ROLE_NAME)
                database = sql.Identifier(reader_url.database)
                verb = sql.SQL("ALTER" if exists else "CREATE")
                cursor.execute(sql.SQL("{} ROLE {} WITH " + ROLE_OPTIONS + " PASSWORD %s").format(verb, role), (reader_url.password,))
                cursor.execute(sql.SQL("REVOKE ALL PRIVILEGES ON DATABASE {} FROM {}").format(database, role))
                cursor.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(database, role))
                cursor.execute(sql.SQL("REVOKE ALL PRIVILEGES ON SCHEMA public FROM {}").format(role))
                cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
                cursor.execute(sql.SQL("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {}").format(role))
                cursor.execute(sql.SQL("REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {}").format(role))
                for table in sorted(ALLOWED_TABLES):
                    cursor.execute(sql.SQL("GRANT SELECT ON TABLE {} TO {}").format(sql.Identifier("public", table), role))
            report = inspect_analytics_privileges(connection)
            if not report.compliant:
                raise AnalyticsRoleError("Effective analytics privileges exceed or miss the intended policy; provisioning rolled back.")
        return report
    except (Error, SQLAlchemyError):
        raise AnalyticsRoleError("Analytics role provisioning failed; check administrative access and local configuration.") from None
    finally:
        engine.dispose()


def main() -> int:
    try:
        report = provision_analytics_role(Settings())
    except (ValueError, AnalyticsRoleError):
        print("Analytics role provisioning failed; check configuration and administrative privileges. No credentials are displayed.", file=sys.stderr)
        return 1
    print(f"Analytics role {report.role} provisioned and effective privileges verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
