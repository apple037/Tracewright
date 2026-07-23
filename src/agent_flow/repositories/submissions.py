from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from agent_flow.auth import AuthorizedCustomerContext
from agent_flow.contracts import InboundMessage, SubmissionResult
from agent_flow.repositories.postgres import PostgresPool


@dataclass(frozen=True)
class SubmissionRecord:
    id: UUID
    trace_id: UUID
    tenant_id: str
    customer_id: str
    status: str
    attempts: int
    payload: dict[str, Any]
    result: dict[str, Any] | None
    last_error_code: str | None
    last_error_component: str | None
    lease_expires_at: datetime | None
    claim_token: UUID | None
    created_at: datetime
    finished_at: datetime | None
    retry_of_trace_id: UUID | None = None

    def to_result(self) -> SubmissionResult:
        if self.result is not None:
            return SubmissionResult.model_validate(self.result)
        return SubmissionResult(
            submission_id=self.id,
            trace_id=self.trace_id,
            status=self.status,
            error_code=self.last_error_code,
            error_component=self.last_error_component,
        )


_COLUMNS = """
id, trace_id, tenant_id, customer_id, status, attempts, payload, result,
last_error_code, last_error_component, lease_expires_at, claim_token,
created_at, finished_at
"""
_COLUMNS_WITH_LINEAGE = f"""
{_COLUMNS},
(
    SELECT traces.retry_of_trace_id
    FROM observability.traces AS traces
    WHERE traces.id = runtime.jobs.trace_id
) AS retry_of_trace_id
"""


def _record(row: dict[str, Any]) -> SubmissionRecord:
    values = {
        field: row[field]
        for field in SubmissionRecord.__dataclass_fields__
        if field != "retry_of_trace_id"
    }
    values["retry_of_trace_id"] = row.get("retry_of_trace_id")
    return SubmissionRecord(**values)


class PostgresSubmissionRepository:
    def __init__(self, pool: PostgresPool) -> None:
        self._pool = pool

    async def enqueue(
        self,
        context: AuthorizedCustomerContext,
        message: InboundMessage,
        *,
        retry_of_trace_id: UUID | None = None,
        retry_initiator: str | None = None,
        retry_reason: str | None = None,
        delivery_disposition: str | None = None,
    ) -> SubmissionRecord:
        if not isinstance(context, AuthorizedCustomerContext):
            raise TypeError("context must be an AuthorizedCustomerContext")
        if not isinstance(message, InboundMessage):
            raise TypeError("message must be an InboundMessage")
        submission_id, trace_id = uuid4(), uuid4()
        payload = {
            "message": message.model_dump(mode="json"),
            "retry": {
                "retry_of_trace_id": (
                    str(retry_of_trace_id)
                    if retry_of_trace_id is not None
                    else None
                ),
                "retry_initiator": retry_initiator,
                "retry_reason": retry_reason,
                "delivery_disposition": delivery_disposition,
            },
        }
        async with self._pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    SELECT pg_advisory_xact_lock(
                        hashtextextended(
                            jsonb_build_array(%s::text, %s::text)::text, 0
                        )
                    )
                    """,
                    (context.tenant_id, message.idempotency_key),
                )
                existing = await self._get_by_key(
                    connection, context.tenant_id, message.idempotency_key
                )
                if existing is not None:
                    if (
                        existing.customer_id != context.customer_id
                        or existing.payload != payload
                    ):
                        raise ValueError("submission idempotency conflict")
                    return existing
                root_id, retry_sequence = await self._resolve_lineage(
                    connection,
                    trace_id=trace_id,
                    retry_of_trace_id=retry_of_trace_id,
                    context=context,
                    session_id=message.session_id,
                )
                await connection.execute(
                    """
                    INSERT INTO observability.traces (
                        id, tenant_id, customer_id, session_id, status,
                        root_trace_id, retry_of_trace_id, retry_sequence,
                        retry_initiator, retry_reason, delivery_disposition,
                        channel, external_message_id, expires_at
                    ) VALUES (
                        %s, %s, %s, %s, 'queued', %s, %s, %s, %s, %s, %s,
                        %s, %s, now() + interval '180 days'
                    )
                    """,
                    (
                        trace_id,
                        context.tenant_id,
                        context.customer_id,
                        message.session_id,
                        root_id,
                        retry_of_trace_id,
                        retry_sequence,
                        retry_initiator,
                        retry_reason,
                        delivery_disposition,
                        message.channel,
                        message.external_message_id,
                    ),
                )
                cursor = await connection.execute(
                    """
                    INSERT INTO runtime.jobs (
                        id, trace_id, tenant_id, customer_id, job_type,
                        payload, status, idempotency_key
                    ) VALUES (%s, %s, %s, %s, 'turn', %s, 'queued', %s)
                    ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
                    RETURNING id
                    """,
                    (
                        submission_id,
                        trace_id,
                        context.tenant_id,
                        context.customer_id,
                        Jsonb(payload),
                        message.idempotency_key,
                    ),
                )
                if await cursor.fetchone() is None:
                    await connection.execute(
                        "DELETE FROM observability.traces WHERE id = %s",
                        (trace_id,),
                    )
                    existing = await self._get_by_key(
                        connection, context.tenant_id, message.idempotency_key
                    )
                    if existing is None or (
                        existing.customer_id != context.customer_id
                        or existing.payload != payload
                    ):
                        raise ValueError("submission idempotency conflict")
                    return existing
                return await self._get_required(connection, submission_id)

    async def get(
        self, submission_id: UUID, *, tenant_id: str, customer_id: str
    ) -> SubmissionRecord | None:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                f"""
                SELECT {_COLUMNS_WITH_LINEAGE}
                FROM runtime.jobs
                WHERE id = %s AND tenant_id = %s AND customer_id = %s
                  AND job_type = 'turn'
                """,
                (submission_id, tenant_id, customer_id),
            )
            row = await cursor.fetchone()
        return _record(row) if row is not None else None

    async def claim(
        self, *, owner: str, limit: int, lease_seconds: int, max_attempts: int
    ) -> tuple[SubmissionRecord, ...]:
        if not owner or min(limit, lease_seconds, max_attempts) < 1:
            raise ValueError("claim requires owner and positive bounds")
        records: list[SubmissionRecord] = []
        async with self._pool.connection() as connection:
            async with connection.transaction():
                cursor = await connection.execute(
                    """
                    SELECT id, attempts
                    FROM runtime.jobs
                    WHERE job_type = 'turn'
                      AND (
                        (status = 'queued' AND available_at <= now())
                        OR (status = 'running' AND lease_expires_at <= now())
                      )
                    ORDER BY priority, created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                    """,
                    (limit,),
                )
                for selected in await cursor.fetchall():
                    if selected["attempts"] >= max_attempts:
                        await self._finalize_exhausted_trace(
                            connection, selected["id"]
                        )
                        await connection.execute(
                            """
                            UPDATE runtime.jobs
                            SET status = 'failed',
                                last_error_code = 'ATTEMPTS_EXHAUSTED',
                                last_error_component = 'turn_worker',
                                lease_expires_at = NULL, finished_at = now()
                            WHERE id = %s
                            """,
                            (selected["id"],),
                        )
                        continue
                    claim_token = uuid4()
                    cursor = await connection.execute(
                        f"""
                        UPDATE runtime.jobs
                        SET status = 'running', attempts = attempts + 1,
                            lock_owner = %s, locked_at = now(),
                            lease_expires_at = now() + make_interval(secs => %s),
                            claim_token = %s, last_error_code = NULL,
                            last_error_component = NULL
                        WHERE id = %s
                        RETURNING id
                        """,
                        (
                            owner,
                            lease_seconds,
                            claim_token,
                            selected["id"],
                        ),
                    )
                    await cursor.fetchone()
                    records.append(
                        await self._get_required(connection, selected["id"])
                    )
        return tuple(records)

    async def heartbeat(
        self,
        submission_id: UUID,
        *,
        owner: str,
        claim_token: UUID,
        lease_seconds: int,
    ) -> bool:
        if not owner or lease_seconds < 1:
            raise ValueError("heartbeat requires owner and a positive lease")
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                """
                UPDATE runtime.jobs
                SET lease_expires_at = now() + make_interval(secs => %s)
                WHERE id = %s AND lock_owner = %s AND claim_token = %s
                  AND status = 'running' AND lease_expires_at > now()
                """,
                (lease_seconds, submission_id, owner, claim_token),
            )
            return cursor.rowcount == 1

    async def complete(
        self,
        submission_id: UUID,
        *,
        owner: str,
        claim_token: UUID,
        result: SubmissionResult,
    ) -> bool:
        if not isinstance(result, SubmissionResult):
            raise TypeError("result must be a SubmissionResult")
        if result.submission_id != submission_id or result.status != "completed":
            raise ValueError("result does not match completed submission")
        safe_result = result.model_dump(mode="json")
        async with self._pool.connection() as connection:
            async with connection.transaction():
                cursor = await connection.execute(
                    """
                    SELECT trace_id, status, lock_owner, claim_token, result,
                           lease_expires_at > now() AS lease_active
                    FROM runtime.jobs
                    WHERE id = %s AND job_type = 'turn'
                    FOR UPDATE
                    """,
                    (submission_id,),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise ValueError("submission does not exist")
                if (
                    row["trace_id"] != result.trace_id
                    or row["lock_owner"] != owner
                    or row["claim_token"] != claim_token
                ):
                    raise ValueError("submission does not have a matching active claim")
                if row["status"] != "running":
                    if row["status"] == "completed" and row["result"] == safe_result:
                        return True
                    raise ValueError("submission settlement replay conflicts")
                if not row["lease_active"]:
                    raise ValueError("submission does not have a matching active claim")
                cursor = await connection.execute(
                    """
                    UPDATE runtime.jobs
                    SET status = 'completed', result = %s, finished_at = now(),
                        lease_expires_at = NULL
                    WHERE id = %s AND lock_owner = %s AND claim_token = %s
                      AND status = 'running' AND lease_expires_at > now()
                    """,
                    (Jsonb(safe_result), submission_id, owner, claim_token),
                )
                if cursor.rowcount != 1:
                    raise ValueError("submission does not have a matching active claim")
        return True

    async def fail(
        self,
        submission_id: UUID,
        *,
        owner: str,
        claim_token: UUID,
        error_code: str,
        error_component: str,
        retryable: bool,
        max_attempts: int,
        backoff_seconds: int,
    ) -> bool:
        if (
            not owner
            or not 1 <= len(error_code) <= 128
            or not 1 <= len(error_component) <= 128
            or max_attempts < 1
            or backoff_seconds < 0
        ):
            raise ValueError("failure settlement requires bounded safe fields")
        async with self._pool.connection() as connection:
            async with connection.transaction():
                cursor = await connection.execute(
                    """
                    SELECT status, attempts, lock_owner, claim_token,
                           lease_expires_at > now() AS lease_active
                    FROM runtime.jobs
                    WHERE id = %s AND job_type = 'turn'
                    FOR UPDATE
                    """,
                    (submission_id,),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise ValueError("submission does not exist")
                if (
                    row["status"] != "running"
                    or row["lock_owner"] != owner
                    or row["claim_token"] != claim_token
                    or not row["lease_active"]
                ):
                    raise ValueError("submission does not have a matching active claim")
                should_retry = retryable and row["attempts"] < max_attempts
                cursor = await connection.execute(
                    """
                    UPDATE runtime.jobs
                    SET status = CASE WHEN %s THEN 'queued' ELSE 'failed' END,
                        available_at = CASE
                            WHEN %s THEN now() + make_interval(secs => %s)
                            ELSE available_at
                        END,
                        last_error_code = %s,
                        last_error_component = %s,
                        result = NULL, lease_expires_at = NULL,
                        finished_at = CASE WHEN %s THEN NULL ELSE now() END
                    WHERE id = %s AND lock_owner = %s AND claim_token = %s
                      AND status = 'running' AND lease_expires_at > now()
                    """,
                    (
                        should_retry,
                        should_retry,
                        backoff_seconds,
                        error_code,
                        error_component,
                        should_retry,
                        submission_id,
                        owner,
                        claim_token,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("submission does not have a matching active claim")
        return True

    async def fail_terminal_with_trace(
        self,
        submission_id: UUID,
        *,
        owner: str,
        claim_token: UUID,
        error_code: str,
        error_component: str,
    ) -> bool:
        if (
            not owner
            or not 1 <= len(error_code) <= 128
            or not 1 <= len(error_component) <= 128
        ):
            raise ValueError(
                "terminal failure settlement requires bounded safe fields"
            )
        async with self._pool.connection() as connection:
            async with connection.transaction():
                cursor = await connection.execute(
                    """
                    SELECT trace_id, status, lock_owner, claim_token,
                           lease_expires_at > now() AS lease_active
                    FROM runtime.jobs
                    WHERE id = %s AND job_type = 'turn'
                    FOR UPDATE
                    """,
                    (submission_id,),
                )
                job = await cursor.fetchone()
                if job is None:
                    raise ValueError("submission does not exist")
                if (
                    job["status"] != "running"
                    or job["lock_owner"] != owner
                    or job["claim_token"] != claim_token
                    or not job["lease_active"]
                ):
                    raise ValueError(
                        "submission does not have a matching active claim"
                    )
                cursor = await connection.execute(
                    """
                    SELECT id, tenant_id, customer_id, status
                    FROM observability.traces
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (job["trace_id"],),
                )
                trace = await cursor.fetchone()
                if trace is None:
                    raise ValueError("submission trace does not exist")
                if trace["status"] not in {"queued", "running"}:
                    raise ValueError("submission trace is already finalized")
                cursor = await connection.execute(
                    """
                    UPDATE observability.traces
                    SET next_event_sequence = next_event_sequence + 1
                    WHERE id = %s
                    RETURNING next_event_sequence
                    """,
                    (trace["id"],),
                )
                sequence = (await cursor.fetchone())["next_event_sequence"]
                cursor = await connection.execute(
                    """
                    INSERT INTO observability.events (
                        trace_id, tenant_id, customer_id, sequence,
                        event_type, component, status, error_code, payload,
                        expires_at
                    ) VALUES (
                        %s, %s, %s, %s, 'worker.pre_pipeline_failed', %s,
                        'failed', %s, %s, now() + interval '180 days'
                    )
                    RETURNING id
                    """,
                    (
                        trace["id"],
                        trace["tenant_id"],
                        trace["customer_id"],
                        sequence,
                        error_component,
                        error_code,
                        Jsonb(
                            {
                                "node": "turn_worker",
                                "operation": "pre_pipeline_validation",
                            }
                        ),
                    ),
                )
                failure_event_id = (await cursor.fetchone())["id"]
                await connection.execute(
                    """
                    UPDATE observability.traces
                    SET status = 'failed', terminal_outcome = 'error',
                        primary_failure_event_id = %s, finished_at = now()
                    WHERE id = %s AND status IN ('queued', 'running')
                    """,
                    (failure_event_id, trace["id"]),
                )
                cursor = await connection.execute(
                    """
                    UPDATE runtime.jobs
                    SET status = 'failed', last_error_code = %s,
                        last_error_component = %s, result = NULL,
                        lease_expires_at = NULL, finished_at = now()
                    WHERE id = %s AND status = 'running'
                      AND lock_owner = %s AND claim_token = %s
                      AND lease_expires_at > now()
                    """,
                    (
                        error_code,
                        error_component,
                        submission_id,
                        owner,
                        claim_token,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError(
                        "submission does not have a matching active claim"
                    )
        return True

    async def recover_expired_claim(
        self,
        submission_id: UUID,
        *,
        owner: str,
        claim_token: UUID,
        error_code: str = "WORKER_LEASE_EXPIRED",
    ) -> SubmissionRecord:
        if not owner or not 1 <= len(error_code) <= 128:
            raise ValueError("recovery requires bounded safe fields")
        async with self._pool.connection() as connection:
            async with connection.transaction():
                cursor = await connection.execute(
                    f"""
                    SELECT {_COLUMNS}, lock_owner,
                           lease_expires_at > now() AS lease_active
                    FROM runtime.jobs
                    WHERE id = %s AND job_type = 'turn'
                    FOR UPDATE
                    """,
                    (submission_id,),
                )
                job = await cursor.fetchone()
                if job is None:
                    raise ValueError("submission does not exist")
                if (
                    job["status"] != "running"
                    or job["lock_owner"] != owner
                    or job["claim_token"] != claim_token
                    or not job["lease_active"]
                ):
                    raise ValueError("submission does not have a matching active claim")
                cursor = await connection.execute(
                    """
                    SELECT id, tenant_id, customer_id, session_id, status,
                           root_trace_id, retry_of_trace_id, retry_sequence,
                           delivery_disposition, channel, external_message_id
                    FROM observability.traces
                    WHERE id = %s AND tenant_id = %s AND customer_id = %s
                    FOR UPDATE
                    """,
                    (job["trace_id"], job["tenant_id"], job["customer_id"]),
                )
                trace = await cursor.fetchone()
                if trace is None:
                    raise ValueError("submission trace does not exist")
                if trace["status"] == "queued":
                    return replace(
                        _record({
                            field: job[field]
                            for field in SubmissionRecord.__dataclass_fields__
                            if field in job
                        }),
                        retry_of_trace_id=trace["retry_of_trace_id"],
                    )
                if trace["status"] != "running":
                    raise ValueError("submission trace is already finalized")
                cursor = await connection.execute(
                    """
                    UPDATE observability.traces
                    SET next_event_sequence = next_event_sequence + 1
                    WHERE id = %s
                    RETURNING next_event_sequence
                    """,
                    (trace["id"],),
                )
                sequence = (await cursor.fetchone())["next_event_sequence"]
                cursor = await connection.execute(
                    """
                    INSERT INTO observability.events (
                        trace_id, tenant_id, customer_id, sequence,
                        event_type, component, status, error_code, payload,
                        expires_at
                    ) VALUES (
                        %s, %s, %s, %s, 'worker.lease_expired', 'turn_worker',
                        'failed', %s, %s, now() + interval '180 days'
                    )
                    RETURNING id
                    """,
                    (
                        trace["id"],
                        trace["tenant_id"],
                        trace["customer_id"],
                        sequence,
                        error_code,
                        Jsonb(
                            {
                                "node": "turn_worker",
                                "operation": "recover_expired_claim",
                            }
                        ),
                    ),
                )
                failure_event_id = (await cursor.fetchone())["id"]
                await connection.execute(
                    """
                    UPDATE observability.traces
                    SET status = 'failed', terminal_outcome = 'error',
                        primary_failure_event_id = %s, finished_at = now()
                    WHERE id = %s
                    """,
                    (failure_event_id, trace["id"]),
                )
                await connection.execute(
                    "SELECT id FROM observability.traces WHERE id = %s FOR UPDATE",
                    (trace["root_trace_id"],),
                )
                cursor = await connection.execute(
                    """
                    SELECT COALESCE(MAX(retry_sequence), 0) + 1 AS sequence
                    FROM observability.traces
                    WHERE root_trace_id = %s
                    """,
                    (trace["root_trace_id"],),
                )
                retry_sequence = (await cursor.fetchone())["sequence"]
                replacement_trace_id = uuid4()
                await connection.execute(
                    """
                    INSERT INTO observability.traces (
                        id, tenant_id, customer_id, session_id, status,
                        root_trace_id, retry_of_trace_id, retry_sequence,
                        retry_initiator, retry_reason, delivery_disposition,
                        channel, external_message_id, expires_at
                    ) VALUES (
                        %s, %s, %s, %s, 'queued', %s, %s, %s,
                        'turn_worker', %s, %s, %s, %s,
                        now() + interval '180 days'
                    )
                    """,
                    (
                        replacement_trace_id,
                        trace["tenant_id"],
                        trace["customer_id"],
                        trace["session_id"],
                        trace["root_trace_id"],
                        trace["id"],
                        retry_sequence,
                        error_code,
                        trace["delivery_disposition"],
                        trace["channel"],
                        trace["external_message_id"],
                    ),
                )
                await connection.execute(
                    """
                    UPDATE runtime.jobs
                    SET trace_id = %s
                    WHERE id = %s AND status = 'running'
                      AND lock_owner = %s AND claim_token = %s
                    """,
                    (replacement_trace_id, submission_id, owner, claim_token),
                )
                return await self._get_required(connection, submission_id)

    async def _get_by_key(
        self, connection, tenant_id: str, idempotency_key: str
    ) -> SubmissionRecord | None:
        cursor = await connection.execute(
            f"""
            SELECT {_COLUMNS_WITH_LINEAGE}, job_type
            FROM runtime.jobs
            WHERE tenant_id = %s AND idempotency_key = %s
            FOR UPDATE
            """,
            (tenant_id, idempotency_key),
        )
        row = await cursor.fetchone()
        if row is not None and row["job_type"] != "turn":
            raise ValueError("submission idempotency conflict")
        return (
            _record(
                {
                    field: row[field]
                    for field in SubmissionRecord.__dataclass_fields__
                }
            )
            if row is not None
            else None
        )

    async def _finalize_exhausted_trace(
        self, connection, submission_id: UUID
    ) -> None:
        cursor = await connection.execute(
            """
            SELECT traces.id, traces.tenant_id, traces.customer_id, traces.status
            FROM runtime.jobs AS jobs
            JOIN observability.traces AS traces ON traces.id = jobs.trace_id
            WHERE jobs.id = %s
            FOR UPDATE OF traces
            """,
            (submission_id,),
        )
        trace = await cursor.fetchone()
        if trace is None or trace["status"] not in {"queued", "running"}:
            return
        cursor = await connection.execute(
            """
            UPDATE observability.traces
            SET next_event_sequence = next_event_sequence + 1
            WHERE id = %s
            RETURNING next_event_sequence
            """,
            (trace["id"],),
        )
        sequence = (await cursor.fetchone())["next_event_sequence"]
        cursor = await connection.execute(
            """
            INSERT INTO observability.events (
                trace_id, tenant_id, customer_id, sequence,
                event_type, component, status, error_code, payload, expires_at
            ) VALUES (
                %s, %s, %s, %s, 'worker.attempts_exhausted', 'turn_worker',
                'failed', 'ATTEMPTS_EXHAUSTED', %s,
                now() + interval '180 days'
            )
            RETURNING id
            """,
            (
                trace["id"],
                trace["tenant_id"],
                trace["customer_id"],
                sequence,
                Jsonb(
                    {
                        "node": "turn_worker",
                        "operation": "claim",
                    }
                ),
            ),
        )
        failure_event_id = (await cursor.fetchone())["id"]
        await connection.execute(
            """
            UPDATE observability.traces
            SET status = 'failed', terminal_outcome = 'error',
                primary_failure_event_id = %s, finished_at = now()
            WHERE id = %s
            """,
            (failure_event_id, trace["id"]),
        )

    async def _get_required(
        self, connection, submission_id: UUID
    ) -> SubmissionRecord:
        cursor = await connection.execute(
            f"""
            SELECT {_COLUMNS_WITH_LINEAGE}
            FROM runtime.jobs
            WHERE id = %s
            """,
            (submission_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise ValueError("submission does not exist")
        return _record(row)

    async def _resolve_lineage(
        self,
        connection,
        *,
        trace_id: UUID,
        retry_of_trace_id: UUID | None,
        context: AuthorizedCustomerContext,
        session_id: str,
    ) -> tuple[UUID, int]:
        if retry_of_trace_id is None:
            return trace_id, 0
        cursor = await connection.execute(
            """
            SELECT root_trace_id
            FROM observability.traces
            WHERE id = %s AND tenant_id = %s AND customer_id = %s
              AND session_id = %s
            """,
            (
                retry_of_trace_id,
                context.tenant_id,
                context.customer_id,
                session_id,
            ),
        )
        source = await cursor.fetchone()
        if source is None:
            raise ValueError("retry source trace does not belong to this scope")
        root_trace_id = source["root_trace_id"]
        await connection.execute(
            "SELECT id FROM observability.traces WHERE id = %s FOR UPDATE",
            (root_trace_id,),
        )
        cursor = await connection.execute(
            """
            SELECT COALESCE(MAX(retry_sequence), 0) + 1 AS sequence
            FROM observability.traces
            WHERE root_trace_id = %s
            """,
            (root_trace_id,),
        )
        return root_trace_id, (await cursor.fetchone())["sequence"]
