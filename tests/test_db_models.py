import pytest
from sqlalchemy import DateTime, Numeric, String
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import configure_mappers
from sqlalchemy.schema import CreateTable

from enterprise_ai_ops_agent.db.base import Base
from enterprise_ai_ops_agent.db import models  # Registers the schema without a database.


EXPECTED_KEYS = {
    "customers": ["customer_id"],
    "orders": ["order_id"],
    "products": ["product_id"],
    "sellers": ["seller_id"],
    "order_items": ["order_id", "order_item_id"],
    "order_payments": ["order_id", "payment_sequential"],
    "category_translations": ["product_category_name"],
}


def test_seven_tables_and_valid_mappings() -> None:
    assert set(Base.metadata.tables) == set(EXPECTED_KEYS)
    configure_mappers()
    assert models.Customer.__mapper__.local_table is Base.metadata.tables["customers"]


@pytest.mark.parametrize("table_name,keys", EXPECTED_KEYS.items())
def test_primary_keys(table_name: str, keys: list[str]) -> None:
    assert list(Base.metadata.tables[table_name].primary_key.columns.keys()) == keys


def test_exact_foreign_keys() -> None:
    actual = {
        (table.name, fk.parent.name, fk.target_fullname)
        for table in Base.metadata.tables.values()
        for fk in table.foreign_keys
    }
    assert actual == {
        ("orders", "customer_id", "customers.customer_id"),
        ("order_items", "order_id", "orders.order_id"),
        ("order_items", "product_id", "products.product_id"),
        ("order_items", "seller_id", "sellers.seller_id"),
        ("order_payments", "order_id", "orders.order_id"),
    }


@pytest.mark.parametrize("table_name,column_name", [
    ("order_items", "price"), ("order_items", "freight_value"),
    ("order_payments", "payment_value"),
])
def test_exact_money_types(table_name: str, column_name: str) -> None:
    column = Base.metadata.tables[table_name].c[column_name]
    assert isinstance(column.type, Numeric)
    assert (column.type.precision, column.type.scale, column.type.asdecimal) == (12, 2, True)
    assert not column.nullable


@pytest.mark.parametrize("table_name,column_name", [
    ("customers", "customer_zip_code_prefix"), ("sellers", "seller_zip_code_prefix"),
])
def test_postal_prefixes_are_text(table_name: str, column_name: str) -> None:
    column = Base.metadata.tables[table_name].c[column_name]
    assert isinstance(column.type, String)
    assert column.type.length == 5


def test_product_names_and_nullable_attributes() -> None:
    table = Base.metadata.tables["products"]
    assert "product_name_length" in table.c
    assert "product_description_length" in table.c
    assert "product_name_lenght" not in table.c
    assert "product_description_lenght" not in table.c
    assert all(column.nullable for column in table.c if column.name != "product_id")
    assert not table.c.product_category_name.foreign_keys


def test_timestamps_preserve_source_nullability_and_timezone() -> None:
    orders = Base.metadata.tables["orders"]
    expected = {
        "order_purchase_timestamp": False, "order_approved_at": True,
        "order_delivered_carrier_date": True, "order_delivered_customer_date": True,
        "order_estimated_delivery_date": False,
    }
    for name, nullable in expected.items():
        column = orders.c[name]
        assert column.nullable is nullable
        assert isinstance(column.type, DateTime)
        assert column.type.timezone is False
    shipping = Base.metadata.tables["order_items"].c.shipping_limit_date
    assert not shipping.nullable
    assert isinstance(shipping.type, DateTime)
    assert shipping.type.timezone is False


def test_repeat_customer_identity_is_not_unique() -> None:
    column = Base.metadata.tables["customers"].c.customer_unique_id
    assert not column.primary_key
    assert not column.unique


@pytest.mark.parametrize("table_name", EXPECTED_KEYS)
def test_postgresql_ddl_compiles_without_server(table_name: str) -> None:
    ddl = str(CreateTable(Base.metadata.tables[table_name]).compile(dialect=postgresql.dialect()))
    assert f"CREATE TABLE {table_name}" in ddl
