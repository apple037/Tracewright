from __future__ import annotations

from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from agent_flow.repositories.retention import PostgresRetentionRepository
from agent_flow.worker import RetentionWorker


TENANT = "task11-retention-test"


async def _clear(pool):
    async with pool.connection() as connection:
        async with connection.transaction():
            await connection.execute(
                "DELETE FROM notification.outbox WHERE tenant_id = %s", (TENANT,)
            )
            await connection.execute(
                "DELETE FROM runtime.turns WHERE tenant_id = %s", (TENANT,)
            )
            await connection.execute(
                "DELETE FROM runtime.turn_inputs WHERE tenant_id = %s", (TENANT,)
            )
            await connection.execute(
                "DELETE FROM runtime.conversation_snapshots WHERE tenant_id = %s",
                (TENANT,),
            )
            await connection.execute(
                "DELETE FROM observability.traces WHERE tenant_id = %s", (TENANT,)
            )
            await connection.execute(
                "DELETE FROM runtime.conversations WHERE tenant_id = %s", (TENANT,)
            )


async def _insert_trace(connection, *, expired: bool):
    trace_id = uuid4()
    await connection.execute(
        """
        INSERT INTO observability.traces (
            id, tenant_id, customer_id, session_id, root_trace_id,
            status, finished_at, expires_at
        ) VALUES (%s, %s, 'customer', %s, %s, 'succeeded', now(),
                  now() + CASE WHEN %s THEN interval '-1 day' ELSE interval '1 day' END)
        """,
        (trace_id, TENANT, str(trace_id), trace_id, expired),
    )
    return trace_id


@pytest.fixture(autouse=True)
async def retention_rows(postgres_pool):
    await _clear(postgres_pool)
    yield
    await _clear(postgres_pool)


@pytest.mark.asyncio
async def test_retention_deletes_expired_raw_text_and_only_empty_expired_conversation(
    postgres_pool,
):
    old_conversation, fresh_conversation, input_conversation = uuid4(), uuid4(), uuid4()
    old_trace, fresh_trace = None, None
    async with postgres_pool.connection() as connection:
        async with connection.transaction():
            old_trace = await _insert_trace(connection, expired=False)
            fresh_trace = await _insert_trace(connection, expired=False)
            await connection.execute(
                """
                INSERT INTO runtime.conversations
                    (id, tenant_id, customer_id, session_id, expires_at)
                VALUES
                    (%s, %s, 'customer', 'old', now() - interval '1 day'),
                    (%s, %s, 'customer', 'fresh', now() + interval '1 day'),
                    (%s, %s, 'customer', 'input-only', now() - interval '1 day')
                """,
                (
                    old_conversation, TENANT,
                    fresh_conversation, TENANT,
                    input_conversation, TENANT,
                ),
            )
            await connection.execute(
                """
                INSERT INTO runtime.turns (
                    conversation_id, tenant_id, customer_id, session_id, trace_id,
                    customer_text, assistant_text, expires_at
                ) VALUES
                    (%s, %s, 'customer', 'old', %s, 'raw old', 'raw old',
                     now() - interval '1 second'),
                    (%s, %s, 'customer', 'fresh', %s, 'raw fresh', 'raw fresh',
                     now() + interval '1 day')
                """,
                (
                    old_conversation, TENANT, old_trace,
                    fresh_conversation, TENANT, fresh_trace,
                ),
            )
            await connection.execute(
                """
                INSERT INTO runtime.turn_inputs (
                    trace_id, tenant_id, customer_id, session_id, message, expires_at
                ) VALUES
                    (%s, %s, 'customer', 'old', 'input old', now() - interval '1 second'),
                    (%s, %s, 'customer', 'fresh', 'input fresh', now() + interval '1 day'),
                    (%s, %s, 'customer', 'input-only', 'input keeps conversation',
                     now() + interval '1 day')
                """,
                (
                    old_trace, TENANT,
                    fresh_trace, TENANT,
                    await _insert_trace(connection, expired=False), TENANT,
                ),
            )
            await connection.execute(
                """
                INSERT INTO runtime.conversation_snapshots (
                    id, trace_id, conversation_id, tenant_id, customer_id,
                    session_id, messages, captured_at
                ) VALUES
                    (%s, %s, %s, %s, 'customer', 'old', %s,
                     now() - interval '31 days'),
                    (%s, %s, %s, %s, 'customer', 'fresh', %s,
                     now() - interval '29 days')
                """,
                (
                    uuid4(), old_trace, old_conversation, TENANT,
                    Jsonb([{"role": "user", "content": "old"}]),
                    uuid4(), fresh_trace, fresh_conversation, TENANT,
                    Jsonb([{"role": "user", "content": "fresh"}]),
                ),
            )

    result = await RetentionWorker(
        PostgresRetentionRepository(postgres_pool), batch_size=10, tenant_id=TENANT
    ).run_once()

    assert result.turns_deleted == 1
    assert result.turn_inputs_deleted == 1
    assert result.snapshots_deleted == 1
    assert result.conversations_deleted == 1
    async with postgres_pool.connection() as connection:
        cursor = await connection.execute(
            """
            SELECT
              (SELECT count(*) FROM runtime.turns WHERE tenant_id = %s) AS turns,
              (SELECT count(*) FROM runtime.turn_inputs WHERE tenant_id = %s) AS inputs,
              (SELECT count(*) FROM runtime.conversation_snapshots
               WHERE tenant_id = %s) AS snapshots,
              (SELECT count(*) FROM runtime.conversations
               WHERE tenant_id = %s) AS conversations
            """,
            (TENANT, TENANT, TENANT, TENANT),
        )
        assert await cursor.fetchone() == {
            "turns": 1, "inputs": 2, "snapshots": 1, "conversations": 2
        }


@pytest.mark.asyncio
async def test_retention_deletes_terminal_outbox_before_trace_and_defers_active_outbox(
    postgres_pool,
):
    async with postgres_pool.connection() as connection:
        async with connection.transaction():
            terminal_trace = await _insert_trace(connection, expired=True)
            active_trace = await _insert_trace(connection, expired=True)
            fresh_trace = await _insert_trace(connection, expired=False)
            await connection.execute(
                """
                INSERT INTO notification.outbox (
                    id, tenant_id, customer_id, trace_id, idempotency_key,
                    payload, status, next_attempt_at
                ) VALUES
                    (%s, %s, 'customer', %s, %s, '{}'::jsonb, 'delivered', NULL),
                    (%s, %s, 'customer', %s, %s, '{}'::jsonb, 'failed', NULL),
                    (%s, %s, 'customer', %s, %s, '{}'::jsonb, 'queued', now()),
                    (%s, %s, 'customer', %s, %s, '{}'::jsonb, 'failed', now())
                """,
                (
                    uuid4(), TENANT, terminal_trace, f"terminal-{terminal_trace}",
                    uuid4(), TENANT, terminal_trace, f"permanent-{terminal_trace}",
                    uuid4(), TENANT, active_trace, f"active-{active_trace}",
                    uuid4(), TENANT, fresh_trace, f"fresh-{fresh_trace}",
                ),
            )

    result = await PostgresRetentionRepository(postgres_pool).cleanup_batch(
        limit=10, tenant_id=TENANT
    )

    assert result.terminal_outbox_deleted == 2
    assert result.traces_deleted == 1
    assert result.traces_deferred_active_outbox == 1
    async with postgres_pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT id FROM observability.traces WHERE tenant_id = %s ORDER BY id",
            (TENANT,),
        )
        remaining = {row["id"] for row in await cursor.fetchall()}
    assert remaining == {active_trace, fresh_trace}


@pytest.mark.asyncio
async def test_retention_is_bounded_tenant_scoped_and_rejects_invalid_limits(
    postgres_pool,
):
    other_tenant = f"{TENANT}-other"
    async with postgres_pool.connection() as connection:
        async with connection.transaction():
            for _ in range(3):
                await _insert_trace(connection, expired=True)
            trace_id = uuid4()
            await connection.execute(
                """
                INSERT INTO observability.traces (
                    id, tenant_id, customer_id, session_id, root_trace_id,
                    status, finished_at, expires_at
                ) VALUES (%s, %s, 'customer', 'other', %s, 'succeeded', now(),
                          now() - interval '1 day')
                """,
                (trace_id, other_tenant, trace_id),
            )

    repository = PostgresRetentionRepository(postgres_pool)
    first = await repository.cleanup_batch(limit=2, tenant_id=TENANT)

    assert first.traces_deleted == 2
    async with postgres_pool.connection() as connection:
        cursor = await connection.execute(
            "SELECT count(*) AS count FROM observability.traces WHERE tenant_id = %s",
            (other_tenant,),
        )
        assert (await cursor.fetchone())["count"] == 1
        await connection.execute(
            "DELETE FROM observability.traces WHERE tenant_id = %s", (other_tenant,)
        )
    with pytest.raises(ValueError, match="positive"):
        await repository.cleanup_batch(limit=0, tenant_id=TENANT)


@pytest.mark.asyncio
async def test_terminal_outbox_deletion_is_bounded_and_not_counted_as_active(
    postgres_pool,
):
    async with postgres_pool.connection() as connection:
        async with connection.transaction():
            trace_id = await _insert_trace(connection, expired=True)
            for index in range(3):
                await connection.execute(
                    """
                    INSERT INTO notification.outbox (
                        id, tenant_id, customer_id, trace_id, idempotency_key,
                        payload, status, next_attempt_at
                    ) VALUES (%s, %s, 'customer', %s, %s, '{}'::jsonb,
                              'delivered', NULL)
                    """,
                    (uuid4(), TENANT, trace_id, f"terminal-{index}-{trace_id}"),
                )

    repository = PostgresRetentionRepository(postgres_pool)
    first = await repository.cleanup_batch(limit=2, tenant_id=TENANT)

    assert first.terminal_outbox_deleted == 2
    assert first.traces_deleted == 0
    assert first.traces_deferred_active_outbox == 0
    assert first.traces_deferred_terminal_outbox == 1

    second = await repository.cleanup_batch(limit=2, tenant_id=TENANT)

    assert second.terminal_outbox_deleted == 1
    assert second.traces_deleted == 1
    assert second.traces_deferred_terminal_outbox == 0
