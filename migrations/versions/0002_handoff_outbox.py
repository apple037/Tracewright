"""Converge Task 9/10 storage across same-revision historical drift.

Several development-era schemas were stamped ``0001_bootstrap_runtime``:
the Task 9 schema, an older schema without ``runtime.turn_inputs``, and an
f117-era schema with only the first lease columns.  This forward migration
inspects the actual PostgreSQL schema and adds only what is absent.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "0002_handoff_outbox"
down_revision = "0001_bootstrap_runtime"
branch_labels = None
depends_on = None

NOW = sa.text("CURRENT_TIMESTAMP")


def _inspector():
    return sa.inspect(op.get_bind())


def _outbox_columns() -> dict[str, dict[str, object]]:
    return {
        column["name"]: column
        for column in _inspector().get_columns("outbox", schema="notification")
    }


def _outbox_checks() -> set[str]:
    return {
        constraint["name"]
        for constraint in _inspector().get_check_constraints(
            "outbox", schema="notification"
        )
        if constraint["name"] is not None
    }


def _ensure_turn_inputs() -> None:
    if "turn_inputs" in _inspector().get_table_names(schema="runtime"):
        return
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


def _ensure_outbox_columns() -> None:
    columns = _outbox_columns()
    if not columns["next_attempt_at"]["nullable"]:
        op.alter_column(
            "outbox", "next_attempt_at", schema="notification",
            existing_type=sa.DateTime(timezone=True), nullable=True,
            existing_server_default=NOW,
        )
    additions = {
        "last_http_status": sa.Column("last_http_status", sa.Integer()),
        "lock_owner": sa.Column("lock_owner", sa.Text()),
        "locked_at": sa.Column("locked_at", sa.DateTime(timezone=True)),
        "lease_expires_at": sa.Column(
            "lease_expires_at", sa.DateTime(timezone=True)
        ),
        "claim_token": sa.Column("claim_token", UUID),
        "settlement_retryable": sa.Column("settlement_retryable", sa.Boolean()),
        "settlement_backoff_seconds": sa.Column(
            "settlement_backoff_seconds", sa.Integer()
        ),
    }
    for name, column in additions.items():
        if name not in columns:
            op.add_column("outbox", column, schema="notification")


def _ensure_outbox_constraints() -> None:
    checks = _outbox_checks()
    if "ck_outbox_attempts" not in checks:
        op.create_check_constraint(
            "ck_outbox_attempts", "outbox", "attempts >= 0",
            schema="notification",
        )
    if "ck_outbox_settlement_backoff" not in checks:
        op.create_check_constraint(
            "ck_outbox_settlement_backoff", "outbox",
            "settlement_backoff_seconds IS NULL OR "
            "settlement_backoff_seconds >= 0",
            schema="notification",
        )
    if "ck_outbox_delivery_claim" not in checks:
        op.create_check_constraint(
            "ck_outbox_delivery_claim", "outbox",
            "status <> 'delivering' OR (lock_owner IS NOT NULL "
            "AND claim_token IS NOT NULL AND locked_at IS NOT NULL "
            "AND lease_expires_at IS NOT NULL)",
            schema="notification",
        )


def upgrade() -> None:
    _ensure_turn_inputs()
    _ensure_outbox_columns()
    # Pre-token delivering rows cannot prove ownership. Make them safely due.
    op.execute(
        "UPDATE notification.outbox SET status = 'failed', "
        "next_attempt_at = CURRENT_TIMESTAMP, lock_owner = NULL, "
        "locked_at = NULL, lease_expires_at = NULL, claim_token = NULL "
        "WHERE status = 'delivering' AND claim_token IS NULL"
    )
    _ensure_outbox_constraints()


def downgrade() -> None:
    """Preserve repaired data because 0001 has multiple historical schemas.

    Dropping ``turn_inputs`` or lease/settlement columns could destroy data that
    already belonged to a same-revision 0001 deployment.  Alembic may move the
    version marker back, but the converged schema intentionally remains intact.
    """
