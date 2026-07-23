from __future__ import annotations

from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from psycopg.types.json import Jsonb

from agent_flow.seed_demo import DemoSeeder


FIXTURE_ROOT = Path("tests/fixtures")
TENANT_ID = "t1"
DEMO_SOURCE_ID = "agent-flow-demo:policy-1"
DOCUMENT_ID = UUID("12000000-0000-0000-0000-000000000001")
CHUNK_ID = UUID("12000000-0000-0000-0000-000000000002")
OWNED_DOCUMENT_ID = uuid5(
    NAMESPACE_URL,
    ":".join(
        (
            "agent-flow-demo",
            "document",
            TENANT_ID,
            DEMO_SOURCE_ID,
            "v1",
        )
    ),
)


def _vector() -> str:
    return "[" + ",".join(["1", *("0" for _ in range(1023))]) + "]"


async def _delete_test_source(postgres_pool) -> None:
    from tests.integration.conftest import require_unambiguous_test_database

    await require_unambiguous_test_database(postgres_pool)
    async with postgres_pool.connection() as connection:
        await connection.execute(
            "DELETE FROM rag.documents "
            "WHERE tenant_id = %s AND source_id = %s AND version = 'v1'",
            (TENANT_ID, DEMO_SOURCE_ID),
        )


async def _snapshot(postgres_pool) -> dict:
    async with postgres_pool.connection() as connection:
        cursor = await connection.execute(
            """
            SELECT
                d.id AS document_id,
                d.customer_id,
                d.checksum,
                d.access_metadata,
                d.ingestion_status,
                d.effective_at,
                d.valid_until,
                c.id AS chunk_id,
                c.tenant_id AS chunk_tenant_id,
                c.customer_id AS chunk_customer_id,
                c.content,
                c.metadata,
                c.embedding::text AS embedding
            FROM rag.documents AS d
            JOIN rag.chunks AS c ON c.document_id = d.id
            WHERE d.tenant_id = %s
              AND d.source_id = %s
              AND d.version = 'v1'
              AND c.ordinal = 0
            """,
            (TENANT_ID, DEMO_SOURCE_ID),
        )
        return await cursor.fetchone()


class _FakeCursor:
    def __init__(self, row=None):
        self._row = row

    async def fetchone(self):
        return self._row


class _FakeConnection:
    def __init__(self, database_name: str):
        self.database_name = database_name
        self.statements: list[str] = []

    async def execute(self, statement, parameters=None):
        self.statements.append(statement)
        if "current_database" in statement:
            return _FakeCursor({"name": self.database_name})
        return _FakeCursor()


class _FakeConnectionContext:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_):
        return None


class _FakePool:
    def __init__(self, database_name: str):
        self.connection_value = _FakeConnection(database_name)

    def connection(self):
        return _FakeConnectionContext(self.connection_value)


@pytest.mark.asyncio
async def test_seed_cleanup_rejects_ambiguous_database_before_delete():
    pool = _FakePool("agent_staging")

    with pytest.raises(RuntimeError, match="test databases"):
        await _delete_test_source(pool)

    assert len(pool.connection_value.statements) == 1
    assert "current_database" in pool.connection_value.statements[0]
    assert "DELETE" not in pool.connection_value.statements[0]


@pytest.mark.asyncio
async def test_seed_rejects_non_demo_collision_without_mutating_document_or_chunk(
    postgres_pool,
):
    await _delete_test_source(postgres_pool)
    async with postgres_pool.connection() as connection:
        await connection.execute(
            """
            INSERT INTO rag.documents (
                id, tenant_id, customer_id, source_id, version, checksum,
                access_metadata, ingestion_status, effective_at, valid_until
            ) VALUES (
                %s, %s, 'sentinel-customer', %s, 'v1', 'sentinel-checksum',
                %s, 'ready', '2024-01-01T00:00:00Z', '2098-01-01T00:00:00Z'
            )
            """,
            (
                DOCUMENT_ID,
                TENANT_ID,
                DEMO_SOURCE_ID,
                Jsonb({"owner": "non-demo", "scope": "sentinel"}),
            ),
        )
        await connection.execute(
            """
            INSERT INTO rag.chunks (
                id, document_id, tenant_id, customer_id, ordinal,
                content, metadata, embedding
            ) VALUES (
                %s, %s, %s, 'sentinel-customer', 0,
                'sentinel-content', %s, %s::vector
            )
            """,
            (
                CHUNK_ID,
                DOCUMENT_ID,
                TENANT_ID,
                Jsonb({"owner": "non-demo", "scope": "sentinel"}),
                _vector(),
            ),
        )
    before = await _snapshot(postgres_pool)

    try:
        with pytest.raises(ValueError, match="document collision"):
            await DemoSeeder(
                postgres_pool,
                fixture_root=FIXTURE_ROOT,
                tenant_id=TENANT_ID,
            ).seed()

        assert await _snapshot(postgres_pool) == before
    finally:
        await _delete_test_source(postgres_pool)


@pytest.mark.asyncio
async def test_seed_is_idempotent_in_real_postgres(postgres_pool):
    await _delete_test_source(postgres_pool)
    seeder = DemoSeeder(
        postgres_pool,
        fixture_root=FIXTURE_ROOT,
        tenant_id=TENANT_ID,
    )

    try:
        first = await seeder.seed()
        second = await seeder.seed()

        async with postgres_pool.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT
                    count(DISTINCT d.id) AS documents,
                    count(c.id) AS chunks,
                    bool_and(
                        d.access_metadata @>
                        '{"demo": true, "fixture_owner": "agent_flow.seed_demo"}'::jsonb
                    ) AS owned
                FROM rag.documents AS d
                JOIN rag.chunks AS c ON c.document_id = d.id
                WHERE d.tenant_id = %s
                  AND d.source_id = %s
                  AND d.version = 'v1'
                """,
                (TENANT_ID, DEMO_SOURCE_ID),
            )
            stored = await cursor.fetchone()
        assert first == second
        assert stored == {"documents": 1, "chunks": 1, "owned": True}
    finally:
        await _delete_test_source(postgres_pool)


@pytest.mark.asyncio
async def test_seed_rejects_non_demo_chunk_and_rolls_back_document_update(
    postgres_pool,
):
    await _delete_test_source(postgres_pool)
    async with postgres_pool.connection() as connection:
        await connection.execute(
            """
            INSERT INTO rag.documents (
                id, tenant_id, customer_id, source_id, version, checksum,
                access_metadata, ingestion_status, effective_at, valid_until
            ) VALUES (
                %s, %s, 'sentinel-customer', %s, 'v1', 'sentinel-checksum',
                %s, 'ready', '2024-01-01T00:00:00Z', '2098-01-01T00:00:00Z'
            )
            """,
            (
                OWNED_DOCUMENT_ID,
                TENANT_ID,
                DEMO_SOURCE_ID,
                Jsonb(
                    {
                        "demo": True,
                        "fixture_owner": "agent_flow.seed_demo",
                        "fixture_type": "mock_rag",
                    }
                ),
            ),
        )
        await connection.execute(
            """
            INSERT INTO rag.chunks (
                id, document_id, tenant_id, customer_id, ordinal,
                content, metadata, embedding
            ) VALUES (
                %s, %s, %s, 'sentinel-customer', 0,
                'sentinel-content', %s, %s::vector
            )
            """,
            (
                CHUNK_ID,
                OWNED_DOCUMENT_ID,
                TENANT_ID,
                Jsonb({"owner": "non-demo", "scope": "sentinel"}),
                _vector(),
            ),
        )
    before = await _snapshot(postgres_pool)

    try:
        with pytest.raises(ValueError, match="chunk collision"):
            await DemoSeeder(
                postgres_pool,
                fixture_root=FIXTURE_ROOT,
                tenant_id=TENANT_ID,
            ).seed()

        assert await _snapshot(postgres_pool) == before
    finally:
        await _delete_test_source(postgres_pool)
