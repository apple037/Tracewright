"""Add durable turn inputs and leased handoff outbox delivery."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "0002_handoff_outbox"
down_revision = "0001_bootstrap_runtime"
branch_labels = None
depends_on = None

NOW = sa.text("CURRENT_TIMESTAMP")


def upgrade() -> None:
    op.create_table(
        "turn_inputs",
        sa.Column("trace_id", UUID, primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("customer_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("case_id", sa.Text()),
        sa.Column(
            "captured_at", sa.DateTime(timezone=True),
            nullable=False, server_default=NOW,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["trace_id"], ["observability.traces.id"], ondelete="CASCADE"
        ),
        schema="runtime",
    )
    op.create_index(
        "ix_turn_inputs_scope", "turn_inputs",
        ["tenant_id", "customer_id", "session_id"], schema="runtime",
    )
    op.create_index(
        "ix_turn_inputs_expires_at", "turn_inputs", ["expires_at"],
        schema="runtime",
    )

    op.alter_column(
        "outbox", "next_attempt_at", schema="notification",
        existing_type=sa.DateTime(timezone=True), nullable=True,
        existing_server_default=NOW,
    )
    op.add_column(
        "outbox", sa.Column("last_http_status", sa.Integer()),
        schema="notification",
    )
    op.add_column(
        "outbox", sa.Column("lock_owner", sa.Text()), schema="notification",
    )
    op.add_column(
        "outbox", sa.Column("locked_at", sa.DateTime(timezone=True)),
        schema="notification",
    )
    op.add_column(
        "outbox", sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        schema="notification",
    )
    op.add_column(
        "outbox", sa.Column("claim_token", UUID), schema="notification",
    )
    op.add_column(
        "outbox", sa.Column("settlement_retryable", sa.Boolean()),
        schema="notification",
    )
    op.add_column(
        "outbox", sa.Column("settlement_backoff_seconds", sa.Integer()),
        schema="notification",
    )
    op.execute(
        "UPDATE notification.outbox SET status = 'failed', "
        "next_attempt_at = CURRENT_TIMESTAMP WHERE status = 'delivering'"
    )
    op.create_check_constraint(
        "ck_outbox_attempts", "outbox", "attempts >= 0", schema="notification"
    )
    op.create_check_constraint(
        "ck_outbox_settlement_backoff", "outbox",
        "settlement_backoff_seconds IS NULL OR settlement_backoff_seconds >= 0",
        schema="notification",
    )
    op.create_check_constraint(
        "ck_outbox_delivery_claim", "outbox",
        "status <> 'delivering' OR (lock_owner IS NOT NULL AND claim_token IS NOT NULL "
        "AND locked_at IS NOT NULL AND lease_expires_at IS NOT NULL)",
        schema="notification",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_outbox_delivery_claim", "outbox", schema="notification", type_="check"
    )
    op.drop_constraint(
        "ck_outbox_settlement_backoff", "outbox",
        schema="notification", type_="check",
    )
    op.drop_constraint(
        "ck_outbox_attempts", "outbox", schema="notification", type_="check"
    )
    for column in (
        "settlement_backoff_seconds", "settlement_retryable", "claim_token",
        "lease_expires_at", "locked_at", "lock_owner", "last_http_status",
    ):
        op.drop_column("outbox", column, schema="notification")
    op.execute(
        "UPDATE notification.outbox SET next_attempt_at = CURRENT_TIMESTAMP "
        "WHERE next_attempt_at IS NULL"
    )
    op.alter_column(
        "outbox", "next_attempt_at", schema="notification",
        existing_type=sa.DateTime(timezone=True), nullable=False,
        existing_server_default=NOW,
    )
    op.drop_table("turn_inputs", schema="runtime")
