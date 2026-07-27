"""Knowledge comes from config/knowledge.yaml, not from one hard-coded file."""

import json

import pytest

from agent_flow.adapters.knowledge import KnowledgeSources
from agent_flow.auth import AuthorizedCustomerContext
from agent_flow.contracts import RagSearchRequest


def _config(tmp_path, body: str):
    path = tmp_path / "knowledge.yaml"
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture
def context():
    return AuthorizedCustomerContext(subject_id="u1", tenant_id="t1", customer_id="c1")


DEMO = """
sources:
  policies:
    type: fixture
    path: config/demo/rag.json
  groupbuy:
    type: fixture
    path: config/demo/groupbuy.json
"""


@pytest.mark.asyncio
async def test_a_query_reaches_the_source_that_owns_it(tmp_path, context):
    sources = KnowledgeSources.from_config(_config(tmp_path, DEMO))

    result = await sources.search(
        context, RagSearchRequest(query="groupbuy:coffee-2026-08", limit=5)
    )

    assert [item.source_id for item in result.items] == ["groupbuy:coffee-2026-08"]


@pytest.mark.asyncio
async def test_the_catalog_advertises_every_enabled_source(tmp_path, context):
    sources = KnowledgeSources.from_config(_config(tmp_path, DEMO))

    catalog = " ".join(sources.catalog())

    assert "policy:refund" in catalog
    assert "policy:shipping" in catalog
    assert "groupbuy:coffee-2026-08" in catalog


@pytest.mark.asyncio
async def test_a_disabled_source_is_not_searched(tmp_path, context):
    disabled = DEMO.replace(
        "    path: config/demo/groupbuy.json",
        "    path: config/demo/groupbuy.json\n    enabled: false",
    )
    sources = KnowledgeSources.from_config(_config(tmp_path, disabled))

    result = await sources.search(
        context, RagSearchRequest(query="groupbuy:coffee-2026-08", limit=5)
    )

    assert "groupbuy:coffee-2026-08" not in {item.source_id for item in result.items}
    assert "groupbuy" not in " ".join(sources.catalog())


def test_an_unknown_source_type_names_the_types_that_exist(tmp_path):
    body = "sources:\n  weird:\n    type: telepathy\n"

    with pytest.raises(ValueError, match="fixture"):
        KnowledgeSources.from_config(_config(tmp_path, body))


def test_the_committed_config_loads(context):
    sources = KnowledgeSources.from_config("config/knowledge.yaml")

    assert sources.catalog()


@pytest.fixture
def writable(tmp_path):
    """A corpus of our own, so a write test never edits the repo's fixtures."""
    corpus = tmp_path / "corpus.json"
    corpus.write_text(
        json.dumps(
            [
                {
                    "tenant_id": "t1",
                    "source_id": "policy:refund",
                    "version": "v1",
                    "content": "Refunds land in 5 working days.",
                }
            ]
        ),
        encoding="utf-8",
    )
    body = f"sources:\n  demo:\n    type: fixture\n    path: {corpus.as_posix()}\n"
    return KnowledgeSources.from_config(_config(tmp_path, body))


@pytest.mark.asyncio
async def test_a_new_document_is_retrievable_without_a_restart(writable, context):
    writable.put_document(
        "demo", "groupbuy:tea", "九月茶葉團購：烏龍 NT$500。", "t1", "v1"
    )

    result = await writable.search(
        context, RagSearchRequest(query="groupbuy:tea", limit=5)
    )

    assert [item.source_id for item in result.items] == ["groupbuy:tea"]
    # The classifier may only name a source it saw in the catalog.
    assert "groupbuy:tea" in " ".join(writable.catalog())


@pytest.mark.asyncio
async def test_replacing_a_document_changes_what_is_retrieved(writable, context):
    written = writable.put_document(
        "demo", "policy:refund", "Refunds now take 2 working days.", "t1", "v1"
    )

    assert written["replaced"] is True
    result = await writable.search(
        context, RagSearchRequest(query="policy:refund", limit=5)
    )
    assert result.items[0].content == "Refunds now take 2 working days."


@pytest.mark.asyncio
async def test_a_deleted_document_stops_being_retrievable(writable, context):
    writable.delete_document("demo", "policy:refund", "t1")

    result = await writable.search(
        context, RagSearchRequest(query="policy:refund", limit=5)
    )

    assert result.items == ()
    with pytest.raises(KeyError):
        writable.delete_document("demo", "policy:refund", "t1")


def test_another_tenants_document_is_neither_listed_nor_deletable(writable):
    writable.put_document("demo", "policy:refund", "Other tenant copy.", "t2", "v1")

    listed = writable.sources("t1")[0]["documents"]
    assert [d["source_id"] for d in listed] == ["policy:refund"]
    assert listed[0]["content"] == "Refunds land in 5 working days."

    with pytest.raises(KeyError):
        writable.delete_document("demo", "policy:refund", "t3")


def test_only_a_declared_source_can_be_written(writable):
    with pytest.raises(KeyError):
        writable.put_document("no_such_source", "x", "y", "t1", "v1")


def test_a_malformed_edit_leaves_the_previous_corpus_serving(writable, tmp_path):
    before = writable.catalog()
    (tmp_path / "corpus.json").write_text("{ not json", encoding="utf-8")

    # A half-written file must not take retrieval down.
    assert writable.catalog() == before
