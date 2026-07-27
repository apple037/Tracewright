"""The generated API documentation.

/docs is the front door for anyone calling this service from their own code, so
what it advertises is part of the contract: a way to authorise, and a shape for
every endpoint. A raw `authorization` header parameter renders as sixteen boxes
to paste the same token into; a declared security scheme renders as one
Authorize button.
"""

from starlette.testclient import TestClient

from agent_flow.main import create_app


def _schema():
    return TestClient(create_app()).get("/openapi.json").json()


def test_the_docs_page_is_served():
    response = TestClient(create_app()).get("/docs")

    assert response.status_code == 200


def test_a_bearer_scheme_is_declared_so_docs_can_authorise():
    schemes = _schema()["components"]["securitySchemes"]

    assert any(
        scheme.get("type") == "http" and scheme.get("scheme") == "bearer"
        for scheme in schemes.values()
    )


def test_every_authenticated_route_advertises_that_it_needs_the_token():
    schema = _schema()
    unsecured = [
        f"{method.upper()} {path}"
        for path, operations in schema["paths"].items()
        for method, operation in operations.items()
        if path.startswith("/api/") and not operation.get("security")
    ]

    assert unsecured == []


def test_no_route_still_asks_for_a_raw_authorization_header():
    schema = _schema()
    raw = [
        f"{method.upper()} {path}"
        for path, operations in schema["paths"].items()
        for method, operation in operations.items()
        for parameter in operation.get("parameters", [])
        if parameter.get("name", "").lower() == "authorization"
    ]

    assert raw == []


def test_routes_are_grouped_rather_than_listed_flat():
    schema = _schema()
    tags = {
        tag
        for operations in schema["paths"].values()
        for operation in operations.values()
        for tag in operation.get("tags", [])
    }

    assert {"Messages", "Conversations", "Traces", "Configuration", "Health"} <= tags


def test_the_landing_text_says_how_to_get_a_reply():
    description = _schema()["info"]["description"]

    # Submitting is asynchronous; a reader who misses that polls nothing and
    # concludes the API is broken.
    assert "/api/v1/submissions" in description
    assert "Authorize" in description
