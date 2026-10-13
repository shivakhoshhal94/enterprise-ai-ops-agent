from unittest.mock import MagicMock

import pytest
from psycopg import OperationalError

from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.db import provision_analytics_role as provisioning

ADMIN_URL = "postgresql+psycopg://admin:admin_secret@localhost/enterprise_ai_ops_agent"
READER_URL = "postgresql+psycopg://enterprise_ai_ops_reader:reader_secret@localhost/enterprise_ai_ops_agent"


def compliant_report():
    return provisioning.AnalyticsPrivileges(
        role=provisioning.ANALYTICS_ROLE_NAME,
        attributes={name: name == "rolcanlogin" for name in (
            "rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls", "rolcanlogin", "rolinherit",
        )},
        database={"CONNECT": True, "CREATE": False, "TEMPORARY": False},
        schema_privileges={"USAGE": True, "CREATE": False},
        tables={table: {name: name == "SELECT" for name in provisioning.TABLE_PRIVILEGES} for table in provisioning.ALLOWED_TABLES},
        owns_objects=False, memberships=[], unexpected_access=[],
    )


def test_report_recognizes_write_and_ownership_defects():
    report = compliant_report()
    assert report.compliant
    for privilege in provisioning.TABLE_PRIVILEGES[1:]:
        changed = report.model_copy(deep=True)
        changed.tables["orders"][privilege] = True
        assert not changed.compliant
    for name, value in (("owns_objects", True), ("memberships", ["admin"]), ("unexpected_access", ["private.extra:SELECT"])):
        assert not report.model_copy(update={name: value}).compliant
    for attribute in ("rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls", "rolinherit"):
        changed = report.model_copy(deep=True)
        changed.attributes[attribute] = True
        assert not changed.compliant


def inspection_connection(*, column_update=False, table_select=True):
    connection = MagicMock()
    report = compliant_report()
    rows = [{"schema_name": "public", "table_name": table, "privilege": name,
             "allowed": table_select if name == "SELECT" else False,
             "column_allowed": (name == "SELECT" or (name == "UPDATE" and column_update))}
            for table in provisioning.ALLOWED_TABLES for name in provisioning.TABLE_PRIVILEGES]
    attributes = MagicMock()
    attributes.mappings.return_value.one_or_none.return_value = report.attributes
    owns = MagicMock()
    owns.scalar_one.return_value = False
    memberships = MagicMock()
    memberships.scalars.return_value.all.return_value = []
    grants = []
    for value in (True, False, False, True, False):
        result = MagicMock()
        result.scalar_one.return_value = value
        grants.append(result)
    tables = MagicMock()
    tables.mappings.return_value.all.return_value = rows
    connection.execute.side_effect = [attributes, owns, memberships, *grants, tables]
    return connection


@pytest.mark.parametrize("column_update,table_select,expected", [(False, True, True), (True, True, False), (False, False, False)])
def test_effective_privilege_inspection(column_update, table_select, expected):
    report = provisioning.inspect_analytics_privileges(inspection_connection(column_update=column_update, table_select=table_select))
    assert report.compliant is expected
    assert report.tables["orders"]["UPDATE"] is column_update
    assert report.tables["orders"]["SELECT"] is table_select


@pytest.mark.parametrize("analytics_url", [None, ADMIN_URL, READER_URL.replace("localhost", "different-host")])
def test_provisioning_missing_or_wrong_config_safe(analytics_url, monkeypatch):
    factory = MagicMock()
    monkeypatch.setattr(provisioning, "create_db_engine", factory)
    settings = Settings(_env_file=None, database_url=ADMIN_URL, analytics_database_url=analytics_url)
    with pytest.raises(provisioning.AnalyticsRoleError) as error:
        provisioning.provision_analytics_role(settings)
    assert "admin_secret" not in str(error.value) and "reader_secret" not in str(error.value)
    factory.assert_not_called()


def fake_provisioning(monkeypatch, *, exists=False, report=None):
    engine = MagicMock()
    connection = engine.begin.return_value.__enter__.return_value
    connection.execute.return_value.scalar_one.return_value = exists
    monkeypatch.setattr(provisioning, "create_db_engine", lambda settings: engine)
    inspect = MagicMock(return_value=report or compliant_report())
    monkeypatch.setattr(provisioning, "inspect_analytics_privileges", inspect)
    cursor = MagicMock()
    client_cursor = MagicMock()
    client_cursor.return_value.__enter__.return_value = cursor
    monkeypatch.setattr(provisioning, "ClientCursor", client_cursor)
    return engine, cursor, inspect


@pytest.mark.parametrize("exists", [False, True])
def test_explicit_grants_and_reconciliation(exists, monkeypatch):
    engine, cursor, inspect = fake_provisioning(monkeypatch, exists=exists)
    result = provisioning.provision_analytics_role(Settings(_env_file=None, database_url=ADMIN_URL, analytics_database_url=READER_URL))
    assert result.compliant
    statements = [call.args[0].as_string() for call in cursor.execute.call_args_list]
    assert statements[0].startswith("ALTER ROLE" if exists else "CREATE ROLE")
    assert provisioning.ROLE_OPTIONS in statements[0]
    assert "%s" in statements[0] and "reader_secret" not in statements[0]
    assert cursor.execute.call_args_list[0].args[1] == ("reader_secret",)
    assert not any("FROM PUBLIC" in statement or "TO PUBLIC" in statement for statement in statements)
    grants = [statement for statement in statements if statement.startswith("GRANT")]
    assert len(grants) == 9  # CONNECT, USAGE, seven explicit SELECT grants.
    assert all(statement.startswith(("GRANT CONNECT", "GRANT USAGE", "GRANT SELECT ON TABLE")) for statement in grants)
    assert not any("DROP" in statement for statement in statements)
    assert inspect.call_count == (2 if exists else 1)
    engine.dispose.assert_called_once()


def test_existing_owned_role_refused_before_changes(monkeypatch):
    report = compliant_report().model_copy(update={"owns_objects": True})
    engine, cursor, _ = fake_provisioning(monkeypatch, exists=True, report=report)
    with pytest.raises(provisioning.AnalyticsRoleError, match="review manually"):
        provisioning.provision_analytics_role(Settings(_env_file=None, database_url=ADMIN_URL, analytics_database_url=READER_URL))
    cursor.execute.assert_not_called()
    engine.begin.return_value.__exit__.assert_called_once()


def test_privilege_defect_causes_transaction_rollback(monkeypatch):
    report = compliant_report().model_copy(update={"unexpected_access": ["private.extra:SELECT"]})
    engine, _, _ = fake_provisioning(monkeypatch, report=report)
    with pytest.raises(provisioning.AnalyticsRoleError, match="rolled back"):
        provisioning.provision_analytics_role(Settings(_env_file=None, database_url=ADMIN_URL, analytics_database_url=READER_URL))
    assert engine.begin.return_value.__exit__.call_args.args[0] is provisioning.AnalyticsRoleError


def test_database_failure_never_discloses_password(monkeypatch):
    engine, cursor, _ = fake_provisioning(monkeypatch)
    cursor.execute.side_effect = OperationalError("reader_secret admin_secret")
    with pytest.raises(provisioning.AnalyticsRoleError) as error:
        provisioning.provision_analytics_role(Settings(_env_file=None, database_url=ADMIN_URL, analytics_database_url=READER_URL))
    assert "secret" not in str(error.value)
    engine.dispose.assert_called_once()


def test_cli_failure_safe(monkeypatch, capsys):
    monkeypatch.setattr(provisioning, "Settings", lambda: Settings(_env_file=None, database_url=None, analytics_database_url=None))
    assert provisioning.main() == 1
    assert "No credentials are displayed" in capsys.readouterr().err


def test_inherited_public_temporary_is_reported_without_global_changes():
    report = compliant_report()
    report.database["TEMPORARY"] = True
    assert report.compliant
    assert report.database["TEMPORARY"] is True
