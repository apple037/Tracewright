"""Add channel-neutral turn submission storage.

The upgrade inspects the deployed schema so databases with development-era
partial submission columns converge without duplicate-column, constraint, or
index failures.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "0003_turn_submissions"
down_revision = "0002_handoff_outbox"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _column_names(table: str, schema: str) -> set[str]:
    return {column["name"] for column in _inspector().get_columns(table, schema=schema)}


def _ensure_trace_status_constraint() -> None:
    allowed_statuses = ("queued", "running", "succeeded", "failed")
    constraints = {
        constraint["name"]: constraint
        for constraint in _inspector().get_check_constraints(
            "traces", schema="observability"
        )
        if constraint["name"] is not None
    }
    current = constraints.get("ck_traces_status")
    if current is not None and not all(
        f"'{status}'" in current["sqltext"] for status in allowed_statuses
    ):
        op.drop_constraint(
            "ck_traces_status",
            "traces",
            schema="observability",
            type_="check",
        )
        current = None
    if current is None:
        op.create_check_constraint(
            "ck_traces_status",
            "traces",
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            schema="observability",
        )


def _ensure_trace_columns() -> None:
    columns = _column_names("traces", "observability")
    additions = {
        "channel": sa.Column("channel", sa.Text(), nullable=True),
        "external_message_id": sa.Column(
            "external_message_id", sa.Text(), nullable=True
        ),
    }
    for name, column in additions.items():
        if name not in columns:
            op.add_column("traces", column, schema="observability")


def _jobs_foreign_keys() -> list[dict[str, object]]:
    return _inspector().get_foreign_keys("jobs", schema="runtime")


def _ensure_job_columns() -> None:
    columns = _column_names("jobs", "runtime")
    additions = {
        "trace_id": sa.Column("trace_id", UUID, nullable=True),
        "result": sa.Column("result", JSONB),
        "finished_at": sa.Column("finished_at", sa.DateTime(timezone=True)),
        "lease_expires_at": sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        "claim_token": sa.Column("claim_token", UUID),
    }
    for name, column in additions.items():
        if name not in columns:
            op.add_column("jobs", column, schema="runtime")

    has_trace_foreign_key = any(
        foreign_key["constrained_columns"] == ["trace_id"]
        and foreign_key["referred_schema"] == "observability"
        and foreign_key["referred_table"] == "traces"
        and foreign_key["referred_columns"] == ["id"]
        and foreign_key.get("options", {}).get("ondelete") == "CASCADE"
        for foreign_key in _jobs_foreign_keys()
    )
    if not has_trace_foreign_key:
        op.create_foreign_key(
            "fk_jobs_trace",
            "jobs",
            "traces",
            ["trace_id"],
            ["id"],
            source_schema="runtime",
            referent_schema="observability",
            ondelete="CASCADE",
        )

    indexes = {
        index["name"] for index in _inspector().get_indexes("jobs", schema="runtime")
    }
    if "ix_jobs_trace" not in indexes:
        op.create_index(
            "ix_jobs_trace",
            "jobs",
            ["trace_id"],
            schema="runtime",
        )


def upgrade() -> None:
    _ensure_trace_status_constraint()
    _ensure_trace_columns()
    _ensure_job_columns()


def downgrade() -> None:
    """Preserve converged, potentially data-bearing submission storage."""
