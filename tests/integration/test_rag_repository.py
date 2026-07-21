from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from psycopg.types.json import Jsonb

from agent_flow.auth import AuthorizedCustomerContext
from agent_flow.contracts import RagSearchRequest
from agent_flow.repositories.rag import RagRepository


PYTEST_OWNER_KEY = "pytest_owner"
PYTEST_OWNER_VALUE = "agent-flow-task6"


def _vector(x: float, y: float = 0.0) -> str:
    return "[" + ",".join([str(x), str(y), *("0" for _ in range(1022))]) + "]"


async def _insert_chunk(
    postgres_pool,
    *,
    document_id: str,
    chunk_id: str,
    tenant_id: str,
    customer_id: str | None,
    source_id: str,
    content: str,
    embedding: str,
    chunk_tenant_id: str | None = None,
    chunk_customer_id: str | None = None,
    pytest_owned: bool = True,
    effective_at: datetime | None = None,
    valid_until: datetime | None = None,
) -> None:
    chunk_tenant_id = chunk_tenant_id or tenant_id
    if chunk_customer_id is None:
        chunk_customer_id = customer_id
    access_metadata = {"kind": "test"}
    if pytest_owned:
        access_metadata[PYTEST_OWNER_KEY] = PYTEST_OWNER_VALUE
    async with postgres_pool.connection() as connection:
        await connection.execute(
            """
            INSERT INTO rag.documents (
                id, tenant_id, customer_id, source_id, version, checksum,
                access_metadata, ingestion_status, effective_at, valid_until
            ) VALUES (%s, %s, %s, %s, 'v1', %s, %s, 'ready', %s, %s)
            """,
            (
                UUID(document_id),
                tenant_id,
                customer_id,
                source_id,
                "document-checksum",
                Jsonb(access_metadata),
                effective_at or datetime(2025, 1, 1, tzinfo=timezone.utc),
                valid_until or datetime(2099, 1, 1, tzinfo=timezone.utc),
            ),
        )
        await connection.execute(
            """
            INSERT INTO rag.chunks (
                id, document_id, tenant_id, customer_id, ordinal,
                content, metadata, embedding
            ) VALUES (%s, %s, %s, %s, 0, %s, %s, %s::vector)
            """,
            (
                UUID(chunk_id),
                UUID(document_id),
                chunk_tenant_id,
                chunk_customer_id,
                content,
                Jsonb({"section": "test"}),
                embedding,
            ),
        )


@pytest.mark.asyncio
async def test_exact_cosine_search_is_ranked_and_scope_bound(
    rag_repository, postgres_pool
):
    await _insert_chunk(
        postgres_pool,
        document_id="00000000-0000-0000-0000-000000000001",
        chunk_id="10000000-0000-0000-0000-000000000001",
        tenant_id="t1",
        customer_id=None,
        source_id="tenant-public",
        content="公開保固內容",
        embedding=_vector(1.0),
    )
    await _insert_chunk(
        postgres_pool,
        document_id="00000000-0000-0000-0000-000000000005",
        chunk_id="10000000-0000-0000-0000-000000000005",
        tenant_id="t1",
        customer_id=None,
        chunk_tenant_id="t2",
        source_id="mismatched-chunk-tenant",
        content="不一致租戶欄位內容",
        embedding=_vector(1.0),
    )
    await _insert_chunk(
        postgres_pool,
        document_id="00000000-0000-0000-0000-000000000006",
        chunk_id="10000000-0000-0000-0000-000000000006",
        tenant_id="t1",
        customer_id="c1",
        chunk_customer_id="c2",
        source_id="mismatched-chunk-customer",
        content="不一致客戶欄位內容",
        embedding=_vector(1.0),
    )
    await _insert_chunk(
        postgres_pool,
        document_id="00000000-0000-0000-0000-000000000007",
        chunk_id="10000000-0000-0000-0000-000000000007",
        tenant_id="t1",
        customer_id="c2",
        chunk_customer_id="c1",
        source_id="mismatched-document-customer",
        content="不一致文件客戶欄位內容",
        embedding=_vector(1.0),
    )
    await _insert_chunk(
        postgres_pool,
        document_id="00000000-0000-0000-0000-000000000002",
        chunk_id="10000000-0000-0000-0000-000000000002",
        tenant_id="t1",
        customer_id="c1",
        source_id="customer-private",
        content="客戶專屬內容",
        embedding=_vector(0.8, 0.6),
    )
    await _insert_chunk(
        postgres_pool,
        document_id="00000000-0000-0000-0000-000000000003",
        chunk_id="10000000-0000-0000-0000-000000000003",
        tenant_id="t1",
        customer_id="c2",
        source_id="other-customer",
        content="不可洩漏的其他客戶內容",
        embedding=_vector(1.0),
    )
    await _insert_chunk(
        postgres_pool,
        document_id="00000000-0000-0000-0000-000000000004",
        chunk_id="10000000-0000-0000-0000-000000000004",
        tenant_id="t2",
        customer_id=None,
        source_id="other-tenant",
        content="不可洩漏的其他租戶內容",
        embedding=_vector(1.0),
    )
    context = AuthorizedCustomerContext(
        subject_id="u1", tenant_id="t1", customer_id="c1"
    )

    result = await rag_repository.search_cosine(
        context,
        RagSearchRequest(query="保固", limit=10),
        [1.0, 0.0, *(0.0 for _ in range(1022))],
    )

    assert [item.source_id for item in result.items] == [
        "tenant-public",
        "customer-private",
    ]
    assert result.items[0].score == pytest.approx(1.0)
    assert result.items[1].score == pytest.approx(0.8)
    assert all(item.retrieved_at is not None for item in result.items)
    assert all(item.effective_at is not None for item in result.items)
    assert all(item.valid_until is not None for item in result.items)
    assert all(len(item.content_checksum) == 64 for item in result.items)


@pytest.mark.asyncio
async def test_search_rejects_wrong_embedding_dimension(rag_repository):
    context = AuthorizedCustomerContext(
        subject_id="u1", tenant_id="t1", customer_id="c1"
    )

    with pytest.raises(ValueError, match="1024"):
        await rag_repository.search_cosine(
            context, RagSearchRequest(query="保固"), [1.0, 0.0]
        )


@pytest.mark.asyncio
async def test_search_rejects_zero_norm_before_database_access():
    pool = _FakePool("agent_flow_test")
    repository = RagRepository(pool)
    context = AuthorizedCustomerContext(
        subject_id="u1", tenant_id="t1", customer_id="c1"
    )

    with pytest.raises(ValueError, match="non-zero"):
        await repository.search_cosine(
            context,
            RagSearchRequest(query="保固"),
            [0.0 for _ in range(1024)],
        )
    assert pool.connection_requests == 0


@pytest.mark.asyncio
async def test_search_excludes_stored_zero_vectors(rag_repository, postgres_pool):
    await _insert_chunk(
        postgres_pool,
        document_id="00000000-0000-0000-0000-000000000008",
        chunk_id="10000000-0000-0000-0000-000000000008",
        tenant_id="t1",
        customer_id=None,
        source_id="zero-vector",
        content="零向量內容",
        embedding=_vector(0.0),
    )
    context = AuthorizedCustomerContext(
        subject_id="u1", tenant_id="t1", customer_id="c1"
    )

    result = await rag_repository.search_cosine(
        context,
        RagSearchRequest(query="保固", limit=10),
        [1.0, 0.0, *(0.0 for _ in range(1022))],
    )

    assert result.items == ()


@pytest.mark.asyncio
async def test_search_excludes_future_and_expired_documents(
    rag_repository, postgres_pool
):
    now = datetime.now(timezone.utc)
    await _insert_chunk(
        postgres_pool,
        document_id="00000000-0000-0000-0000-000000000011",
        chunk_id="10000000-0000-0000-0000-000000000011",
        tenant_id="t1",
        customer_id=None,
        source_id="active-now",
        content="目前有效內容",
        embedding=_vector(1.0),
        effective_at=now - timedelta(days=1),
        valid_until=now + timedelta(days=1),
    )
    await _insert_chunk(
        postgres_pool,
        document_id="00000000-0000-0000-0000-000000000012",
        chunk_id="10000000-0000-0000-0000-000000000012",
        tenant_id="t1",
        customer_id=None,
        source_id="future-document",
        content="未來內容",
        embedding=_vector(1.0),
        effective_at=now + timedelta(days=1),
        valid_until=now + timedelta(days=2),
    )
    await _insert_chunk(
        postgres_pool,
        document_id="00000000-0000-0000-0000-000000000013",
        chunk_id="10000000-0000-0000-0000-000000000013",
        tenant_id="t1",
        customer_id=None,
        source_id="expired-document",
        content="過期內容",
        embedding=_vector(1.0),
        effective_at=now - timedelta(days=2),
        valid_until=now - timedelta(days=1),
    )
    context = AuthorizedCustomerContext(
        subject_id="u1", tenant_id="t1", customer_id="c1"
    )

    result = await rag_repository.search_cosine(
        context,
        RagSearchRequest(query="內容", limit=10),
        [1.0, 0.0, *(0.0 for _ in range(1022))],
    )

    assert [item.source_id for item in result.items] == ["active-now"]


@pytest.mark.asyncio
async def test_repository_rejects_wrong_context_before_database_access(
):
    pool = _FakePool("agent_flow_test")
    repository = RagRepository(pool)
    with pytest.raises(TypeError, match="AuthorizedCustomerContext"):
        await repository.search_cosine(
            object(),
            RagSearchRequest(query="保固"),
            [1.0, 0.0, *(0.0 for _ in range(1022))],
        )
    assert pool.connection_requests == 0


class _FakeCursor:
    def __init__(self, row):
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
        return _FakeCursor(None)


class _FakeConnectionContext:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _FakePool:
    def __init__(self, database_name: str):
        self.fake_connection = _FakeConnection(database_name)
        self.connection_requests = 0

    def connection(self):
        self.connection_requests += 1
        return _FakeConnectionContext(self.fake_connection)


@pytest.mark.asyncio
@pytest.mark.parametrize("database_name", ["latest", "contest"])
async def test_rag_cleanup_rejects_ambiguous_database_names_without_delete(
    database_name,
):
    from conftest import _clear_rag_test_data

    pool = _FakePool(database_name)

    with pytest.raises(RuntimeError, match="test databases"):
        await _clear_rag_test_data(pool)

    assert len(pool.fake_connection.statements) == 1
    assert "current_database" in pool.fake_connection.statements[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("database_name", ["agent_test", "agent_flow_test"])
async def test_rag_cleanup_accepts_documented_test_database_names(database_name):
    from conftest import _clear_rag_test_data

    pool = _FakePool(database_name)

    await _clear_rag_test_data(pool)

    assert len(pool.fake_connection.statements) == 2
    assert "DELETE FROM rag.documents" in pool.fake_connection.statements[1]


@pytest.mark.asyncio
async def test_rag_cleanup_deletes_only_explicitly_owned_rows(postgres_pool):
    from conftest import _clear_rag_test_data

    sentinel_document_id = "00000000-0000-0000-0000-000000000009"
    await _insert_chunk(
        postgres_pool,
        document_id=sentinel_document_id,
        chunk_id="10000000-0000-0000-0000-000000000009",
        tenant_id="sentinel-tenant",
        customer_id=None,
        source_id="unrelated-sentinel",
        content="不得刪除的資料",
        embedding=_vector(1.0),
        pytest_owned=False,
    )
    await _insert_chunk(
        postgres_pool,
        document_id="00000000-0000-0000-0000-000000000010",
        chunk_id="10000000-0000-0000-0000-000000000010",
        tenant_id="owned-tenant",
        customer_id=None,
        source_id="owned-row",
        content="測試擁有的資料",
        embedding=_vector(1.0),
    )

    try:
        await _clear_rag_test_data(postgres_pool)
        async with postgres_pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT source_id FROM rag.documents "
                "WHERE id IN (%s, %s) ORDER BY source_id",
                (
                    UUID(sentinel_document_id),
                    UUID("00000000-0000-0000-0000-000000000010"),
                ),
            )
            assert [row["source_id"] for row in await cursor.fetchall()] == [
                "unrelated-sentinel"
            ]
    finally:
        async with postgres_pool.connection() as connection:
            await connection.execute(
                "DELETE FROM rag.documents WHERE id = %s",
                (UUID(sentinel_document_id),),
            )
