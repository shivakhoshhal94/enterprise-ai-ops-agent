"""Initial Olist schema. Importing this module registers all seven tables."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from enterprise_ai_ops_agent.db.base import Base


class Customer(Base):
    __tablename__ = "customers"

    customer_id: Mapped[str] = mapped_column(Text, primary_key=True)
    customer_unique_id: Mapped[str] = mapped_column(Text)
    customer_zip_code_prefix: Mapped[str] = mapped_column(String(5))
    customer_city: Mapped[str] = mapped_column(Text)
    customer_state: Mapped[str] = mapped_column(String(2))


class Order(Base):
    __tablename__ = "orders"

    order_id: Mapped[str] = mapped_column(Text, primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.customer_id"))
    order_status: Mapped[str] = mapped_column(Text)
    order_purchase_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=False))
    order_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    order_delivered_carrier_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    order_delivered_customer_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    order_estimated_delivery_date: Mapped[datetime] = mapped_column(DateTime(timezone=False))


class Product(Base):
    __tablename__ = "products"

    product_id: Mapped[str] = mapped_column(Text, primary_key=True)
    # Translation coverage is incomplete, so this is deliberately not a foreign key.
    product_category_name: Mapped[str | None] = mapped_column(Text)
    # CSV spelling: product_name_lenght / product_description_lenght.
    product_name_length: Mapped[int | None] = mapped_column(Integer)
    product_description_length: Mapped[int | None] = mapped_column(Integer)
    product_photos_qty: Mapped[int | None] = mapped_column(Integer)
    product_weight_g: Mapped[int | None] = mapped_column(Integer)
    product_length_cm: Mapped[int | None] = mapped_column(Integer)
    product_height_cm: Mapped[int | None] = mapped_column(Integer)
    product_width_cm: Mapped[int | None] = mapped_column(Integer)


class Seller(Base):
    __tablename__ = "sellers"

    seller_id: Mapped[str] = mapped_column(Text, primary_key=True)
    seller_zip_code_prefix: Mapped[str] = mapped_column(String(5))
    seller_city: Mapped[str] = mapped_column(Text)
    seller_state: Mapped[str] = mapped_column(String(2))


class OrderItem(Base):
    __tablename__ = "order_items"

    order_id: Mapped[str] = mapped_column(ForeignKey("orders.order_id"), primary_key=True)
    order_item_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.product_id"))
    seller_id: Mapped[str] = mapped_column(ForeignKey("sellers.seller_id"))
    # Seller handoff deadline, not an actual shipment timestamp.
    shipping_limit_date: Mapped[datetime] = mapped_column(DateTime(timezone=False))
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    freight_value: Mapped[Decimal] = mapped_column(Numeric(12, 2))


class OrderPayment(Base):
    __tablename__ = "order_payments"

    order_id: Mapped[str] = mapped_column(ForeignKey("orders.order_id"), primary_key=True)
    payment_sequential: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    payment_type: Mapped[str] = mapped_column(Text)
    payment_installments: Mapped[int] = mapped_column(Integer)
    payment_value: Mapped[Decimal] = mapped_column(Numeric(12, 2))


class CategoryTranslation(Base):
    __tablename__ = "category_translations"

    product_category_name: Mapped[str] = mapped_column(Text, primary_key=True)
    product_category_name_english: Mapped[str] = mapped_column(Text)
