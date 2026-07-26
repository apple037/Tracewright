from datetime import datetime
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from agent_flow.contracts import CapturedTurnInput, ConversationSnapshot, TurnRequest
from agent_flow.repositories.postgres import PostgresPool


def _as_turns(stored: object) -> tuple[dict[str, str], ...]:
    # ponytail: snapshots written before role tagging are flat strings; recover
    # the roles by position rather than migrating the table. Drop this when no
    # pre-1.1 snapshot rows remain (they expire after 30 days).
    turns: list[dict[str, str]] = []
    for index, entry in enumerate(stored or ()):
        if isinstance(entry, dict):
            turns.append({"role": entry["role"], "text": entry["text"]})
        else:
            turns.append(
                {
                    "role": "customer" if index % 2 == 0 else "assistant",
                    "text": str(entry),
                }
            )
    return tuple(turns)


class PostgresConversationRepository:
    def __init__(self, pool: PostgresPool, *, history_turns: int = 8) -> None:
        self._pool = pool
        self._history_turns = history_turns

    async def capture_turn_input(
        self, *, tenant_id: str, customer_id: str, session_id: str,
        trace_id: UUID, request: TurnRequest, captured_at: datetime | None = None,
    ) -> CapturedTurnInput:
        if request.session_id != session_id:
            raise ValueError("turn input session conflicts with trace scope")
        async with self._pool.connection() as connection:
            async with connection.transaction():
                cursor = await connection.execute(
                    "SELECT 1 FROM observability.traces WHERE id = %s AND tenant_id = %s "
                    "AND customer_id = %s AND session_id = %s",
                    (trace_id, tenant_id, customer_id, session_id),
                )
                if await cursor.fetchone() is None:
                    raise ValueError("trace does not belong to turn input scope")
                await connection.execute(
                    """
                    INSERT INTO runtime.turn_inputs (
                        trace_id, tenant_id, customer_id, session_id, message,
                        case_id, captured_at, expires_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, COALESCE(%s, now()),
                              COALESCE(%s, now()) + interval '30 days')
                    ON CONFLICT (trace_id) DO NOTHING
                    """,
                    (trace_id, tenant_id, customer_id, session_id, request.message,
                     request.case_id, captured_at, captured_at),
                )
                cursor = await connection.execute(
                    """
                    SELECT session_id, message, case_id, captured_at, expires_at
                    FROM runtime.turn_inputs
                    WHERE trace_id = %s AND tenant_id = %s AND customer_id = %s
                    """,
                    (trace_id, tenant_id, customer_id),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise ValueError("turn input does not exist in this scope")
        stored = TurnRequest(
            session_id=row["session_id"], message=row["message"], case_id=row["case_id"]
        )
        if stored != request:
            raise ValueError("turn input binding conflicts")
        return CapturedTurnInput(
            request=stored, captured_at=row["captured_at"], expires_at=row["expires_at"]
        )

    async def get_retry_turn_input(
        self, trace_id: UUID, *, tenant_id: str, customer_id: str,
        bind_trace_id: UUID | None = None,
    ) -> CapturedTurnInput:
        async with self._pool.connection() as connection:
            async with connection.transaction():
                cursor = await connection.execute(
                    """
                    SELECT session_id, message, case_id, captured_at, expires_at
                    FROM runtime.turn_inputs
                    WHERE trace_id = %s AND tenant_id = %s AND customer_id = %s
                    """,
                    (trace_id, tenant_id, customer_id),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise ValueError("turn input does not exist in this scope")
                if row["expires_at"] <= datetime.now(row["expires_at"].tzinfo):
                    raise ValueError("turn input has expired")
                if bind_trace_id is not None:
                    cursor = await connection.execute(
                        "SELECT 1 FROM observability.traces WHERE id = %s AND tenant_id = %s "
                        "AND customer_id = %s AND session_id = %s",
                        (bind_trace_id, tenant_id, customer_id, row["session_id"]),
                    )
                    if await cursor.fetchone() is None:
                        raise ValueError("retry trace does not belong to turn input scope")
                    await connection.execute(
                        """
                        INSERT INTO runtime.turn_inputs (
                            trace_id, tenant_id, customer_id, session_id, message,
                            case_id, captured_at, expires_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (trace_id) DO NOTHING
                        """,
                        (bind_trace_id, tenant_id, customer_id, row["session_id"],
                         row["message"], row["case_id"], row["captured_at"], row["expires_at"]),
                    )
                    cursor = await connection.execute(
                        "SELECT session_id, message, case_id, captured_at, expires_at "
                        "FROM runtime.turn_inputs WHERE trace_id = %s AND tenant_id = %s "
                        "AND customer_id = %s",
                        (bind_trace_id, tenant_id, customer_id),
                    )
                    bound = await cursor.fetchone()
                    if bound is None or dict(bound) != dict(row):
                        raise ValueError("turn input binding conflicts")
        return CapturedTurnInput(
            request=TurnRequest(session_id=row["session_id"], message=row["message"], case_id=row["case_id"]),
            captured_at=row["captured_at"], expires_at=row["expires_at"],
        )

    async def _ensure_conversation(
        self, connection, tenant_id: str, customer_id: str, session_id: str
    ) -> UUID:
        conversation_id = uuid4()
        cursor = await connection.execute(
            """
            INSERT INTO runtime.conversations (
                id, tenant_id, customer_id, session_id, expires_at
            ) VALUES (%s, %s, %s, %s, now() + interval '30 days')
            ON CONFLICT (tenant_id, customer_id, session_id) DO UPDATE
            SET updated_at = now(), expires_at = now() + interval '30 days'
            RETURNING id
            """,
            (conversation_id, tenant_id, customer_id, session_id),
        )
        return (await cursor.fetchone())["id"]

    async def list_turns(
        self, *, tenant_id: str, customer_id: str, session_id: str, limit: int = 100
    ) -> tuple[dict[str, object], ...]:
        """Visible transcript for a session, oldest first.

        Scoped to one tenant+customer so a token can only ever read its own
        conversation. Used by the console to restore the chat after a reload.
        """
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT customer_text, assistant_text, citations, created_at
                FROM runtime.turns
                WHERE tenant_id = %s AND customer_id = %s AND session_id = %s
                ORDER BY created_at, id
                LIMIT %s
                """,
                (tenant_id, customer_id, session_id, limit),
            )
            rows = await cursor.fetchall()
        return tuple(
            {
                "customer_text": row["customer_text"],
                "assistant_text": row["assistant_text"],
                "citations": tuple(row["citations"] or ()),
                "created_at": row["created_at"],
            }
            for row in rows
        )

    async def get_snapshot(
        self,
        *,
        tenant_id: str,
        customer_id: str,
        session_id: str,
        trace_id: UUID,
    ) -> ConversationSnapshot:
        async with self._pool.connection() as connection:
            async with connection.transaction():
                cursor = await connection.execute(
                    """
                    SELECT 1 FROM observability.traces
                    WHERE id = %s AND tenant_id = %s AND customer_id = %s
                      AND session_id = %s
                    """,
                    (trace_id, tenant_id, customer_id, session_id),
                )
                if await cursor.fetchone() is None:
                    raise ValueError("trace does not belong to conversation scope")
                conversation_id = await self._ensure_conversation(
                    connection, tenant_id, customer_id, session_id
                )
                # Newest `history_turns` exchanges, oldest-first. Unbounded
                # history used to blow the classifier's 100-message cap and turn
                # a long session into a permanent failure. Handed-off turns
                # ('failed') are included: the customer's message still happened,
                # and dropping it makes a rephrase start from zero context.
                cursor = await connection.execute(
                    """
                    SELECT customer_text, assistant_text FROM (
                        SELECT turn.customer_text, turn.assistant_text,
                               turn.created_at, turn.id
                        FROM runtime.turns AS turn
                        JOIN observability.traces AS trace ON trace.id = turn.trace_id
                        WHERE turn.tenant_id = %s AND turn.customer_id = %s
                          AND turn.session_id = %s
                          AND trace.status IN ('succeeded', 'failed')
                        ORDER BY turn.created_at DESC, turn.id DESC
                        LIMIT %s
                    ) AS recent
                    ORDER BY recent.created_at, recent.id
                    """,
                    (tenant_id, customer_id, session_id, self._history_turns),
                )
                messages = tuple(
                    entry
                    for row in await cursor.fetchall()
                    for entry in (
                        {"role": "customer", "text": row["customer_text"]},
                        {"role": "assistant", "text": row["assistant_text"]},
                    )
                )
                snapshot_id = uuid4()
                await connection.execute(
                    """
                    INSERT INTO runtime.conversation_snapshots (
                        id, trace_id, conversation_id, tenant_id, customer_id,
                        session_id, messages
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (trace_id) DO NOTHING
                    """,
                    (
                        snapshot_id,
                        trace_id,
                        conversation_id,
                        tenant_id,
                        customer_id,
                        session_id,
                        Jsonb(messages),
                    ),
                )
                cursor = await connection.execute(
                    """
                    SELECT session_id, messages, captured_at
                    FROM runtime.conversation_snapshots
                    WHERE trace_id = %s AND tenant_id = %s AND customer_id = %s
                    """,
                    (trace_id, tenant_id, customer_id),
                )
                row = await cursor.fetchone()
        return ConversationSnapshot(
            session_id=row["session_id"],
            messages=_as_turns(row["messages"]),
            captured_at=row["captured_at"],
        )

    async def get_retry_snapshot(
        self,
        trace_id: UUID,
        *,
        tenant_id: str,
        customer_id: str,
        bind_trace_id: UUID | None = None,
    ) -> ConversationSnapshot:
        async with self._pool.connection() as connection:
            async with connection.transaction():
                cursor = await connection.execute(
                    """
                    SELECT conversation_id, session_id, messages, captured_at
                    FROM runtime.conversation_snapshots
                    WHERE trace_id = %s AND tenant_id = %s AND customer_id = %s
                    """,
                    (trace_id, tenant_id, customer_id),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise ValueError("retry snapshot does not exist in this scope")
                if bind_trace_id is not None:
                    cursor = await connection.execute(
                        """
                        SELECT 1 FROM observability.traces
                        WHERE id = %s AND tenant_id = %s AND customer_id = %s
                          AND session_id = %s
                        """,
                        (bind_trace_id, tenant_id, customer_id, row["session_id"]),
                    )
                    if await cursor.fetchone() is None:
                        raise ValueError("retry trace does not belong to snapshot scope")
                    await connection.execute(
                        """
                        INSERT INTO runtime.conversation_snapshots (
                            id, trace_id, conversation_id, tenant_id, customer_id,
                            session_id, messages, captured_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (trace_id) DO NOTHING
                        """,
                        (
                            uuid4(), bind_trace_id, row["conversation_id"], tenant_id,
                            customer_id, row["session_id"], Jsonb(row["messages"]),
                            row["captured_at"],
                        ),
                    )
        return ConversationSnapshot(
            session_id=row["session_id"],
            messages=_as_turns(row["messages"]),
            captured_at=row["captured_at"],
        )

    async def append_turn(
        self,
        *,
        tenant_id: str,
        customer_id: str,
        session_id: str,
        trace_id: UUID,
        customer_text: str,
        assistant_text: str,
        citations: tuple[str, ...] = (),
    ) -> None:
        async with self._pool.connection() as connection:
            async with connection.transaction():
                cursor = await connection.execute(
                    """
                    SELECT status, finished_at FROM observability.traces
                    WHERE id = %s AND tenant_id = %s AND customer_id = %s
                      AND session_id = %s
                    FOR SHARE
                    """,
                    (trace_id, tenant_id, customer_id, session_id),
                )
                trace = await cursor.fetchone()
                if trace is None:
                    raise ValueError("trace does not belong to conversation scope")
                if trace["status"] not in {"running", "succeeded"}:
                    raise ValueError("failed traces cannot persist assistant turns")
                conversation_id = await self._ensure_conversation(
                    connection, tenant_id, customer_id, session_id
                )
                await connection.execute(
                    """
                    INSERT INTO runtime.turns (
                        conversation_id, tenant_id, customer_id, session_id,
                        trace_id, customer_text, assistant_text, citations, expires_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                              now() + interval '30 days')
                    ON CONFLICT (trace_id) DO NOTHING
                    """,
                    (
                        conversation_id,
                        tenant_id,
                        customer_id,
                        session_id,
                        trace_id,
                        customer_text,
                        assistant_text,
                        Jsonb(citations),
                    ),
                )
