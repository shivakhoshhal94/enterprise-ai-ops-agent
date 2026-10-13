"""Fail-closed PostgreSQL AST policy, with scope-aware physical table resolution."""

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, SqlglotError
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.scope import traverse_scope

from enterprise_ai_ops_agent.analytics.models import SQLValidationError, ValidatedQuery

ALLOWED_TABLES = frozenset({
    "customers", "category_translations", "sellers", "products",
    "orders", "order_items", "order_payments",
})
MAX_SQL_LENGTH = 20_000

# Entire operational families are denied; unknown functions also fail closed below.
DENIED_FUNCTION_PREFIXES = ("pg_", "lo_", "dblink", "query_to_xml", "table_to_xml", "database_to_xml", "schema_to_xml")
DENIED_FUNCTIONS = frozenset({
    "set_config", "nextval", "setval", "currval", "lastval", "load_extension",
    "generate_series", "repeat", "lpad", "rpad",
})
ALLOWED_FUNCTIONS = frozenset({
    "COUNT", "SUM", "AVG", "MIN", "MAX", "ABS", "ROUND", "CEIL", "FLOOR",
    "COALESCE", "NULLIF", "GREATEST", "LEAST", "CASE", "IF", "CAST",
    "EXTRACT", "TIMESTAMP_TRUNC", "DATE_TRUNC", "DATE", "CURRENT_DATE",
    "CURRENT_TIMESTAMP", "LOWER", "UPPER", "LENGTH", "SUBSTRING", "TRIM",
    "CONCAT", "CONCAT_WS", "STRING_AGG", "GROUP_CONCAT", "ARRAY_AGG",
    "STDDEV", "STDDEV_POP", "STDDEV_SAMP", "VARIANCE", "VAR_POP", "VAR_SAMP",
    "BOOL_AND", "BOOL_OR", "ROW_NUMBER", "RANK", "DENSE_RANK", "LAG", "LEAD",
    "FIRST_VALUE", "LAST_VALUE", "NTH_VALUE", "NTILE",
})
# New parser constructs require review, rather than being implicitly executable.
ALLOWED_NODE_KEYS = frozenset("""
select union intersect except subquery with cte table tablealias identifier column
from join where group having order ordered limit offset distinct star alias
literal null boolean var paren tuple interval datatype datatypeparam
add sub mul div intdiv mod neg pow eq neq gt gte lt lte and or not is
between in exists like ilike escape filter window windowspec withinGroup
atTimeZone dot bracket slice concat dpipe
""".lower().split())
ALLOWED_CAST_TYPES = frozenset({
    "BIGINT", "INT", "SMALLINT", "DECIMAL", "DOUBLE", "FLOAT", "TEXT", "VARCHAR",
    "CHAR", "DATE", "TIME", "TIMESTAMP", "TIMESTAMPTZ", "INTERVAL", "BOOLEAN",
})


def validate_analytical_sql(sql: str) -> ValidatedQuery:
    if not isinstance(sql, str) or not sql.strip() or len(sql) > MAX_SQL_LENGTH:
        raise SQLValidationError("Provide a nonblank SQL statement of at most 20000 characters.")
    try:
        statements = sqlglot.parse(sql, read="postgres", error_level=ErrorLevel.RAISE)
        # SQLGlot represents comments after a trailing semicolon as a comment-only node.
        if len(statements) == 2 and isinstance(statements[-1], exp.Semicolon) and statements[-1].comments:
            statements.pop()
        if len(statements) != 1 or statements[0] is None:
            raise SQLValidationError("Exactly one SQL statement is required.")
        tree = normalize_identifiers(statements[0], dialect="postgres")
        if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
            raise SQLValidationError("Only analytical SELECT statements are allowed.")
        for node in tree.walk():
            if isinstance(node, exp.Func):
                name = node.name.lower() if isinstance(node, exp.Anonymous) else node.sql_name().lower()
                if name in DENIED_FUNCTIONS or name.startswith(DENIED_FUNCTION_PREFIXES):
                    raise SQLValidationError("Operational or unsafe functions are forbidden.")
                if name.upper() not in ALLOWED_FUNCTIONS:
                    raise SQLValidationError("Function is not in the analytical allowlist.")
                if isinstance(node.parent, exp.Dot):
                    raise SQLValidationError("Schema-qualified function calls are forbidden.")
            elif node.key not in ALLOWED_NODE_KEYS:
                raise SQLValidationError("SQL contains an unsupported or unsafe construct.")
            if isinstance(node, exp.With) and node.args.get("recursive"):
                raise SQLValidationError("Recursive CTEs are not enabled.")
            if isinstance(node, exp.DataType) and node.this.value not in ALLOWED_CAST_TYPES:
                raise SQLValidationError("Only reviewed scalar analytical cast types are allowed.")
            if isinstance(node, exp.Column) and (node.db or node.catalog):
                raise SQLValidationError("Qualified column schemas are not enabled.")

        referenced: set[str] = set()
        resolved: set[int] = set()
        scopes = traverse_scope(tree)
        for scope in scopes:
            # Includes physical sources in nested and unused CTEs, not just the outer query.
            for source in scope.sources.values():
                if not isinstance(source, exp.Table):
                    continue  # CTE and derived-table scopes are inspected separately.
                if not isinstance(source.this, exp.Identifier):
                    raise SQLValidationError("Table-valued functions are not enabled.")
                if source.catalog or source.db not in ("", "public") or source.name not in ALLOWED_TABLES:
                    raise SQLValidationError("Only the seven public application tables are allowed.")
                referenced.add(source.name)
                resolved.add(id(source))
                source.set("db", exp.to_identifier("public"))
            # Trigger duplicate alias detection rather than silently resolving ambiguities.
            scope.selected_sources
        for table in tree.find_all(exp.Table):
            if id(table) in resolved:
                continue
            # Remaining tables must be genuine in-scope CTE aliases, never qualified names.
            if table.db or table.catalog or not isinstance(table.this, exp.Identifier):
                raise SQLValidationError("Unresolved or qualified table reference.")
            if not any(table in scope.tables and table.name in scope.cte_sources for scope in scopes):
                raise SQLValidationError("Unresolved table reference.")
        normalized = tree.sql(dialect="postgres", comments=False, unsupported_level=ErrorLevel.RAISE)
        return ValidatedQuery(normalized, tuple(sorted(referenced)))
    except SQLValidationError:
        raise
    except (SqlglotError, ValueError, TypeError, RecursionError):
        raise SQLValidationError("SQL could not be parsed or resolved safely.") from None
