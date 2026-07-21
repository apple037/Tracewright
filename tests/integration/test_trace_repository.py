import asyncio
from pathlib import Path

import pytest

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


@pytest.mark.asyncio
async def test_trace_events_are_monotonic_and_locate_failure(trace_repository):
    trace_id = await trace_repository.start_trace(
        tenant_id="t1", customer_id="c1", session_id="s1"
    )
    span_id = await trace_repository.start_span(trace_id, "response_validator")
    event = await trace_repository.append_event(
        trace_id=trace_id,
        span_id=span_id,
        event_type="validation.failed",
        component="qwen_judge",
        status="failed",
        error_code="VAL_GROUND_004",
        payload={"field_path": "draft.delivery_date"},
    )
    await trace_repository.finish_trace(
        trace_id, "failed", primary_failure_event_id=event.id
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
    span_id = await trace_repository.start_span(trace_id, "model_call")

    events = await asyncio.gather(
        *(
            trace_repository.append_event(
                trace_id=trace_id,
                span_id=span_id,
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
    await trace_repository.finish_trace(seed_trace, "succeeded")
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
    await trace_repository.finish_trace(original, "succeeded")
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
async def test_turn_cannot_be_appended_before_trace_finalization(
    postgres_pool, trace_repository
):
    conversations = PostgresConversationRepository(postgres_pool)
    trace_id = await trace_repository.start_trace(
        tenant_id="t1", customer_id="c1", session_id="s1"
    )

    with pytest.raises(ValueError, match="finalized"):
        await conversations.append_turn(
            tenant_id="t1",
            customer_id="c1",
            session_id="s1",
            trace_id=trace_id,
            customer_text="question",
            assistant_text="answer",
        )
