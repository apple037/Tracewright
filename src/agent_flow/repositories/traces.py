from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from agent_flow.repositories.postgres import PostgresPool


@dataclass(frozen=True)
class TraceEvent:
    id: int
    trace_id: UUID
    span_id: UUID | None
    sequence: int
    event_type: str
    component: str
    status: str
    error_code: str | None
    payload_schema_version: int
    payload: dict[str, Any]
    created_at: datetime

    @property
    def node(self) -> str | None:
        value = self.payload.get("node")
        return value if isinstance(value, str) else None

    @property
    def kind(self) -> str:
        return self.status

    @property
    def metadata(self) -> dict[str, Any]:
        value = self.payload.get("metadata", {})
        return value if isinstance(value, dict) else {}

    @property
    def operation(self) -> str | None:
        value = self.payload.get("operation")
        return value if isinstance(value, str) else None


@dataclass(frozen=True)
class TraceSpan:
    id: UUID
    trace_id: UUID
    parent_span_id: UUID | None
    name: str
    status: str
    attempt: int
    error_code: str | None
    created_at: datetime
    finished_at: datetime | None

    @property
    def node(self) -> str:
        return self.name


@dataclass(frozen=True)
class TraceIssueSummary:
    error_code: str | None
    failed_node: str | None
    component: str
    operation: str | None


@dataclass(frozen=True)
class TraceRecord:
    id: UUID
    tenant_id: str
    customer_id: str
    session_id: str
    status: str
    terminal_outcome: str | None
    primary_failure_event_id: int | None
    root_trace_id: UUID
    retry_of_trace_id: UUID | None
    retry_sequence: int
    retry_initiator: str | None
    retry_reason: str | None
    delivery_disposition: str | None
    created_at: datetime
    finished_at: datetime | None
    spans: tuple[TraceSpan, ...]
    events: tuple[TraceEvent, ...]

    @property
    def issue_summary(self) -> TraceIssueSummary | None:
        if self.primary_failure_event_id is None:
            return None
        event = next(
            (value for value in self.events if value.id == self.primary_failure_event_id),
            None,
        )
        if event is None:
            return None
        return TraceIssueSummary(
            error_code=event.error_code,
            failed_node=event.node,
            component=event.component,
            operation=event.operation,
        )


def _event(row: dict[str, Any]) -> TraceEvent:
    return TraceEvent(**row)


def _span(row: dict[str, Any]) -> TraceSpan:
    return TraceSpan(**row)


class PostgresTraceRepository:
    def __init__(self, pool: PostgresPool) -> None:
        self._pool = pool

    async def _lock_running_trace(
        self, connection, trace_id: UUID, tenant_id: str
    ) -> dict[str, Any]:
        cursor = await connection.execute(
            """
            SELECT id, tenant_id, customer_id, status, finished_at
            FROM observability.traces
            WHERE id = %s AND tenant_id = %s
            FOR UPDATE
            """,
            (trace_id, tenant_id),
        )
        trace = await cursor.fetchone()
        if trace is None:
            raise ValueError("trace does not exist")
        if trace["status"] != "running" or trace["finished_at"] is not None:
            raise ValueError("trace is already finalized")
        return trace

    async def clear_test_data(self) -> None:
        async with self._pool.connection() as connection:
            database = await connection.execute("SELECT current_database() AS name")
            name = (await database.fetchone())["name"]
            if "test" not in name.lower():
                raise RuntimeError("clear_test_data is restricted to test databases")
            await connection.execute(
                "TRUNCATE observability.traces, runtime.conversations RESTART IDENTITY CASCADE"
            )

    async def start_trace(
        self,
        *,
        tenant_id: str,
        customer_id: str,
        session_id: str,
        retry_of_trace_id: UUID | None = None,
        retry_initiator: str | None = None,
        retry_reason: str | None = None,
        delivery_disposition: str | None = None,
    ) -> UUID:
        trace_id = uuid4()
        root_trace_id = trace_id
        retry_sequence = 0
        async with self._pool.connection() as connection:
            async with connection.transaction():
                if retry_of_trace_id is not None:
                    cursor = await connection.execute(
                        """
                        SELECT root_trace_id
                        FROM observability.traces
                        WHERE id = %s AND tenant_id = %s AND customer_id = %s
                          AND session_id = %s
                        """,
                        (retry_of_trace_id, tenant_id, customer_id, session_id),
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
                        "SELECT COALESCE(MAX(retry_sequence), 0) + 1 AS sequence "
                        "FROM observability.traces WHERE root_trace_id = %s",
                        (root_trace_id,),
                    )
                    retry_sequence = (await cursor.fetchone())["sequence"]
                await connection.execute(
                    """
                    INSERT INTO observability.traces (
                        id, tenant_id, customer_id, session_id, root_trace_id,
                        retry_of_trace_id, retry_sequence, retry_initiator,
                        retry_reason, delivery_disposition, expires_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                              now() + interval '180 days')
                    """,
                    (
                        trace_id,
                        tenant_id,
                        customer_id,
                        session_id,
                        root_trace_id,
                        retry_of_trace_id,
                        retry_sequence,
                        retry_initiator,
                        retry_reason,
                        delivery_disposition,
                    ),
                )
        return trace_id

    async def start_span(
        self,
        trace_id: UUID,
        name: str,
        *,
        tenant_id: str,
        parent_span_id: UUID | None = None,
        attempt: int = 1,
    ) -> UUID:
        span_id = uuid4()
        async with self._pool.connection() as connection:
            async with connection.transaction():
                trace = await self._lock_running_trace(connection, trace_id, tenant_id)
                if parent_span_id is not None:
                    cursor = await connection.execute(
                        """
                        SELECT 1 FROM observability.spans
                        WHERE id = %s AND trace_id = %s AND tenant_id = %s
                        """,
                        (parent_span_id, trace_id, tenant_id),
                    )
                    if await cursor.fetchone() is None:
                        raise ValueError(
                            "parent span must belong to the same trace and tenant"
                        )
                await connection.execute(
                    """
                    INSERT INTO observability.spans (
                        id, trace_id, tenant_id, customer_id, parent_span_id, name, attempt
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        span_id,
                        trace_id,
                        tenant_id,
                        trace["customer_id"],
                        parent_span_id,
                        name,
                        attempt,
                    ),
                )
        return span_id

    async def append_event(
        self,
        *,
        trace_id: UUID,
        span_id: UUID | None,
        tenant_id: str,
        event_type: str,
        component: str,
        status: str,
        payload: dict[str, Any],
        error_code: str | None = None,
        payload_schema_version: int = 1,
    ) -> TraceEvent:
        async with self._pool.connection() as connection:
            async with connection.transaction():
                trace = await self._lock_running_trace(connection, trace_id, tenant_id)
                if span_id is not None:
                    cursor = await connection.execute(
                        """
                        SELECT 1 FROM observability.spans
                        WHERE id = %s AND trace_id = %s AND tenant_id = %s
                        """,
                        (span_id, trace_id, tenant_id),
                    )
                    if await cursor.fetchone() is None:
                        raise ValueError("span must belong to the same trace and tenant")
                cursor = await connection.execute(
                    """
                    UPDATE observability.traces
                    SET next_event_sequence = next_event_sequence + 1
                    WHERE id = %s AND tenant_id = %s
                    RETURNING next_event_sequence
                    """,
                    (trace_id, tenant_id),
                )
                sequence = (await cursor.fetchone())["next_event_sequence"]
                cursor = await connection.execute(
                    """
                    INSERT INTO observability.events (
                        trace_id, span_id, tenant_id, customer_id, sequence,
                        event_type, component, status, error_code,
                        payload_schema_version, payload, expires_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                              now() + interval '180 days')
                    RETURNING id, trace_id, span_id, sequence, event_type, component,
                              status, error_code, payload_schema_version, payload, created_at
                    """,
                    (
                        trace_id,
                        span_id,
                        trace["tenant_id"],
                        trace["customer_id"],
                        sequence,
                        event_type,
                        component,
                        status,
                        error_code,
                        payload_schema_version,
                        Jsonb(payload),
                    ),
                )
                row = await cursor.fetchone()
        return _event(row)

    async def finish_span(
        self,
        span_id: UUID,
        status: str,
        *,
        tenant_id: str,
        error_code: str | None = None,
    ) -> None:
        if status not in {"completed", "failed", "cancelled", "skipped"}:
            raise ValueError("finish_span requires a terminal status")
        async with self._pool.connection() as connection:
            async with connection.transaction():
                cursor = await connection.execute(
                    "SELECT trace_id FROM observability.spans "
                    "WHERE id = %s AND tenant_id = %s",
                    (span_id, tenant_id),
                )
                span = await cursor.fetchone()
                if span is None:
                    raise ValueError("span does not exist")
                await self._lock_running_trace(
                    connection, span["trace_id"], tenant_id
                )
                cursor = await connection.execute(
                    """
                    UPDATE observability.spans
                    SET status = %s, error_code = %s, finished_at = now()
                    WHERE id = %s AND tenant_id = %s AND finished_at IS NULL
                    RETURNING id
                    """,
                    (status, error_code, span_id, tenant_id),
                )
                if await cursor.fetchone() is None:
                    cursor = await connection.execute(
                        "SELECT status, error_code, finished_at FROM observability.spans "
                        "WHERE id = %s AND tenant_id = %s",
                        (span_id, tenant_id),
                    )
                    existing = await cursor.fetchone()
                    if existing is None:
                        raise ValueError("span does not exist")
                    if (
                        existing["finished_at"] is not None
                        and (existing["status"], existing["error_code"])
                        == (status, error_code)
                    ):
                        return
                    raise ValueError("span is already finished with conflicting values")

    async def finish_trace(
        self,
        trace_id: UUID,
        status: str,
        *,
        tenant_id: str,
        primary_failure_event_id: int | None = None,
        terminal_outcome: str | None = None,
        delivery_disposition: str | None = None,
    ) -> None:
        if status not in {"succeeded", "failed"}:
            raise ValueError("finish_trace requires a terminal status")
        async with self._pool.connection() as connection:
            async with connection.transaction():
                if primary_failure_event_id is not None:
                    cursor = await connection.execute(
                        """
                        SELECT 1 FROM observability.events
                        WHERE id = %s AND trace_id = %s AND tenant_id = %s
                        """,
                        (primary_failure_event_id, trace_id, tenant_id),
                    )
                    if await cursor.fetchone() is None:
                        raise ValueError("primary failure event does not belong to trace")
                cursor = await connection.execute(
                    """
                    UPDATE observability.traces
                    SET status = %s, terminal_outcome = %s,
                        primary_failure_event_id = %s,
                        delivery_disposition = COALESCE(%s, delivery_disposition),
                        finished_at = now()
                    WHERE id = %s AND tenant_id = %s AND finished_at IS NULL
                    """,
                    (
                        status,
                        terminal_outcome,
                        primary_failure_event_id,
                        delivery_disposition,
                        trace_id,
                        tenant_id,
                    ),
                )
                if cursor.rowcount != 1:
                    cursor = await connection.execute(
                        "SELECT status, primary_failure_event_id, terminal_outcome, "
                        "delivery_disposition, finished_at FROM observability.traces "
                        "WHERE id = %s AND tenant_id = %s",
                        (trace_id, tenant_id),
                    )
                    existing = await cursor.fetchone()
                    if existing is None:
                        raise ValueError("trace does not exist")
                    requested = (
                        status, primary_failure_event_id, terminal_outcome,
                        delivery_disposition,
                    )
                    stored = (
                        existing["status"], existing["primary_failure_event_id"],
                        existing["terminal_outcome"], existing["delivery_disposition"],
                    )
                    if existing["finished_at"] is not None and stored == requested:
                        return
                    raise ValueError("trace is already finalized with conflicting values")

    async def get_trace(self, trace_id: UUID, *, tenant_id: str) -> TraceRecord | None:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT id, tenant_id, customer_id, session_id, status,
                       terminal_outcome, primary_failure_event_id, root_trace_id,
                       retry_of_trace_id, retry_sequence, retry_initiator,
                       retry_reason, delivery_disposition, created_at, finished_at
                FROM observability.traces
                WHERE id = %s AND tenant_id = %s
                """,
                (trace_id, tenant_id),
            )
            trace = await cursor.fetchone()
            if trace is None:
                return None
            cursor = await connection.execute(
                """
                SELECT id, trace_id, parent_span_id, name, status, attempt,
                       error_code, created_at, finished_at
                FROM observability.spans WHERE trace_id = %s ORDER BY created_at, id
                """,
                (trace_id,),
            )
            spans = tuple(_span(row) for row in await cursor.fetchall())
            events = await self._events(connection, trace_id, 0)
        return TraceRecord(**trace, spans=spans, events=events)

    async def events_after(
        self, trace_id: UUID, *, tenant_id: str, after_sequence: int
    ) -> tuple[TraceEvent, ...]:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT 1 FROM observability.traces WHERE id = %s AND tenant_id = %s",
                (trace_id, tenant_id),
            )
            if await cursor.fetchone() is None:
                return ()
            return await self._events(connection, trace_id, after_sequence)

    async def _events(self, connection, trace_id: UUID, after_sequence: int):
        cursor = await connection.execute(
            """
            SELECT id, trace_id, span_id, sequence, event_type, component,
                   status, error_code, payload_schema_version, payload, created_at
            FROM observability.events
            WHERE trace_id = %s AND sequence > %s
            ORDER BY sequence
            """,
            (trace_id, after_sequence),
        )
        return tuple(_event(row) for row in await cursor.fetchall())
