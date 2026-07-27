"""Tools come from config/tools.yaml, so a demo fixture and a live ERP are
interchangeable without touching the pipeline.

The http tests run against a real local HTTP server rather than a mocked
client: what matters is the request that actually leaves the process — the URL
it builds, the header it sends, and the fields it keeps.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agent_flow.adapters.tools import ToolSources
from agent_flow.auth import AuthorizedCustomerContext
from agent_flow.contracts import ToolCallRequest


@pytest.fixture
def context():
    return AuthorizedCustomerContext(subject_id="u1", tenant_id="t1", customer_id="c1")


def _config(tmp_path, body: str):
    path = tmp_path / "tools.yaml"
    path.write_text(body, encoding="utf-8")
    return path


FIXTURE = """
tools:
  order.lookup:
    type: fixture
    path: config/demo/tools.json
"""


@pytest.fixture
def erp():
    """A stand-in ERP. Records what it was asked, answers a nested body."""
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["path"] = self.path
            seen["auth"] = self.headers.get("Authorization")
            body = json.dumps({
                "data": {"state": "in_transit", "estimated_delivery": "2026-08-02"},
                "internal_cost": 41.5,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", seen
    server.shutdown()


def _http_config(tmp_path, base, extra=""):
    return _config(tmp_path, f"""
tools:
  order.lookup:
    type: http
    url: {base}/orders/{{order_id}}
    auth_header_env: TEST_ERP_KEY
    timeout_seconds: 5
{extra}
""")


MAPPED = """    map:
      status: data.state
      ships_on: data.estimated_delivery
"""


@pytest.mark.asyncio
async def test_a_fixture_tool_still_answers(tmp_path, context):
    tools = ToolSources.from_config(_config(tmp_path, FIXTURE))

    result = await tools.call(
        context, ToolCallRequest(tool="order.lookup", arguments={"order_id": "order-1"})
    )

    assert result.evidence.source_id == "tool:order.lookup"
    assert "in_transit" in result.evidence.content


@pytest.mark.asyncio
async def test_an_http_tool_answers_the_same_shape_as_a_fixture(
    tmp_path, context, erp, monkeypatch
):
    base, seen = erp
    monkeypatch.setenv("TEST_ERP_KEY", "erp-secret")
    tools = ToolSources.from_config(_http_config(tmp_path, base, MAPPED))

    result = await tools.call(
        context, ToolCallRequest(tool="order.lookup", arguments={"order_id": "order-3"})
    )

    # Identical evidence shape, so the validator and the citations cannot tell
    # a live ERP from the demo fixture.
    assert result.evidence.source_id == "tool:order.lookup"
    assert result.evidence.metadata["customer_id"] == "c1"
    assert json.loads(result.evidence.content) == {
        "status": "in_transit", "ships_on": "2026-08-02"
    }
    assert seen["auth"] == "Bearer erp-secret"


@pytest.mark.asyncio
async def test_only_mapped_fields_can_reach_the_customer(
    tmp_path, context, erp, monkeypatch
):
    base, _ = erp
    monkeypatch.setenv("TEST_ERP_KEY", "erp-secret")
    tools = ToolSources.from_config(_http_config(tmp_path, base, MAPPED))

    result = await tools.call(
        context, ToolCallRequest(tool="order.lookup", arguments={"order_id": "order-3"})
    )

    # The reply may quote anything in the evidence, so an unmapped internal
    # field must not be in it.
    assert "internal_cost" not in result.evidence.content


@pytest.mark.asyncio
async def test_an_argument_cannot_escape_its_place_in_the_url(
    tmp_path, context, erp, monkeypatch
):
    base, seen = erp
    monkeypatch.setenv("TEST_ERP_KEY", "erp-secret")
    tools = ToolSources.from_config(_http_config(tmp_path, base, MAPPED))

    # Arguments arrive from the model, so they are values, never path segments.
    await tools.call(
        context,
        ToolCallRequest(tool="order.lookup", arguments={"order_id": "../../admin"}),
    )

    assert "/orders/" in seen["path"]
    assert "/admin" not in seen["path"].split("?")[0].replace("%2F", "")


@pytest.mark.asyncio
async def test_an_unreachable_service_is_a_lookup_failure_not_a_leak(
    tmp_path, context
):
    tools = ToolSources.from_config(
        _http_config(tmp_path, "http://127.0.0.1:1")
    )

    with pytest.raises(LookupError) as raised:
        await tools.call(
            context, ToolCallRequest(tool="order.lookup", arguments={"order_id": "x"})
        )

    # The pipeline turns this into a safe error code; the upstream URL and body
    # must not travel with it.
    assert "127.0.0.1" not in str(raised.value)


def test_an_unknown_tool_type_names_the_types_that_exist(tmp_path):
    body = "tools:\n  weird:\n    type: telepathy\n"

    with pytest.raises(ValueError, match="fixture"):
        ToolSources.from_config(_config(tmp_path, body))


def test_the_committed_config_loads():
    assert ToolSources.from_config("config/tools.yaml")
