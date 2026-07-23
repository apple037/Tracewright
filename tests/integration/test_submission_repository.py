import asyncio
import json
from uuid import uuid4

import pytest
import pytest_asyncio
from psycopg.types.json import Jsonb

from agent_flow.auth import AuthorizedCustomerContext
from agent_flow.contracts import InboundMessage, SubmissionResult
from agent_flow.repositories.submissions import PostgresSubmissionRepository
from agent_flow.repositories.traces import PostgresTraceRepository


def inbound_message(**overrides) -> InboundMessage:
    values = {
        "channel": "console",
        "external_message_id": f"message-{uuid4()}",
        "session_id": "session-1",
        "text": "Where is my order?",
        "idempotency_key": f"submission-{uuid4()}",
        "metadata": {"source": "trace-console"},
    }
    values.update(overrides)
    return InboundMessage.model_validate(values)


@pytest.fixture
def customer_context() -> AuthorizedCustomerContext:
    return AuthorizedCustomerContext(
        subject_id="user-1", tenant_id="t1", customer_id="c1"
    )


@pytest.fixture
def other_tenant_context() -> AuthorizedCustomerContext:
    return AuthorizedCustomerContext(
        subject_id="user-2", tenant_id="t2", customer_id="c1"
    )


@pytest_asyncio.fixture
async def submissions(postgres_pool):
    traces = PostgresTraceRepository(postgres_pool)
    await traces.clear_test_data()
    repository = PostgresSubmissionRepository(postgres_pool)
    yield repository
    await traces.clear_test_data()


@pytest_asyncio.fixture
async def expire_job_lease(postgres_pool):
    async def expire(submission_id):
        async with postgres_pool.connection() as connection:
            await connection.execute(
                """
                UPDATE runtime.jobs
                SET lease_expires_at = now() - interval '1 second'
                WHERE id = %s
                """,
                (submission_id,),
            )

    return expire


async def trace_status(postgres_pool, trace_id):
    async with postgres_pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT status FROM observability.traces WHERE id = %s", (trace_id,)
        )
        return (await cursor.fetchone())["status"]


async def job_count(postgres_pool, idempotency_key, *, tenant_id):
    async with postgres_pool.connection() as connection:
        cursor = await connection.execute(
            """
            SELECT count(*) AS count
            FROM runtime.jobs
            WHERE tenant_id = %s AND idempotency_key = %s
            """,
            (tenant_id, idempotency_key),
        )
        return (await cursor.fetchone())["count"]


def completed_submission_result(record) -> SubmissionResult:
    return SubmissionResult(
        submission_id=record.id,
        trace_id=record.trace_id,
        status="completed",
        text="Your order is in transit.",
        citations=("tool-result-1",),
    )


@pytest.mark.asyncio
async def test_enqueue_atomically_reserves_trace_and_deduplicates(
    submissions, postgres_pool, customer_context
):
    message = inbound_message(idempotency_key="same-key")
    first = await submissions.enqueue(customer_context, message)
    second = await submissions.enqueue(customer_context, message)

    assert second == first
    assert first.status == "queued"
    assert await trace_status(postgres_pool, first.trace_id) == "queued"
    assert await job_count(postgres_pool, "same-key", tenant_id="t1") == 1


@pytest.mark.asyncio
async def test_same_key_in_another_tenant_is_distinct(
    submissions, customer_context, other_tenant_context
):
    first = await submissions.enqueue(
        customer_context, inbound_message(idempotency_key="shared")
    )
    second = await submissions.enqueue(
        other_tenant_context, inbound_message(idempotency_key="shared")
    )

    assert first.id != second.id


@pytest.mark.asyncio
async def test_scoped_get_hides_other_customer(submissions, customer_context):
    record = await submissions.enqueue(customer_context, inbound_message())

    assert (
        await submissions.get(record.id, tenant_id="t1", customer_id="c2") is None
    )


@pytest.mark.asyncio
async def test_enqueue_maps_validated_channel_fields_without_identity_payload(
    submissions, customer_context, postgres_pool
):
    record = await submissions.enqueue(
        customer_context,
        inbound_message(
            channel="web_chat-v2",
            external_message_id="external-42",
            idempotency_key="mapped-message",
        ),
    )

    trace = await PostgresTraceRepository(postgres_pool).get_trace(
        record.trace_id, tenant_id="t1", customer_id="c1"
    )
    assert trace is not None
    assert (trace.channel, trace.external_message_id) == (
        "web_chat-v2",
        "external-42",
    )
    serialized = json.dumps(record.payload)
    assert "subject_id" not in serialized
    assert "bearer" not in serialized.lower()


@pytest.mark.asyncio
async def test_enqueue_rejects_same_tenant_key_with_different_payload(
    submissions, customer_context
):
    await submissions.enqueue(
        customer_context, inbound_message(idempotency_key="conflicting-key")
    )

    with pytest.raises(ValueError, match="submission idempotency conflict"):
        await submissions.enqueue(
            customer_context,
            inbound_message(
                idempotency_key="conflicting-key",
                text="This is a different immutable request.",
            ),
        )


@pytest.mark.asyncio
async def test_enqueue_rejects_key_owned_by_another_job_type(
    submissions, customer_context, postgres_pool
):
    async with postgres_pool.connection() as connection:
        await connection.execute(
            """
            INSERT INTO runtime.jobs (
                id, tenant_id, customer_id, job_type, payload, idempotency_key
            ) VALUES (%s, 't1', 'c1', 'maintenance', %s, 'shared-job-key')
            """,
            (uuid4(), Jsonb({"safe": "maintenance"})),
        )

    with pytest.raises(ValueError, match="submission idempotency conflict"):
        await submissions.enqueue(
            customer_context,
            inbound_message(idempotency_key="shared-job-key"),
        )


@pytest.mark.asyncio
async def test_enqueue_classifies_concurrent_other_job_collision_without_orphan_trace(
    submissions, customer_context, postgres_pool
):
    lock_class, lock_object = 2_147_480_000, 3
    async with postgres_pool.connection() as connection:
        await connection.execute(
            """
            CREATE OR REPLACE FUNCTION runtime.block_turn_job_insert_for_test()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF NEW.job_type = 'turn' THEN
                    PERFORM pg_advisory_xact_lock(2147480000, 3);
                END IF;
                RETURN NEW;
            END
            $$
            """
        )
        await connection.execute(
            """
            CREATE TRIGGER block_turn_job_insert_for_test
            BEFORE INSERT ON runtime.jobs
            FOR EACH ROW
            EXECUTE FUNCTION runtime.block_turn_job_insert_for_test()
            """
        )

    enqueue_task = None
    try:
        async with postgres_pool.connection() as collision:
            async with collision.transaction():
                await collision.execute(
                    "SELECT pg_advisory_xact_lock(%s, %s)",
                    (lock_class, lock_object),
                )
                enqueue_task = asyncio.create_task(
                    submissions.enqueue(
                        customer_context,
                        inbound_message(idempotency_key="concurrent-job-key"),
                    )
                )
                for _ in range(200):
                    cursor = await collision.execute(
                        """
                        SELECT count(*) AS count
                        FROM pg_locks
                        WHERE locktype = 'advisory'
                          AND classid = %s AND objid = %s
                          AND NOT granted
                        """,
                        (lock_class, lock_object),
                    )
                    if (await cursor.fetchone())["count"] == 1:
                        break
                    await asyncio.sleep(0.01)
                else:
                    pytest.fail("submission insert did not reach the lock barrier")
                await collision.execute(
                    """
                    INSERT INTO runtime.jobs (
                        id, tenant_id, customer_id, job_type,
                        payload, idempotency_key
                    ) VALUES (
                        %s, 't1', 'c1', 'maintenance',
                        %s, 'concurrent-job-key'
                    )
                    """,
                    (uuid4(), Jsonb({"safe": "maintenance"})),
                )

        with pytest.raises(ValueError, match="submission idempotency conflict"):
            await enqueue_task
        async with postgres_pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT count(*) AS count
                FROM observability.traces
                WHERE tenant_id = 't1' AND customer_id = 'c1'
                  AND external_message_id IS NOT NULL
                """
            )
            assert (await cursor.fetchone())["count"] == 0
    finally:
        if enqueue_task is not None and not enqueue_task.done():
            enqueue_task.cancel()
        async with postgres_pool.connection() as connection:
            await connection.execute(
                """
                DROP TRIGGER IF EXISTS block_turn_job_insert_for_test
                ON runtime.jobs
                """
            )
            await connection.execute(
                """
                DROP FUNCTION IF EXISTS
                runtime.block_turn_job_insert_for_test()
                """
            )


@pytest.mark.asyncio
async def test_claim_uses_unique_token_and_skip_locked(
    submissions, postgres_pool, customer_context
):
    queued = await submissions.enqueue(customer_context, inbound_message())
    other_repository = PostgresSubmissionRepository(postgres_pool)

    first, second = await asyncio.gather(
        submissions.claim(
            owner="worker-a", limit=1, lease_seconds=30, max_attempts=3
        ),
        other_repository.claim(
            owner="worker-b", limit=1, lease_seconds=30, max_attempts=3
        ),
    )

    claimed = first or second
    skipped = second if first else first
    assert [item.id for item in claimed] == [queued.id]
    assert claimed[0].claim_token is not None
    assert skipped == ()


@pytest.mark.asyncio
async def test_claim_skips_row_locked_by_another_transaction(
    submissions, postgres_pool, customer_context
):
    queued = await submissions.enqueue(customer_context, inbound_message())
    other_repository = PostgresSubmissionRepository(postgres_pool)

    async with postgres_pool.connection() as connection:
        async with connection.transaction():
            await connection.execute(
                "SELECT id FROM runtime.jobs WHERE id = %s FOR UPDATE",
                (queued.id,),
            )
            assert (
                await other_repository.claim(
                    owner="worker-b",
                    limit=1,
                    lease_seconds=30,
                    max_attempts=3,
                )
                == ()
            )


@pytest.mark.asyncio
async def test_heartbeat_requires_matching_owner_and_claim_token(
    submissions, customer_context
):
    await submissions.enqueue(customer_context, inbound_message())
    claimed = (
        await submissions.claim(
            owner="worker-a", limit=1, lease_seconds=30, max_attempts=3
        )
    )[0]

    assert not await submissions.heartbeat(
        claimed.id,
        owner="worker-b",
        claim_token=claimed.claim_token,
        lease_seconds=30,
    )
    assert await submissions.heartbeat(
        claimed.id,
        owner="worker-a",
        claim_token=claimed.claim_token,
        lease_seconds=30,
    )


@pytest.mark.asyncio
async def test_complete_is_idempotent_for_identical_safe_result(
    submissions, customer_context
):
    await submissions.enqueue(customer_context, inbound_message())
    claimed = (
        await submissions.claim(
            owner="worker-a", limit=1, lease_seconds=30, max_attempts=3
        )
    )[0]
    result = completed_submission_result(claimed)

    assert await submissions.complete(
        claimed.id,
        owner="worker-a",
        claim_token=claimed.claim_token,
        result=result,
    )
    assert await submissions.complete(
        claimed.id,
        owner="worker-a",
        claim_token=claimed.claim_token,
        result=result,
    )
    stored = await submissions.get(
        claimed.id, tenant_id=claimed.tenant_id, customer_id=claimed.customer_id
    )
    assert stored is not None
    assert stored.to_result() == result


@pytest.mark.asyncio
async def test_fail_stores_only_error_code_and_component(
    submissions, customer_context
):
    await submissions.enqueue(customer_context, inbound_message())
    claimed = (
        await submissions.claim(
            owner="worker-a", limit=1, lease_seconds=30, max_attempts=3
        )
    )[0]

    await submissions.fail(
        claimed.id,
        owner="worker-a",
        claim_token=claimed.claim_token,
        error_code="MODEL_TIMEOUT",
        error_component="response_generator",
        retryable=False,
        max_attempts=3,
        backoff_seconds=1,
    )
    stored = await submissions.get(
        claimed.id, tenant_id=claimed.tenant_id, customer_id=claimed.customer_id
    )
    assert stored is not None
    assert stored.last_error_code == "MODEL_TIMEOUT"
    assert stored.last_error_component == "response_generator"
    assert "exception" not in json.dumps(stored.result or {})


@pytest.mark.asyncio
async def test_expired_running_claim_is_recoverable(
    submissions, customer_context, expire_job_lease
):
    await submissions.enqueue(customer_context, inbound_message())
    first = (
        await submissions.claim(
            owner="worker-a", limit=1, lease_seconds=30, max_attempts=3
        )
    )[0]
    await expire_job_lease(first.id)

    recovered = await submissions.claim(
        owner="worker-b", limit=1, lease_seconds=30, max_attempts=3
    )

    assert recovered[0].id == first.id
    assert recovered[0].attempts == 2
    assert recovered[0].claim_token != first.claim_token


@pytest.mark.asyncio
async def test_claim_finalizes_running_trace_when_attempts_are_exhausted(
    submissions, customer_context, postgres_pool
):
    queued = await submissions.enqueue(customer_context, inbound_message())
    traces = PostgresTraceRepository(postgres_pool)
    await traces.activate_trace(
        queued.trace_id, tenant_id="t1", expected_retry_of=None
    )
    async with postgres_pool.connection() as connection:
        await connection.execute(
            """
            UPDATE runtime.jobs
            SET status = 'running', attempts = 3,
                lease_expires_at = now() - interval '1 second'
            WHERE id = %s
            """,
            (queued.id,),
        )

    assert (
        await submissions.claim(
            owner="worker-b", limit=1, lease_seconds=30, max_attempts=3
        )
        == ()
    )
    stored = await submissions.get(
        queued.id, tenant_id="t1", customer_id="c1"
    )
    trace = await traces.get_trace(queued.trace_id, tenant_id="t1")
    assert stored is not None
    assert trace is not None
    assert stored.status == "failed"
    assert trace.status == "failed"
    assert [event.error_code for event in trace.events] == ["ATTEMPTS_EXHAUSTED"]


@pytest.mark.asyncio
async def test_recover_expired_claim_retries_running_trace_atomically(
    submissions, customer_context, expire_job_lease, postgres_pool
):
    queued = await submissions.enqueue(customer_context, inbound_message())
    traces = PostgresTraceRepository(postgres_pool)
    await traces.activate_trace(
        queued.trace_id, tenant_id="t1", expected_retry_of=None
    )
    first = (
        await submissions.claim(
            owner="worker-a", limit=1, lease_seconds=30, max_attempts=3
        )
    )[0]
    await expire_job_lease(first.id)
    second = (
        await submissions.claim(
            owner="worker-b", limit=1, lease_seconds=30, max_attempts=3
        )
    )[0]

    replacement = await submissions.recover_expired_claim(
        second.id,
        owner="worker-b",
        claim_token=second.claim_token,
    )

    assert replacement.id == first.id
    assert replacement.trace_id != first.trace_id
    assert replacement.payload == first.payload
    assert replacement.status == "running"
    failed_trace = await traces.get_trace(first.trace_id, tenant_id="t1")
    retry_trace = await traces.get_trace(replacement.trace_id, tenant_id="t1")
    assert failed_trace is not None
    assert retry_trace is not None
    assert failed_trace.status == "failed"
    assert failed_trace.events[0].error_code == "WORKER_LEASE_EXPIRED"
    assert retry_trace.status == "queued"
    assert retry_trace.retry_of_trace_id == first.trace_id
    assert retry_trace.root_trace_id == first.trace_id
    assert retry_trace.retry_sequence == 1


@pytest.mark.asyncio
async def test_activate_trace_is_scoped_lineage_checked_and_idempotent(
    submissions, customer_context, postgres_pool
):
    queued = await submissions.enqueue(customer_context, inbound_message())
    traces = PostgresTraceRepository(postgres_pool)

    with pytest.raises(ValueError):
        await traces.activate_trace(
            queued.trace_id, tenant_id="other", expected_retry_of=None
        )
    with pytest.raises(ValueError):
        await traces.activate_trace(
            queued.trace_id, tenant_id="t1", expected_retry_of=uuid4()
        )
    await traces.activate_trace(
        queued.trace_id, tenant_id="t1", expected_retry_of=None
    )
    await traces.activate_trace(
        queued.trace_id, tenant_id="t1", expected_retry_of=None
    )

    assert await trace_status(postgres_pool, queued.trace_id) == "running"
    await traces.finish_trace(queued.trace_id, "failed", tenant_id="t1")
    with pytest.raises(ValueError, match="trace cannot be activated"):
        await traces.activate_trace(
            queued.trace_id, tenant_id="t1", expected_retry_of=None
        )
