"""Make forgetting a conversation reversible.

Resetting a session's memory used to delete its turns outright, which also
destroyed the transcript with no way back. The rows are now marked instead, so
the reset is still complete from the assistant's point of view — nothing with
`forgotten_at` set is ever loaded — while remaining recoverable.

Retention is unchanged and still deletes these rows at `expires_at`. A forgotten
turn is hidden, not exempt.
"""

from alembic import op
import sqlalchemy as sa


revision = "0004_forgotten_turns"
down_revision = "0003_turn_submissions"
branch_labels = None
depends_on = None


def _has_column() -> bool:
    return "forgotten_at" in {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("turns", schema="runtime")
    }


def upgrade() -> None:
    if _has_column():
        return
    op.add_column(
        "turns",
        sa.Column("forgotten_at", sa.TIMESTAMP(timezone=True), nullable=True),
        schema="runtime",
    )
    # Every read filters on this, and the live ones are the overwhelming
    # majority, so index only the rows a query can return.
    op.create_index(
        "ix_turns_live_session",
        "turns",
        ["tenant_id", "customer_id", "session_id", "created_at"],
        schema="runtime",
        postgresql_where=sa.text("forgotten_at IS NULL"),
    )


def downgrade() -> None:
    if not _has_column():
        return
    op.drop_index("ix_turns_live_session", table_name="turns", schema="runtime")
    op.drop_column("turns", "forgotten_at", schema="runtime")
