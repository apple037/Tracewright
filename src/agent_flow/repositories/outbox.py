from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from agent_flow.contracts import HandoffEvent
from agent_flow.repositories.postgres import PostgresPool


@dataclass(frozen=True)
class OutboxRecord:
    id: UUID
    tenant_id: str
    customer_id: str
    trace_id: UUID
    idempotency_key: str
    payload_schema_version: int
    payload: dict[str, Any]
    status: str
    attempts: int
    next_attempt_at: datetime | None
    last_error_code: str | None
    last_http_status: int | None
    lock_owner: str | None
    locked_at: datetime | None
    lease_expires_at: datetime | None
    created_at: datetime
    delivered_at: datetime | None


_COLUMNS = """
id, tenant_id, customer_id, trace_id, idempotency_key,
payload_schema_version, payload, status, attempts, next_attempt_at,
last_error_code, last_http_status, lock_owner, locked_at,
lease_expires_at, created_at, delivered_at
"""


def _record(row: dict[str, Any]) -> OutboxRecord:
    return OutboxRecord(**row)


class OutboxRepository:
    def __init__(self, pool: PostgresPool) -> None:
        self._pool = pool

    async def enqueue(
        self,
        *,
        tenant_id: str,
        customer_id: str,
        trace_id: UUID,
        idempotency_key: str,
        event: HandoffEvent | None = None,
        reason_code: str | None = None,
        safe_message: str = "A human specialist will review this request.",
        session_id: str | None = None,
        primary_failure_event_id: int | None = None,
        delivery_disposition: str = "suppressed",
    ) -> OutboxRecord:
        handoff = event or HandoffEvent(
            required=True,
            reason_code=reason_code or "UNEXPECTED_ERROR",
            safe_message=safe_message,
        )
        payload: dict[str, Any] = {
            "tenant_id": tenant_id,
            "customer_id": customer_id,
            "trace_id": str(trace_id),
            "event": handoff.model_dump(mode="json"),
        }
        if session_id is not None:
            payload["session_id"] = session_id
        async with self._pool.connection() as connection:
            async with connection.transaction():
                cursor = await connection.execute(
                    f"""
                    INSERT INTO notification.outbox (
                        id, tenant_id, customer_id, trace_id, idempotency_key, payload
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                    RETURNING {_COLUMNS}
                    """,
                    (uuid4(), tenant_id, customer_id, trace_id, idempotency_key, Jsonb(payload)),
                )
                row = await cursor.fetchone()
                if row is None:
                    cursor = await connection.execute(
                        f"SELECT {_COLUMNS} FROM notification.outbox "
                        "WHERE tenant_id = %s AND idempotency_key = %s FOR UPDATE",
                        (tenant_id, idempotency_key),
                    )
                    row = await cursor.fetchone()
                    if row is None or (
                        row["customer_id"], row["trace_id"], row["payload"]
                    ) != (customer_id, trace_id, payload):
                        raise ValueError("idempotency key conflicts with immutable handoff")
                if primary_failure_event_id is not None:
                    await self._finalize_handoff_trace(
                        connection,
                        trace_id=trace_id,
                        tenant_id=tenant_id,
                        customer_id=customer_id,
                        primary_failure_event_id=primary_failure_event_id,
                        delivery_disposition=delivery_disposition,
                    )
        return _record(row)

    async def _finalize_handoff_trace(
        self,
        connection,
        *,
        trace_id: UUID,
        tenant_id: str,
        customer_id: str,
        primary_failure_event_id: int,
        delivery_disposition: str,
    ) -> None:
        cursor = await connection.execute(
            "SELECT 1 FROM observability.events "
            "WHERE id = %s AND trace_id = %s AND tenant_id = %s",
            (primary_failure_event_id, trace_id, tenant_id),
        )
        if await cursor.fetchone() is None:
            raise ValueError("primary failure event does not belong to trace")
        cursor = await connection.execute(
            """
            UPDATE observability.traces
            SET status = 'failed', terminal_outcome = 'handoff',
                primary_failure_event_id = %s, delivery_disposition = %s,
                finished_at = now()
            WHERE id = %s AND tenant_id = %s AND customer_id = %s
              AND finished_at IS NULL
            """,
            (
                primary_failure_event_id, delivery_disposition, trace_id,
                tenant_id, customer_id,
            ),
        )
        if cursor.rowcount == 1:
            return
        cursor = await connection.execute(
            "SELECT status, terminal_outcome, primary_failure_event_id, "
            "delivery_disposition, finished_at FROM observability.traces "
            "WHERE id = %s AND tenant_id = %s AND customer_id = %s FOR UPDATE",
            (trace_id, tenant_id, customer_id),
        )
        stored = await cursor.fetchone()
        expected = ("failed", "handoff", primary_failure_event_id, delivery_disposition)
        if stored is None or stored["finished_at"] is None or (
            stored["status"], stored["terminal_outcome"],
            stored["primary_failure_event_id"], stored["delivery_disposition"],
        ) != expected:
            raise ValueError("trace is already finalized with conflicting values")

    async def claim(
        self, *, owner: str, limit: int, lease_seconds: int
    ) -> tuple[OutboxRecord, ...]:
        if not owner or limit < 1 or lease_seconds < 1:
            raise ValueError("claim requires owner and positive bounds")
        records: list[OutboxRecord] = []
        async with self._pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    UPDATE notification.outbox
                    SET status = 'failed', lock_owner = NULL, locked_at = NULL,
                        lease_expires_at = NULL, next_attempt_at = now(),
                        last_error_code = 'LEASE_EXPIRED'
                    WHERE status = 'delivering' AND lease_expires_at <= now()
                    """
                )
                cursor = await connection.execute(
                    """
                    SELECT id FROM notification.outbox
                    WHERE status IN ('queued', 'failed') AND next_attempt_at <= now()
                    ORDER BY created_at, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                    """,
                    (limit,),
                )
                selected = await cursor.fetchall()
                for selected_row in selected:
                    cursor = await connection.execute(
                        f"""
                        UPDATE notification.outbox
                        SET status = 'delivering', attempts = attempts + 1,
                            lock_owner = %s, locked_at = now(),
                            lease_expires_at = now() + make_interval(secs => %s)
                        WHERE id = %s
                        RETURNING {_COLUMNS}
                        """,
                        (owner, lease_seconds, selected_row["id"]),
                    )
                    records.append(_record(await cursor.fetchone()))
        return tuple(records)

    async def complete(
        self, row_id: UUID, *, owner: str, http_status: int
    ) -> OutboxRecord:
        return await self._settle(
            row_id,
            owner=owner,
            status="delivered",
            error_code=None,
            http_status=http_status,
            backoff_seconds=None,
        )

    async def fail(
        self,
        row_id: UUID,
        *,
        owner: str,
        error_code: str,
        http_status: int | None,
        retryable: bool,
        max_attempts: int,
        backoff_seconds: int,
    ) -> OutboxRecord:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT attempts FROM notification.outbox WHERE id = %s",
                (row_id,),
            )
            row = await cursor.fetchone()
        permanent = not retryable or row is None or row["attempts"] >= max_attempts
        return await self._settle(
            row_id,
            owner=owner,
            status="failed",
            error_code=error_code,
            http_status=http_status,
            backoff_seconds=None if permanent else backoff_seconds,
        )

    async def _settle(
        self,
        row_id: UUID,
        *,
        owner: str,
        status: str,
        error_code: str | None,
        http_status: int | None,
        backoff_seconds: int | None,
    ) -> OutboxRecord:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                f"""
                UPDATE notification.outbox
                SET status = %s, last_error_code = %s, last_http_status = %s,
                    next_attempt_at = now() + make_interval(secs => %s),
                    delivered_at = CASE WHEN %s = 'delivered' THEN now() ELSE delivered_at END,
                    locked_at = NULL, lease_expires_at = NULL
                WHERE id = %s AND status = 'delivering' AND lock_owner = %s
                RETURNING {_COLUMNS}
                """,
                (
                    status, error_code, http_status, backoff_seconds,
                    status, row_id, owner,
                ),
            )
            row = await cursor.fetchone()
            if row is None:
                cursor = await connection.execute(
                    f"SELECT {_COLUMNS} FROM notification.outbox WHERE id = %s",
                    (row_id,),
                )
                row = await cursor.fetchone()
                next_attempt_matches = row is not None and (
                    (backoff_seconds is None and row["next_attempt_at"] is None)
                    or (backoff_seconds is not None and row["next_attempt_at"] is not None)
                )
                if row is None or (
                    row["status"], row["lock_owner"], row["last_error_code"],
                    row["last_http_status"],
                ) != (status, owner, error_code, http_status) or not next_attempt_matches:
                    raise ValueError("outbox row is not delivering for claim owner")
        return _record(row)

    async def count(self, *, tenant_id: str) -> int:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT count(*) AS count FROM notification.outbox WHERE tenant_id = %s",
                (tenant_id,),
            )
            return (await cursor.fetchone())["count"]
