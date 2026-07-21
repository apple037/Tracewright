from datetime import datetime, timezone
from uuid import UUID

import pytest
from psycopg.types.json import Jsonb

from agent_flow.auth import AuthorizedCustomerContext
from agent_flow.contracts import RagSearchRequest


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
) -> None:
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
                Jsonb({"kind": "test"}),
                datetime(2025, 1, 1, tzinfo=timezone.utc),
                datetime(2099, 1, 1, tzinfo=timezone.utc),
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
                tenant_id,
                customer_id,
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
