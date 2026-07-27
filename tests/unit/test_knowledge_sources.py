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

    catalog = " ".join(await sources.catalog())

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
    assert "groupbuy" not in " ".join(await sources.catalog())


def test_an_unknown_source_type_names_the_types_that_exist(tmp_path):
    body = "sources:\n  weird:\n    type: telepathy\n"

    with pytest.raises(ValueError, match="fixture"):
        KnowledgeSources.from_config(_config(tmp_path, body))


@pytest.mark.asyncio
async def test_the_committed_config_loads(context):
    sources = KnowledgeSources.from_config("config/knowledge.yaml")

    assert await sources.catalog()


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
    assert "groupbuy:tea" in " ".join(await writable.catalog())


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


@pytest.mark.asyncio
async def test_a_malformed_edit_leaves_the_previous_corpus_serving(writable, tmp_path):
    before = await writable.catalog()
    (tmp_path / "corpus.json").write_text("{ not json", encoding="utf-8")

    # A half-written file must not take retrieval down.
    assert await writable.catalog() == before


@pytest.mark.asyncio
async def test_an_expired_document_is_neither_offered_nor_retrieved(context):
    sources = KnowledgeSources.from_config("config/knowledge.yaml")

    # The demo corpus carries a promotion that ended in 2025. The classifier may
    # only name a source it saw in the catalog, so an expired document that is
    # still advertised gets named, matches nothing, and retrieval falls back to
    # the whole corpus — the assistant then answers from an unrelated document.
    assert "promo:summer-2025" not in " ".join(await sources.catalog())

    result = await sources.search(
        context, RagSearchRequest(query="promo:summer-2025", limit=5)
    )

    assert "promo:summer-2025" not in {item.source_id for item in result.items}


@pytest.mark.asyncio
async def test_a_document_bound_to_one_customer_stays_with_them(context):
    sources = KnowledgeSources.from_config("config/knowledge.yaml")
    other = AuthorizedCustomerContext(
        subject_id="u2", tenant_id="t1", customer_id="c2"
    )

    mine = await sources.search(
        context, RagSearchRequest(query="account:c1:coupon", limit=5)
    )
    theirs = await sources.search(
        other, RagSearchRequest(query="account:c1:coupon", limit=5)
    )

    assert "account:c1:coupon" in {item.source_id for item in mine.items}
    assert "account:c1:coupon" not in {item.source_id for item in theirs.items}


@pytest.fixture
def kb():
    """A stand-in knowledge base: a catalog endpoint and a document endpoint."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    state = {"down": False, "hits": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["hits"] += 1
            if state["down"]:
                self.send_response(503)
                self.end_headers()
                return
            if self.path.startswith("/documents/"):
                body = json.dumps({
                    "source_id": "policy:refund", "version": "v2",
                    "content": "Refunds land in 5 working days.",
                }).encode()
            else:
                body = json.dumps({"documents": [
                    {"source_id": "policy:refund", "summary": "退款政策"},
                ]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", state
    server.shutdown()


def _kb_config(tmp_path, base, extra=""):
    return _config(tmp_path, f"""
sources:
  handbook:
    type: http
    catalog_url: {base}/documents
    document_url: {base}/documents/{{source_id}}
{extra}
""")


@pytest.mark.asyncio
async def test_a_knowledge_base_supplies_the_catalog_and_the_document(
    tmp_path, context, kb
):
    base, _ = kb
    sources = KnowledgeSources.from_config(_kb_config(tmp_path, base))

    assert "policy:refund: 退款政策" in await sources.catalog()

    result = await sources.search(
        context, RagSearchRequest(query="policy:refund", limit=5)
    )

    # Identical evidence shape to a fixture, so citations and validation cannot
    # tell where the document came from.
    assert result.items[0].source_id == "policy:refund"
    assert result.items[0].content == "Refunds land in 5 working days."
    assert result.items[0].evidence_id.startswith("rag:policy:refund:v2:")


@pytest.mark.asyncio
async def test_the_catalog_is_cached_rather_than_fetched_every_turn(
    tmp_path, context, kb
):
    base, state = kb
    sources = KnowledgeSources.from_config(
        _kb_config(tmp_path, base, "    cache_seconds: 60\n")
    )

    await sources.catalog()
    hits = state["hits"]
    await sources.catalog()

    # Every turn asks for the catalog; a knowledge base does not gain documents
    # by the second.
    assert state["hits"] == hits


@pytest.mark.asyncio
async def test_a_knowledge_base_that_goes_down_keeps_the_last_catalog(
    tmp_path, context, kb
):
    base, state = kb
    sources = KnowledgeSources.from_config(
        _kb_config(tmp_path, base, "    cache_seconds: 0\n")
    )
    before = await sources.catalog()
    state["down"] = True

    # An empty catalog would silently turn every grounded answer into "I don't
    # know", which looks like a model problem and is not one.
    assert await sources.catalog() == before

    result = await sources.search(
        context, RagSearchRequest(query="policy:refund", limit=5)
    )
    # Retrieval itself fails safe: nothing retrieved, so nothing invented.
    assert result.items == ()


@pytest.mark.asyncio
async def test_a_knowledge_source_and_a_local_fixture_answer_together(
    tmp_path, context, kb
):
    base, _ = kb
    body = f"""
sources:
  handbook:
    type: http
    catalog_url: {base}/documents
    document_url: {base}/documents/{{source_id}}
  account:
    type: fixture
    path: config/demo/account.json
"""
    sources = KnowledgeSources.from_config(_config(tmp_path, body))

    catalog = " ".join(await sources.catalog())

    assert "policy:refund" in catalog
    assert "account:c1:coupon" in catalog


@pytest.mark.asyncio
async def test_an_address_can_come_from_the_environment(tmp_path, context, kb, monkeypatch):
    # The same committed YAML runs on the host and in a container, where the
    # knowledge base is not on localhost. Without expansion the URL reaches
    # httpx as the literal "${...}" and every retrieval silently returns
    # nothing — which reads as a model problem and is not one.
    base, _ = kb
    monkeypatch.setenv("TEST_KB_URL", base)
    body = """
sources:
  handbook:
    type: http
    catalog_url: ${TEST_KB_URL:-http://unused}/documents
    document_url: ${TEST_KB_URL:-http://unused}/documents/{source_id}
"""
    sources = KnowledgeSources.from_config(_config(tmp_path, body))

    assert "policy:refund" in " ".join(await sources.catalog())
