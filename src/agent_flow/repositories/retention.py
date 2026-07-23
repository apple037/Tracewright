from __future__ import annotations

from dataclasses import dataclass

from agent_flow.repositories.postgres import PostgresPool


@dataclass(frozen=True)
class RetentionResult:
    turns_deleted: int = 0
    turn_inputs_deleted: int = 0
    snapshots_deleted: int = 0
    conversations_deleted: int = 0
    terminal_outbox_deleted: int = 0
    traces_deleted: int = 0
    traces_deferred_active_outbox: int = 0
    traces_deferred_terminal_outbox: int = 0


class PostgresRetentionRepository:
    def __init__(self, pool: PostgresPool) -> None:
        self._pool = pool

    async def cleanup_batch(
        self, *, limit: int, tenant_id: str | None = None
    ) -> RetentionResult:
        if limit < 1:
            raise ValueError("retention limit must be positive")
        scope = "AND tenant_id = %s" if tenant_id is not None else ""
        parameters = (tenant_id,) if tenant_id is not None else ()
        counts: dict[str, int] = {}
        async with self._pool.connection() as connection:
            async with connection.transaction():
                counts["turns_deleted"] = await self._delete_expired(
                    connection, "runtime.turns", "expires_at", scope, parameters, limit
                )
                counts["turn_inputs_deleted"] = await self._delete_expired(
                    connection,
                    "runtime.turn_inputs",
                    "expires_at",
                    scope,
                    parameters,
                    limit,
                )
                counts["snapshots_deleted"] = await self._delete_expired(
                    connection,
                    "runtime.conversation_snapshots",
                    "captured_at + interval '30 days'",
                    scope,
                    parameters,
                    limit,
                )
                cursor = await connection.execute(
                    f"""
                    WITH candidates AS (
                        SELECT id FROM runtime.conversations
                        WHERE expires_at <= now() {scope}
                          AND NOT EXISTS (
                              SELECT 1 FROM runtime.turns
                              WHERE conversation_id = runtime.conversations.id
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM runtime.conversation_snapshots
                              WHERE conversation_id = runtime.conversations.id
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM runtime.turn_inputs
                              WHERE tenant_id = runtime.conversations.tenant_id
                                AND customer_id = runtime.conversations.customer_id
                                AND session_id = runtime.conversations.session_id
                          )
                        ORDER BY expires_at, id
                        FOR UPDATE SKIP LOCKED
                        LIMIT %s
                    )
                    DELETE FROM runtime.conversations
                    WHERE id IN (SELECT id FROM candidates)
                    """,
                    (*parameters, limit),
                )
                counts["conversations_deleted"] = cursor.rowcount

                cursor = await connection.execute(
                    f"""
                    SELECT id FROM observability.traces
                    WHERE expires_at <= now() {scope}
                    ORDER BY expires_at, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                    """,
                    (*parameters, limit),
                )
                trace_ids = [row["id"] for row in await cursor.fetchall()]
                if trace_ids:
                    cursor = await connection.execute(
                        """
                        WITH terminal AS (
                            SELECT id FROM notification.outbox
                            WHERE trace_id = ANY(%s)
                              AND (
                                status = 'delivered'
                                OR (
                                  status = 'failed'
                                  AND next_attempt_at IS NULL
                                )
                              )
                            ORDER BY created_at, id
                            FOR UPDATE SKIP LOCKED
                            LIMIT %s
                        )
                        DELETE FROM notification.outbox
                        WHERE id IN (SELECT id FROM terminal)
                        """,
                        (trace_ids, limit),
                    )
                    counts["terminal_outbox_deleted"] = cursor.rowcount
                    cursor = await connection.execute(
                        """
                        SELECT count(DISTINCT trace_id) AS count
                        FROM notification.outbox
                        WHERE trace_id = ANY(%s)
                          AND (
                            status IN ('queued', 'delivering')
                            OR (status = 'failed' AND next_attempt_at IS NOT NULL)
                          )
                        """,
                        (trace_ids,),
                    )
                    counts["traces_deferred_active_outbox"] = (
                        await cursor.fetchone()
                    )["count"]
                    cursor = await connection.execute(
                        """
                        SELECT count(DISTINCT trace_id) AS count
                        FROM notification.outbox
                        WHERE trace_id = ANY(%s)
                          AND (
                            status = 'delivered'
                            OR (status = 'failed' AND next_attempt_at IS NULL)
                          )
                        """,
                        (trace_ids,),
                    )
                    counts["traces_deferred_terminal_outbox"] = (
                        await cursor.fetchone()
                    )["count"]
                    cursor = await connection.execute(
                        """
                        DELETE FROM observability.traces AS candidate
                        WHERE candidate.id = ANY(%s)
                          AND NOT EXISTS (
                            SELECT 1 FROM notification.outbox
                            WHERE trace_id = candidate.id
                          )
                          AND NOT EXISTS (
                            SELECT 1 FROM runtime.turns
                            WHERE trace_id = candidate.id
                          )
                          AND NOT EXISTS (
                            SELECT 1 FROM runtime.turn_inputs
                            WHERE trace_id = candidate.id
                          )
                          AND NOT EXISTS (
                            SELECT 1 FROM runtime.conversation_snapshots
                            WHERE trace_id = candidate.id
                          )
                          AND NOT EXISTS (
                            SELECT 1 FROM observability.traces AS child
                            WHERE child.id <> candidate.id
                              AND (
                                child.root_trace_id = candidate.id
                                OR child.retry_of_trace_id = candidate.id
                              )
                          )
                        """,
                        (trace_ids,),
                    )
                    counts["traces_deleted"] = cursor.rowcount
                else:
                    counts.update(
                        terminal_outbox_deleted=0,
                        traces_deferred_active_outbox=0,
                        traces_deferred_terminal_outbox=0,
                        traces_deleted=0,
                    )
        return RetentionResult(**counts)

    @staticmethod
    async def _delete_expired(
        connection,
        table: str,
        boundary: str,
        scope: str,
        parameters: tuple[object, ...],
        limit: int,
    ) -> int:
        cursor = await connection.execute(
            f"""
            WITH candidates AS (
                SELECT ctid FROM {table}
                WHERE {boundary} <= now() {scope}
                ORDER BY {boundary}, ctid
                FOR UPDATE SKIP LOCKED
                LIMIT %s
            )
            DELETE FROM {table}
            WHERE ctid IN (SELECT ctid FROM candidates)
            """,
            (*parameters, limit),
        )
        return cursor.rowcount
