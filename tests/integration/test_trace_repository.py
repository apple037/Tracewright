import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg.errors import CheckViolation
from psycopg.types.json import Jsonb

from agent_flow.repositories.conversations import PostgresConversationRepository


MIGRATION = Path("migrations/versions/0001_bootstrap_runtime.py")


def test_bootstrap_migration_declares_required_storage() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    for schema in ("runtime", "observability", "rag", "notification"):
        assert f'CREATE SCHEMA IF NOT EXISTS {schema}' in source
    for table in (
        "conversations",
        "turns",
        "jobs",
        "traces",
        "spans",
        "events",
        "documents",
        "chunks",
        "outbox",
    ):
        assert f'"{table}"' in source
    assert "VECTOR(1024)" in source.upper()
    assert "HNSW" not in source.upper()
    for constraint in (
        "ck_traces_status",
        "ck_spans_status",
        "ck_events_status",
        "ck_jobs_status",
        "ck_documents_ingestion_status",
        "ck_outbox_status",
    ):
        assert constraint in source
    assert "ix_events_trace_sequence" not in source


def test_explicit_integration_invocation_requires_database(
    database_requirement_checker,
) -> None:
    assert database_requirement_checker(
        ("tests/integration/test_trace_repository.py",), {}
    )
    assert database_requirement_checker(
        (r"C:\repo\tests\integration\test_trace_repository.py",), {}
    )
    assert database_requirement_checker(("tests",), {"REQUIRE_DB_INTEGRATION": "1"})
    assert not database_requirement_checker(("tests",), {})


@pytest.mark.asyncio
async def test_trace_events_are_monotonic_and_locate_failure(trace_repository):
    trace_id = await trace_repository.start_trace(
        tenant_id="t1", customer_id="c1", session_id="s1"
    )
    span_id = await trace_repository.start_span(
        trace_id, "response_validator", tenant_id="t1"
    )
    event = await trace_repository.append_event(
        trace_id=trace_id,
        span_id=span_id,
        tenant_id="t1",
        event_type="validation.failed",
        component="qwen_judge",
        status="failed",
        error_code="VAL_GROUND_004",
        payload={"field_path": "draft.delivery_date"},
    )
    await trace_repository.finish_trace(
        trace_id, "failed", tenant_id="t1", primary_failure_event_id=event.id
    )

    loaded = await trace_repository.get_trace(trace_id, tenant_id="t1")
    assert loaded is not None
    assert loaded.primary_failure_event_id == event.id
    assert [item.sequence for item in loaded.events] == [1]
    assert loaded.events[0].payload == {"field_path": "draft.delivery_date"}
    assert await trace_repository.get_trace(trace_id, tenant_id="other") is None


@pytest.mark.asyncio
async def test_concurrent_event_appends_have_one_monotonic_sequence(trace_repository):
    trace_id = await trace_repository.start_trace(
        tenant_id="t1", customer_id="c1", session_id="s1"
    )
    span_id = await trace_repository.start_span(trace_id, "model_call", tenant_id="t1")

    events = await asyncio.gather(
        *(
            trace_repository.append_event(
                trace_id=trace_id,
                span_id=span_id,
                tenant_id="t1",
                event_type="model.progress",
                component="qwen",
                status="completed",
                payload={"ordinal": ordinal},
            )
            for ordinal in range(12)
        )
    )

    assert sorted(event.sequence for event in events) == list(range(1, 13))
    loaded = await trace_repository.events_after(
        trace_id, tenant_id="t1", after_sequence=5
    )
    assert [event.sequence for event in loaded] == list(range(6, 13))


@pytest.mark.asyncio
async def test_trace_retry_lineage_is_immutable(trace_repository):
    original = await trace_repository.start_trace(
        tenant_id="t1", customer_id="c1", session_id="s1"
    )
    retry = await trace_repository.start_trace(
        tenant_id="t1",
        customer_id="c1",
        session_id="s1",
        retry_of_trace_id=original,
        retry_initiator="operator:jasper",
        retry_reason="manual_retry",
    )

    loaded = await trace_repository.get_trace(retry, tenant_id="t1")
    assert loaded is not None
    assert loaded.root_trace_id == original
    assert loaded.retry_of_trace_id == original
    assert loaded.retry_sequence == 1


@pytest.mark.asyncio
async def test_manual_retry_reuses_original_context_snapshot(
    postgres_pool, trace_repository
):
    conversations = PostgresConversationRepository(postgres_pool)
    seed_trace = await trace_repository.start_trace(
        tenant_id="t1", customer_id="c1", session_id="s1"
    )
    await trace_repository.finish_trace(seed_trace, "succeeded", tenant_id="t1")
    await conversations.append_turn(
        tenant_id="t1",
        customer_id="c1",
        session_id="s1",
        trace_id=seed_trace,
        customer_text="first question",
        assistant_text="first answer",
        citations=("doc:1",),
    )

    original = await trace_repository.start_trace(
        tenant_id="t1", customer_id="c1", session_id="s1"
    )
    captured = await conversations.get_snapshot(
        tenant_id="t1",
        customer_id="c1",
        session_id="s1",
        trace_id=original,
    )
    await trace_repository.finish_trace(original, "succeeded", tenant_id="t1")
    await conversations.append_turn(
        tenant_id="t1",
        customer_id="c1",
        session_id="s1",
        trace_id=original,
        customer_text="second question",
        assistant_text="second answer",
    )

    retry_snapshot = await conversations.get_retry_snapshot(
        original, tenant_id="t1", customer_id="c1"
    )
    assert retry_snapshot == captured
    assert "second question" not in retry_snapshot.messages


@pytest.mark.asyncio
async def test_turn_append_is_idempotent_by_trace_before_finalization(
    postgres_pool, trace_repository
):
    conversations = PostgresConversationRepository(postgres_pool)
    trace_id = await trace_repository.start_trace(
        tenant_id="t1", customer_id="c1", session_id="s1"
    )

    for _ in range(2):
        await conversations.append_turn(
            tenant_id="t1",
            customer_id="c1",
            session_id="s1",
            trace_id=trace_id,
            customer_text="question",
            assistant_text="answer",
        )
    await trace_repository.finish_trace(trace_id, "succeeded", tenant_id="t1")
    reader_trace = await trace_repository.start_trace(
        tenant_id="t1", customer_id="c1", session_id="s1"
    )
    snapshot = await conversations.get_snapshot(
        tenant_id="t1", customer_id="c1", session_id="s1", trace_id=reader_trace
    )
    assert snapshot.messages == ("question", "answer")


@pytest.mark.asyncio
async def test_trace_finalization_is_terminal_and_cannot_be_clobbered(
    trace_repository,
):
    trace_id = await trace_repository.start_trace(
        tenant_id="t1", customer_id="c1", session_id="s1"
    )
    with pytest.raises(ValueError, match="terminal"):
        await trace_repository.finish_trace(trace_id, "running", tenant_id="t1")
    await trace_repository.finish_trace(trace_id, "succeeded", tenant_id="t1")

    with pytest.raises(ValueError, match="already finalized"):
        await trace_repository.finish_trace(trace_id, "failed", tenant_id="t1")
    with pytest.raises(ValueError, match="does not exist"):
        await trace_repository.finish_trace(uuid4(), "failed", tenant_id="t1")

    loaded = await trace_repository.get_trace(trace_id, tenant_id="t1")
    assert loaded is not None
    assert loaded.status == "succeeded"
    assert loaded.primary_failure_event_id is None


@pytest.mark.asyncio
async def test_finalized_trace_rejects_all_child_mutations_without_state_change(
    postgres_pool, trace_repository
):
    trace_id = await trace_repository.start_trace(
        tenant_id="t1", customer_id="c1", session_id="s1"
    )
    span_id = await trace_repository.start_span(trace_id, "node", tenant_id="t1")
    initial_event = await trace_repository.append_event(
        trace_id=trace_id,
        span_id=span_id,
        tenant_id="t1",
        event_type="node.started",
        component="node",
        status="started",
        payload={},
    )
    await trace_repository.finish_trace(trace_id, "succeeded", tenant_id="t1")

    with pytest.raises(ValueError, match="already finalized"):
        await trace_repository.start_span(trace_id, "late_node", tenant_id="t1")
    with pytest.raises(ValueError, match="already finalized"):
        await trace_repository.append_event(
            trace_id=trace_id,
            span_id=span_id,
            tenant_id="t1",
            event_type="node.completed",
            component="node",
            status="completed",
            payload={},
        )
    with pytest.raises(ValueError, match="already finalized"):
        await trace_repository.finish_span(span_id, "completed", tenant_id="t1")

    loaded = await trace_repository.get_trace(trace_id, tenant_id="t1")
    assert loaded is not None
    assert [span.status for span in loaded.spans] == ["running"]
    assert [event.id for event in loaded.events] == [initial_event.id]
    async with postgres_pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT next_event_sequence FROM observability.traces WHERE id = %s",
            (trace_id,),
        )
        assert (await cursor.fetchone())["next_event_sequence"] == 1


@pytest.mark.asyncio
async def test_finish_span_is_terminal_and_immutable(trace_repository):
    trace_id = await trace_repository.start_trace(
        tenant_id="t1", customer_id="c1", session_id="s1"
    )
    span_id = await trace_repository.start_span(trace_id, "node", tenant_id="t1")

    with pytest.raises(ValueError, match="terminal"):
        await trace_repository.finish_span(span_id, "running", tenant_id="t1")
    await trace_repository.finish_span(span_id, "completed", tenant_id="t1")
    with pytest.raises(ValueError, match="already finished"):
        await trace_repository.finish_span(span_id, "failed", tenant_id="t1")
    with pytest.raises(ValueError, match="does not exist"):
        await trace_repository.finish_span(uuid4(), "failed", tenant_id="t1")

    loaded = await trace_repository.get_trace(trace_id, tenant_id="t1")
    assert loaded is not None
    assert loaded.spans[0].status == "completed"


@pytest.mark.asyncio
async def test_parent_span_must_share_trace_and_tenant(trace_repository):
    trace_id = await trace_repository.start_trace(
        tenant_id="t1", customer_id="c1", session_id="s1"
    )
    other_trace = await trace_repository.start_trace(
        tenant_id="t1", customer_id="c1", session_id="s2"
    )
    other_tenant_trace = await trace_repository.start_trace(
        tenant_id="t2", customer_id="c2", session_id="s3"
    )
    cross_trace_parent = await trace_repository.start_span(
        other_trace, "parent", tenant_id="t1"
    )
    cross_tenant_parent = await trace_repository.start_span(
        other_tenant_trace, "parent", tenant_id="t2"
    )

    with pytest.raises(ValueError, match="parent span must belong"):
        await trace_repository.start_span(
            trace_id,
            "child",
            tenant_id="t1",
            parent_span_id=cross_trace_parent,
        )
    with pytest.raises(ValueError, match="parent span must belong"):
        await trace_repository.start_span(
            trace_id,
            "child",
            tenant_id="t1",
            parent_span_id=cross_tenant_parent,
        )

    loaded = await trace_repository.get_trace(trace_id, tenant_id="t1")
    assert loaded is not None
    assert loaded.spans == ()


@pytest.mark.asyncio
async def test_wrong_tenant_cannot_mutate_trace_and_span(trace_repository):
    trace_id = await trace_repository.start_trace(
        tenant_id="t1", customer_id="c1", session_id="s1"
    )
    other_trace = await trace_repository.start_trace(
        tenant_id="t1", customer_id="c1", session_id="s2"
    )

    with pytest.raises(ValueError, match="does not exist"):
        await trace_repository.start_span(trace_id, "node", tenant_id="other")
    span_id = await trace_repository.start_span(trace_id, "node", tenant_id="t1")
    other_span_id = await trace_repository.start_span(
        other_trace, "other_node", tenant_id="t1"
    )

    with pytest.raises(ValueError, match="does not exist"):
        await trace_repository.append_event(
            trace_id=trace_id,
            span_id=span_id,
            tenant_id="other",
            event_type="node.completed",
            component="node",
            status="completed",
            payload={},
        )
    with pytest.raises(ValueError, match="same trace"):
        await trace_repository.append_event(
            trace_id=trace_id,
            span_id=other_span_id,
            tenant_id="t1",
            event_type="node.completed",
            component="node",
            status="completed",
            payload={},
        )
    with pytest.raises(ValueError, match="does not exist"):
        await trace_repository.finish_span(
            span_id, "completed", tenant_id="other"
        )
    with pytest.raises(ValueError, match="does not exist"):
        await trace_repository.finish_trace(trace_id, "failed", tenant_id="other")

    loaded = await trace_repository.get_trace(trace_id, tenant_id="t1")
    assert loaded is not None
    assert loaded.status == "running"
    assert loaded.events == ()
    assert loaded.spans[0].status == "running"


@pytest.mark.asyncio
async def test_database_rejects_invalid_lifecycle_values(
    postgres_pool, trace_repository
):
    trace_id = await trace_repository.start_trace(
        tenant_id="t1", customer_id="c1", session_id="s1"
    )
    span_id = await trace_repository.start_span(trace_id, "node", tenant_id="t1")
    event = await trace_repository.append_event(
        trace_id=trace_id,
        span_id=span_id,
        tenant_id="t1",
        event_type="custom.event.type",
        component="node",
        status="started",
        payload={},
    )

    invalid_writes = (
        ("UPDATE observability.traces SET status = 'invalid' WHERE id = %s", (trace_id,)),
        ("UPDATE observability.spans SET status = 'invalid' WHERE id = %s", (span_id,)),
        ("UPDATE observability.events SET status = 'invalid' WHERE id = %s", (event.id,)),
        (
            "INSERT INTO runtime.jobs (id, tenant_id, job_type, status, idempotency_key) "
            "VALUES (%s, 't1', 'demo', 'invalid', %s)",
            (uuid4(), f"job-{uuid4()}"),
        ),
        (
            "INSERT INTO rag.documents "
            "(id, tenant_id, source_id, version, checksum, ingestion_status) "
            "VALUES (%s, 't1', 'source', 'v1', 'checksum', 'invalid')",
            (uuid4(),),
        ),
        (
            "INSERT INTO notification.outbox "
            "(id, tenant_id, customer_id, trace_id, idempotency_key, payload, status) "
            "VALUES (%s, 't1', 'c1', %s, %s, %s, 'invalid')",
            (uuid4(), trace_id, f"outbox-{uuid4()}", Jsonb({})),
        ),
    )

    for sql, parameters in invalid_writes:
        async with postgres_pool.connection() as connection:
            with pytest.raises(CheckViolation):
                async with connection.transaction():
                    await connection.execute(sql, parameters)
