from starlette.testclient import TestClient

from agent_flow.main import create_app


def _client():
    return TestClient(create_app())


def test_console_and_declared_assets_are_served():
    client = _client()
    assert client.get("/console/").status_code == 200
    assert client.get("/console/styles.css").headers["content-type"].startswith(
        "text/css"
    )
    assert client.get("/console/api.js").headers["content-type"].startswith(
        "text/javascript"
    )
    assert client.get("/console/chat.html").status_code == 200
    assert client.get("/console/chat.js").headers["content-type"].startswith(
        "text/javascript"
    )
    assert client.get("/console/i18n.js").headers["content-type"].startswith(
        "text/javascript"
    )


def test_console_does_not_expose_parent_files():
    client = _client()
    response = client.get("/console/%2e%2e/%2e%2e/.env")
    assert response.status_code in {404, 405}
    assert "DATABASE_URL" not in response.text
