"""Knowledge comes from config/knowledge.yaml, not from one hard-coded file."""

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
