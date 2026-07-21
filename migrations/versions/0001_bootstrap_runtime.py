"""Bootstrap runtime, observability, RAG, and notification storage."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.types import UserDefinedType


revision = "0001_bootstrap_runtime"
down_revision = None
branch_labels = None
depends_on = None


class Vector1024(UserDefinedType):
    cache_ok = True

    def get_col_spec(self, **kw: object) -> str:
        return "VECTOR(1024)"


UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())
NOW = sa.text("now()")
EMPTY_JSON = sa.text("'{}'::jsonb")
EMPTY_ARRAY = sa.text("'[]'::jsonb")


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE SCHEMA IF NOT EXISTS runtime")
    op.execute("CREATE SCHEMA IF NOT EXISTS observability")
    op.execute("CREATE SCHEMA IF NOT EXISTS rag")
    op.execute("CREATE SCHEMA IF NOT EXISTS notification")

    op.create_table(
        "traces",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("customer_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="running"),
        sa.Column("terminal_outcome", sa.Text()),
        sa.Column("primary_failure_event_id", sa.BigInteger()),
        sa.Column("root_trace_id", UUID, nullable=False),
        sa.Column("retry_of_trace_id", UUID),
        sa.Column("retry_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retry_initiator", sa.Text()),
        sa.Column("retry_reason", sa.Text()),
        sa.Column("delivery_disposition", sa.Text()),
        sa.Column("next_event_sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["root_trace_id"], ["observability.traces.id"], name="fk_traces_root"
        ),
        sa.ForeignKeyConstraint(
            ["retry_of_trace_id"],
            ["observability.traces.id"],
            name="fk_traces_retry_of",
        ),
        sa.CheckConstraint("retry_sequence >= 0", name="ck_traces_retry_sequence"),
        schema="observability",
    )
    op.create_index("ix_traces_scope", "traces", ["tenant_id", "customer_id", "session_id"], schema="observability")
    op.create_index("ix_traces_expires_at", "traces", ["expires_at"], schema="observability")
    op.create_index("ix_traces_root_retry", "traces", ["root_trace_id", "retry_sequence"], unique=True, schema="observability")

    op.create_table(
        "spans",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("trace_id", UUID, nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("customer_id", sa.Text(), nullable=False),
        sa.Column("parent_span_id", UUID),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="running"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("error_code", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["trace_id"], ["observability.traces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_span_id"], ["observability.spans.id"]),
        sa.CheckConstraint("attempt >= 1", name="ck_spans_attempt"),
        schema="observability",
    )
    op.create_index("ix_spans_trace", "spans", ["trace_id", "created_at"], schema="observability")

    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("trace_id", UUID, nullable=False),
        sa.Column("span_id", UUID),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("customer_id", sa.Text(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("component", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text()),
        sa.Column("payload_schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("payload", JSONB, nullable=False, server_default=EMPTY_JSON),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["trace_id"], ["observability.traces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["span_id"], ["observability.spans.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("trace_id", "sequence", name="uq_events_trace_sequence"),
        sa.CheckConstraint("sequence > 0", name="ck_events_sequence"),
        sa.CheckConstraint("payload_schema_version > 0", name="ck_events_payload_version"),
        schema="observability",
    )
    op.create_index("ix_events_trace_sequence", "events", ["trace_id", "sequence"], schema="observability")
    op.create_index("ix_events_type_status", "events", ["event_type", "status"], schema="observability")
    op.create_index("ix_events_expires_at", "events", ["expires_at"], schema="observability")
    op.create_foreign_key(
        "fk_traces_primary_failure",
        "traces",
        "events",
        ["primary_failure_event_id"],
        ["id"],
        source_schema="observability",
        referent_schema="observability",
    )

    op.create_table(
        "conversations",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("customer_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "customer_id", "session_id", name="uq_conversations_scope"),
        schema="runtime",
    )
    op.create_index("ix_conversations_expires_at", "conversations", ["expires_at"], schema="runtime")
    op.create_table(
        "conversation_snapshots",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("trace_id", UUID, nullable=False, unique=True),
        sa.Column("conversation_id", UUID, nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("customer_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("messages", JSONB, nullable=False, server_default=EMPTY_ARRAY),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.ForeignKeyConstraint(["trace_id"], ["observability.traces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["runtime.conversations.id"], ondelete="CASCADE"),
        schema="runtime",
    )
    op.create_index("ix_snapshots_scope", "conversation_snapshots", ["tenant_id", "customer_id", "session_id"], schema="runtime")
    op.create_table(
        "turns",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("conversation_id", UUID, nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("customer_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("trace_id", UUID, nullable=False, unique=True),
        sa.Column("customer_text", sa.Text(), nullable=False),
        sa.Column("assistant_text", sa.Text(), nullable=False),
        sa.Column("citations", JSONB, nullable=False, server_default=EMPTY_ARRAY),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["runtime.conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["trace_id"], ["observability.traces.id"]),
        schema="runtime",
    )
    op.create_index("ix_turns_scope_created", "turns", ["tenant_id", "customer_id", "session_id", "created_at"], schema="runtime")
    op.create_index("ix_turns_expires_at", "turns", ["expires_at"], schema="runtime")
    op.create_table(
        "jobs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("customer_id", sa.Text()),
        sa.Column("job_type", sa.Text(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("payload_schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("payload", JSONB, nullable=False, server_default=EMPTY_JSON),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lock_owner", sa.Text()),
        sa.Column("locked_at", sa.DateTime(timezone=True)),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("last_error_code", sa.Text()),
        sa.Column("last_error_component", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_jobs_idempotency"),
        schema="runtime",
    )
    op.create_index("ix_jobs_claim", "jobs", ["status", "available_at", "priority"], schema="runtime")

    op.create_table(
        "documents",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("customer_id", sa.Text()),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("checksum", sa.Text(), nullable=False),
        sa.Column("access_metadata", JSONB, nullable=False, server_default=EMPTY_JSON),
        sa.Column("ingestion_status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("effective_at", sa.DateTime(timezone=True)),
        sa.Column("valid_until", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.UniqueConstraint("tenant_id", "source_id", "version", name="uq_documents_source_version"),
        schema="rag",
    )
    op.create_table(
        "chunks",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("document_id", UUID, nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("customer_id", sa.Text()),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata", JSONB, nullable=False, server_default=EMPTY_JSON),
        sa.Column("embedding", Vector1024(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.ForeignKeyConstraint(["document_id"], ["rag.documents.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("document_id", "ordinal", name="uq_chunks_document_ordinal"),
        schema="rag",
    )
    op.create_index("ix_chunks_scope", "chunks", ["tenant_id", "document_id"], schema="rag")

    op.create_table(
        "outbox",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("customer_id", sa.Text(), nullable=False),
        sa.Column("trace_id", UUID, nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("payload_schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("last_error_code", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["trace_id"], ["observability.traces.id"]),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_outbox_idempotency"),
        schema="notification",
    )
    op.create_index("ix_outbox_claim", "outbox", ["status", "next_attempt_at", "created_at"], schema="notification")


def downgrade() -> None:
    op.drop_table("outbox", schema="notification")
    op.drop_table("chunks", schema="rag")
    op.drop_table("documents", schema="rag")
    op.drop_table("jobs", schema="runtime")
    op.drop_table("turns", schema="runtime")
    op.drop_table("conversation_snapshots", schema="runtime")
    op.drop_table("conversations", schema="runtime")
    op.drop_constraint("fk_traces_primary_failure", "traces", schema="observability", type_="foreignkey")
    op.drop_table("events", schema="observability")
    op.drop_table("spans", schema="observability")
    op.drop_table("traces", schema="observability")
    for schema in ("notification", "rag", "observability", "runtime"):
        op.execute(f"DROP SCHEMA IF EXISTS {schema}")
