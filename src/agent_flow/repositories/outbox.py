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
    claim_token: UUID | None
    settlement_retryable: bool | None
    settlement_backoff_seconds: int | None
    created_at: datetime
    delivered_at: datetime | None


_COLUMNS = """
id, tenant_id, customer_id, trace_id, idempotency_key,
payload_schema_version, payload, status, attempts, next_attempt_at,
last_error_code, last_http_status, lock_owner, locked_at,
lease_expires_at, claim_token, settlement_retryable,
settlement_backoff_seconds, created_at, delivered_at
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
        self, *, owner: str, limit: int, lease_seconds: int, max_attempts: int
    ) -> tuple[OutboxRecord, ...]:
        if not owner or min(limit, lease_seconds, max_attempts) < 1:
            raise ValueError("claim requires owner and positive bounds")
        records: list[OutboxRecord] = []
        async with self._pool.connection() as connection:
            async with connection.transaction():
                while len(records) < limit:
                    cursor = await connection.execute(
                        """
                        SELECT id, attempts FROM notification.outbox
                        WHERE (
                            status IN ('queued', 'failed')
                            AND next_attempt_at IS NOT NULL
                            AND next_attempt_at <= now()
                        ) OR (
                            status = 'delivering' AND lease_expires_at <= now()
                        )
                        ORDER BY created_at, id
                        FOR UPDATE SKIP LOCKED
                        LIMIT %s
                        """,
                        (limit - len(records),),
                    )
                    selected = await cursor.fetchall()
                    if not selected:
                        break
                    for selected_row in selected:
                        if selected_row["attempts"] >= max_attempts:
                            await connection.execute(
                                """
                                UPDATE notification.outbox
                                SET status = 'failed', next_attempt_at = NULL,
                                    last_error_code = 'ATTEMPTS_EXHAUSTED',
                                    last_http_status = NULL, locked_at = NULL,
                                    lease_expires_at = NULL,
                                    settlement_retryable = false,
                                    settlement_backoff_seconds = NULL
                                WHERE id = %s
                                """,
                                (selected_row["id"],),
                            )
                            continue
                        claim_token = uuid4()
                        cursor = await connection.execute(
                            f"""
                            UPDATE notification.outbox
                            SET status = 'delivering', attempts = attempts + 1,
                                lock_owner = %s, claim_token = %s, locked_at = now(),
                                lease_expires_at = now() + make_interval(secs => %s),
                                last_error_code = NULL, last_http_status = NULL,
                                settlement_retryable = NULL,
                                settlement_backoff_seconds = NULL
                            WHERE id = %s
                            RETURNING {_COLUMNS}
                            """,
                            (owner, claim_token, lease_seconds, selected_row["id"]),
                        )
                        records.append(_record(await cursor.fetchone()))
        return tuple(records)

    async def complete(
        self, row_id: UUID, *, owner: str, claim_token: UUID, http_status: int
    ) -> OutboxRecord:
        return await self._settle(
            row_id,
            owner=owner,
            claim_token=claim_token,
            status="delivered",
            error_code=None,
            http_status=http_status,
            retryable=None,
            max_attempts=None,
            backoff_seconds=None,
        )

    async def fail(
        self,
        row_id: UUID,
        *,
        owner: str,
        claim_token: UUID,
        error_code: str,
        http_status: int | None,
        retryable: bool,
        max_attempts: int,
        backoff_seconds: int,
    ) -> OutboxRecord:
        return await self._settle(
            row_id,
            owner=owner,
            claim_token=claim_token,
            status="failed",
            error_code=error_code,
            http_status=http_status,
            retryable=retryable,
            max_attempts=max_attempts,
            backoff_seconds=backoff_seconds,
        )

    async def _settle(
        self,
        row_id: UUID,
        *,
        owner: str,
        claim_token: UUID,
        status: str,
        error_code: str | None,
        http_status: int | None,
        retryable: bool | None,
        max_attempts: int | None,
        backoff_seconds: int | None,
    ) -> OutboxRecord:
        if status == "failed" and (
            max_attempts is None or max_attempts < 1
            or backoff_seconds is None or backoff_seconds < 0
        ):
            raise ValueError("failure settlement requires positive retry bounds")
        async with self._pool.connection() as connection:
            async with connection.transaction():
                cursor = await connection.execute(
                    f"SELECT {_COLUMNS}, lease_expires_at > now() AS lease_active "
                    "FROM notification.outbox WHERE id = %s FOR UPDATE",
                    (row_id,),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise ValueError("outbox row does not exist")
                effective_retryable = (
                    retryable and row["attempts"] < max_attempts
                    if status == "failed"
                    else None
                )
                effective_backoff = backoff_seconds if effective_retryable else None
                expected = (
                    status, owner, claim_token, error_code, http_status,
                    effective_retryable, effective_backoff,
                )
                stored = (
                    row["status"], row["lock_owner"], row["claim_token"],
                    row["last_error_code"], row["last_http_status"],
                    row["settlement_retryable"],
                    row["settlement_backoff_seconds"],
                )
                if row["status"] != "delivering":
                    if stored == expected:
                        return _record({key: row[key] for key in OutboxRecord.__dataclass_fields__})
                    raise ValueError("settlement replay conflicts with stored decision")
                if (
                    row["lock_owner"] != owner
                    or row["claim_token"] != claim_token
                    or not row["lease_active"]
                ):
                    raise ValueError("outbox row does not have a matching active claim")
                cursor = await connection.execute(
                    f"""
                    UPDATE notification.outbox
                    SET status = %s, last_error_code = %s, last_http_status = %s,
                        next_attempt_at = now() + make_interval(secs => %s),
                        delivered_at = CASE
                            WHEN %s = 'delivered' THEN now() ELSE delivered_at END,
                        locked_at = NULL, lease_expires_at = NULL,
                        settlement_retryable = %s,
                        settlement_backoff_seconds = %s
                    WHERE id = %s AND status = 'delivering'
                      AND lock_owner = %s AND claim_token = %s
                      AND lease_expires_at > now()
                    RETURNING {_COLUMNS}
                    """,
                    (
                        status, error_code, http_status, effective_backoff,
                        status, effective_retryable, effective_backoff,
                        row_id, owner, claim_token,
                    ),
                )
                settled = await cursor.fetchone()
                if settled is None:
                    raise ValueError("outbox row does not have a matching active claim")
                row = settled
        return _record(row)

    async def get(self, row_id: UUID) -> OutboxRecord | None:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                f"SELECT {_COLUMNS} FROM notification.outbox WHERE id = %s",
                (row_id,),
            )
            row = await cursor.fetchone()
        return _record(row) if row is not None else None

    async def count(self, *, tenant_id: str) -> int:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT count(*) AS count FROM notification.outbox WHERE tenant_id = %s",
                (tenant_id,),
            )
            return (await cursor.fetchone())["count"]
