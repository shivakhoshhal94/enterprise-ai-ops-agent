"""Create application-owned review cases and creation audit events only."""

from alembic import op
import sqlalchemy as sa

revision = "0001_order_review_cases"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE SCHEMA operations")
    op.create_table(
        "order_review_cases",
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Text(), nullable=False),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column("priority", sa.String(6), nullable=False),
        sa.Column("status", sa.String(4), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.PrimaryKeyConstraint("case_id", name="pk_order_review_cases"),
        sa.ForeignKeyConstraint(["order_id"], ["public.orders.order_id"], name="fk_case_order"),
        sa.UniqueConstraint("idempotency_key", name="uq_case_idempotency_key"),
        sa.CheckConstraint("priority IN ('low', 'medium', 'high')", name="ck_case_priority"),
        sa.CheckConstraint("status = 'open'", name="ck_case_status"),
        sa.CheckConstraint("length(trim(reason)) > 0", name="ck_case_reason"),
        sa.CheckConstraint("length(trim(idempotency_key)) > 0", name="ck_case_key"),
        schema="operations",
    )
    op.create_index("ix_operations_order_review_cases_order_id", "order_review_cases", ["order_id"], schema="operations")
    op.create_table(
        "case_events",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(12), nullable=False),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("event_id", name="pk_case_events"),
        sa.ForeignKeyConstraint(["case_id"], ["operations.order_review_cases.case_id"], name="fk_event_case"),
        sa.CheckConstraint("event_type = 'case_created'", name="ck_event_type"),
        schema="operations",
    )
    op.create_index("ix_operations_case_events_case_id", "case_events", ["case_id"], schema="operations")


def downgrade():
    # Deliberately destructive to operational records; never run as verification.
    op.drop_table("case_events", schema="operations")
    op.drop_table("order_review_cases", schema="operations")
    op.execute("DROP SCHEMA operations")
