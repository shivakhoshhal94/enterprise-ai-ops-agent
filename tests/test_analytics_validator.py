import pytest

from enterprise_ai_ops_agent.analytics.models import SQLValidationError
from enterprise_ai_ops_agent.analytics.validator import validate_analytical_sql


@pytest.mark.parametrize("sql,tables", [
    ("SELECT order_id FROM orders;", ("orders",)),
    ("SELECT COUNT(*), SUM(payment_value) FROM order_payments", ("order_payments",)),
    ("SELECT o.order_id FROM orders o JOIN customers c USING (customer_id)", ("customers", "orders")),
    ("SELECT order_status, COUNT(*) FROM orders GROUP BY order_status ORDER BY 2 DESC", ("orders",)),
    ("WITH x AS (SELECT * FROM orders) SELECT COUNT(*) FROM x", ("orders",)),
    ("WITH x AS (SELECT * FROM orders), y AS (SELECT * FROM x) SELECT * FROM y", ("orders",)),
    ("SELECT * FROM (SELECT order_id FROM orders) x", ("orders",)),
    ("SELECT order_id FROM orders UNION ALL SELECT order_id FROM order_items", ("order_items", "orders")),
    ("SELECT order_id FROM orders UNION SELECT order_id FROM order_items", ("order_items", "orders")),
    ("SELECT date_trunc('month', order_purchase_timestamp), COUNT(*) FROM orders GROUP BY 1", ("orders",)),
    ("SELECT EXTRACT(YEAR FROM order_purchase_timestamp), order_id::text FROM orders", ("orders",)),
    ("SELECT CASE WHEN order_status='canceled' THEN 1 ELSE 0 END FROM orders", ("orders",)),
    ("SELECT ROW_NUMBER() OVER (PARTITION BY order_status ORDER BY order_id) FROM orders", ("orders",)),
    ("SELECT COUNT(*) FILTER (WHERE order_status='canceled') FROM orders", ("orders",)),
    ("SELECT * FROM public.orders", ("orders",)),
    ('SELECT * FROM "orders"', ("orders",)),
    ("SELECT * FROM ORDERS", ("orders",)),
    ("WITH orders AS (SELECT * FROM customers) SELECT * FROM orders", ("customers",)),
    ("SELECT 'DELETE; pg_sleep(2)' AS harmless_literal", ()),
    ("SELECT 1 /* DROP TABLE orders */; -- trailing comment", ()),
])
def test_allowed_analytics(sql, tables):
    query = validate_analytical_sql(sql)
    assert query.referenced_tables == tables
    assert validate_analytical_sql(query.normalized_sql) == query
    for table in tables:
        assert f"public.{table}" in query.normalized_sql or f'public."{table}"' in query.normalized_sql


@pytest.mark.parametrize("sql", [
    "INSERT INTO orders (order_id) VALUES ('x')", "UPDATE orders SET order_status='x'",
    "DELETE FROM orders", "MERGE INTO orders USING customers ON true WHEN MATCHED THEN DELETE",
    "CREATE TABLE x (a int)", "ALTER TABLE orders ADD COLUMN x int", "DROP TABLE orders",
    "TRUNCATE orders", "GRANT SELECT ON orders TO public", "REVOKE SELECT ON orders FROM public",
    "COPY orders TO '/tmp/orders'", "VACUUM orders", "ANALYZE orders", "REINDEX TABLE orders",
    "CALL something()", "DO $$ BEGIN NULL; END $$", "SET statement_timeout=0", "RESET ALL",
    "SELECT 1; SELECT 2", "SELECT * FROM orders; DELETE FROM orders", "SELECT 1;;",
    "SELECT * FROM unknown_table", "SELECT * FROM private.orders", "SELECT * FROM other.public.orders",
    "SELECT * FROM information_schema.tables", "SELECT * FROM pg_catalog.pg_tables",
    "SELECT * FROM pg_tables", 'SELECT * FROM "Orders"',
    "SELECT * INTO copied_orders FROM orders", "SELECT * FROM orders FOR UPDATE",
    "SELECT * FROM orders FOR SHARE", "SELECT * FROM orders FOR NO KEY UPDATE",
    "SELECT * FROM orders FOR KEY SHARE SKIP LOCKED",
    "WITH x AS (DELETE FROM orders RETURNING *) SELECT * FROM x",
    "WITH x AS (UPDATE orders SET order_status='x' RETURNING *) SELECT * FROM x",
    "WITH unused AS (SELECT * FROM pg_catalog.pg_tables) SELECT 1",
    "WITH x AS (SELECT * FROM customers) SELECT * FROM public.x",
    "WITH x AS (SELECT * FROM customers) SELECT * FROM unknown_table x",
    "SELECT pg_sleep(1)", "SELECT PG_SLEEP(1)", 'SELECT "pg_sleep"(1)',
    "SELECT pg_catalog.pg_sleep(1)", "SELECT pg_read_file('/etc/passwd')",
    "SELECT pg_read_binary_file('/etc/passwd')", "SELECT pg_ls_dir('.')", "SELECT pg_stat_file('.')",
    "SELECT lo_import('/tmp/a')", "SELECT lo_export(1,'/tmp/a')",
    "SELECT dblink_exec('remote','DELETE FROM orders')", "SELECT pg_advisory_lock(1)",
    "SELECT set_config('transaction_read_only','off',true)", "SELECT nextval('some_sequence')",
    "SELECT query_to_xml('SELECT * FROM pg_catalog.pg_tables', true, false, '')",
    "SELECT custom_function()", "SELECT public.sum(payment_value) FROM order_payments",
    "SELECT * FROM generate_series(1,1000000000)", "SELECT repeat('x',1000000000)",
    "SELECT 'orders'::regclass", "SELECT 'x'::custom_type", "SELECT 'x'::xml",
    "WITH RECURSIVE x AS (SELECT 1 UNION ALL SELECT 1 FROM x) SELECT * FROM x",
    "SELECT * FROM orders TABLESAMPLE SYSTEM(10)", "SELECT :parameter", "not SQL", "", "   ",
])
def test_unsafe_or_unreviewed_sql_rejected(sql):
    with pytest.raises(SQLValidationError):
        validate_analytical_sql(sql)


def test_input_limit_and_safe_parse_error():
    with pytest.raises(SQLValidationError):
        validate_analytical_sql("SELECT '" + "x" * 20000 + "'")
    with pytest.raises(SQLValidationError) as error:
        validate_analytical_sql("SELECT secret_password FROM (")
    assert "secret_password" not in str(error.value)
