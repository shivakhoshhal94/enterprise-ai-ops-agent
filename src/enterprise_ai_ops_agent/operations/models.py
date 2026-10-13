"""Operational metadata is deliberately separate from historical Olist metadata."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from enterprise_ai_ops_agent.db.models import Order


class OperationsBase(DeclarativeBase):
    pass


class OrderReviewCase(OperationsBase):
    __tablename__ = "order_review_cases"
    __table_args__ = (
        CheckConstraint("priority IN ('low', 'medium', 'high')", name="ck_case_priority"),
        CheckConstraint("status = 'open'", name="ck_case_status"),
        CheckConstraint("length(trim(reason)) > 0", name="ck_case_reason"),
        CheckConstraint("length(trim(idempotency_key)) > 0", name="ck_case_key"),
        {"schema": "operations"},
    )

    case_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey(Order.order_id), index=True)
    reason: Mapped[str] = mapped_column(String(1000))
    priority: Mapped[str] = mapped_column(String(6))
    status: Mapped[str] = mapped_column(String(4))
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(100))


class CaseEvent(OperationsBase):
    __tablename__ = "case_events"
    __table_args__ = (
        CheckConstraint("event_type = 'case_created'", name="ck_event_type"),
        {"schema": "operations"},
    )

    event_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    case_id: Mapped[UUID] = mapped_column(ForeignKey("operations.order_review_cases.case_id"), index=True)
    event_type: Mapped[str] = mapped_column(String(12))
    actor: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
