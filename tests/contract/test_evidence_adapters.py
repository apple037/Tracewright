import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_flow.adapters.evidence import MockRagClient, MockToolClient
from agent_flow.auth import AuthorizedCustomerContext
from agent_flow.contracts import RagSearchRequest, ToolCallRequest


@pytest.fixture
def authorized_context():
    return AuthorizedCustomerContext(
        subject_id="u1", tenant_id="t1", customer_id="c1"
    )


@pytest.fixture
def mock_tool():
    return MockToolClient.from_fixture("tests/fixtures/tools.json")


@pytest.mark.asyncio
async def test_rag_result_contains_complete_provenance(authorized_context):
    client = MockRagClient.from_fixture("tests/fixtures/rag.json")

    result = await client.search(
        authorized_context, RagSearchRequest(query="保固多久", limit=3)
    )

    item = result.items[0]
    assert item.source_id == "policy-1"
    assert item.version == "v1"
    assert item.retrieved_at is not None
    assert item.effective_at is not None
    assert item.valid_until is not None
    assert item.score == 1.0
    assert item.content_checksum == hashlib.sha256(
        "標準保固期為一年。".encode("utf-8")
    ).hexdigest()


@pytest.mark.asyncio
async def test_rag_filters_before_returning_other_tenant_or_customer_data():
    client = MockRagClient.from_fixture("tests/fixtures/rag.json")
    other_tenant = AuthorizedCustomerContext(
        subject_id="u2", tenant_id="t2", customer_id="c1"
    )

    result = await client.search(
        other_tenant, RagSearchRequest(query="保固多久", limit=3)
    )

    assert result.items == ()


@pytest.mark.asyncio
async def test_rag_filters_same_tenant_different_customer_data(authorized_context):
    client = MockRagClient.from_fixture("tests/fixtures/rag_authorization.json")

    result = await client.search(
        authorized_context, RagSearchRequest(query="專屬內容", limit=10)
    )

    assert [item.source_id for item in result.items] == ["customer-c1"]


@pytest.mark.asyncio
async def test_rag_applies_sql_equivalent_freshness_boundaries(authorized_context):
    as_of = datetime(2026, 1, 1, tzinfo=timezone.utc)
    client = MockRagClient.from_fixture(
        "tests/fixtures/rag_freshness.json", as_of=as_of
    )

    result = await client.search(
        authorized_context, RagSearchRequest(query="內容", limit=10)
    )

    assert [item.source_id for item in result.items] == [
        "active",
        "effective-boundary",
    ]
    assert all(item.retrieved_at == as_of for item in result.items)


@pytest.mark.asyncio
async def test_rag_rejects_wrong_context_before_record_access():
    client = MockRagClient.from_fixture("tests/fixtures/rag.json")

    with pytest.raises(TypeError, match="AuthorizedCustomerContext"):
        await client.search(object(), RagSearchRequest(query="保固"))


@pytest.mark.asyncio
async def test_tool_result_is_canonical_evidence(mock_tool, authorized_context):
    result = await mock_tool.call(
        authorized_context,
        ToolCallRequest(tool="order.lookup", arguments={"order_id": "o1"}),
    )

    canonical = json.dumps(
        {"delivery_date": None, "status": "in_transit"},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert result.tool == "order.lookup"
    assert result.evidence.content == canonical
    assert result.evidence.content_checksum == hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()
    assert result.evidence.source_id == "tool:order.lookup"
    assert result.evidence.version == "v1"
    assert result.evidence.retrieved_at is not None
    assert result.evidence.effective_at is not None
    assert result.evidence.valid_until is not None
    assert result.evidence.score == 1.0


@pytest.mark.asyncio
async def test_tool_rejects_cross_customer_fixture(mock_tool):
    wrong_customer = AuthorizedCustomerContext(
        subject_id="u2", tenant_id="t1", customer_id="c2"
    )

    with pytest.raises(LookupError, match="authorized scope"):
        await mock_tool.call(
            wrong_customer,
            ToolCallRequest(tool="order.lookup", arguments={"order_id": "o1"}),
        )


@pytest.mark.asyncio
async def test_tool_requires_authorized_context(mock_tool):
    with pytest.raises(TypeError):
        await mock_tool.call(
            ToolCallRequest(tool="order.lookup", arguments={"order_id": "o1"})
        )


@pytest.mark.asyncio
async def test_rag_returns_only_the_named_source_when_the_corpus_has_several(
    authorized_context,
):
    # The classifier picks a source_id out of catalog(); a corpus holding both
    # the refund and the shipping policy must not answer one with the other.
    client = MockRagClient.from_fixture("config/demo/rag.json")

    result = await client.search(
        authorized_context, RagSearchRequest(query="policy:shipping", limit=5)
    )

    assert [item.source_id for item in result.items] == ["policy:shipping"]


@pytest.mark.asyncio
async def test_demo_orders_cover_more_than_one_status(authorized_context):
    tools = MockToolClient.from_fixture("config/demo/tools.json")

    statuses = {}
    for order_id in ("order-1", "order-2", "order-3"):
        result = await tools.call(
            authorized_context,
            ToolCallRequest(tool="order.lookup", arguments={"order_id": order_id}),
        )
        statuses[order_id] = json.loads(result.evidence.content)["status"]

    assert statuses == {
        "order-1": "in_transit",
        "order-2": "delivered",
        "order-3": "preparing",
    }


def test_fixtures_are_committed_utf8_json():
    rag = json.loads(Path("tests/fixtures/rag.json").read_text(encoding="utf-8"))
    tools = json.loads(Path("tests/fixtures/tools.json").read_text(encoding="utf-8"))

    assert rag[0]["content"] == "標準保固期為一年。"
    assert tools[0]["result"]["status"] == "in_transit"
