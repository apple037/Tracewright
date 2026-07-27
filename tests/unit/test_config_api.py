"""The admin config endpoint.

It exposes prompt text and lets it be rewritten, so the scope check is the whole
security story here: a customer token must not be able to read the system
prompts, let alone change what the assistant is instructed to do.
"""

import pytest
from starlette.testclient import TestClient

from agent_flow.auth import AuthenticatedPrincipal
from agent_flow.main import create_app


ADMIN = AuthenticatedPrincipal(
    subject_id="admin", tenant_id="t1", customer_id="c1",
    scopes=frozenset({"turn:write", "trace:read", "trace:retry", "trace:admin"}),
)
CUSTOMER = AuthenticatedPrincipal(
    subject_id="customer", tenant_id="t1", customer_id="c1",
    scopes=frozenset({"turn:write", "trace:read", "trace:retry"}),
)


class FakeConfigService:
    def __init__(self):
        self.saved = []

    def artifacts(self):
        from agent_flow.artifacts import load_runtime_artifacts
        from pathlib import Path

        return load_runtime_artifacts(Path("config"))

    def model_summary(self):
        return {"roles": {}, "profiles": [], "disabled_roles": [], "config_path": "x"}

    def settings_summary(self):
        return {"history_turns": 8}

    def set_prompt(self, node, system_prompt):
        if node == "no_such_node":
            raise KeyError(node)
        self.saved.append((node, system_prompt))
        return {"node": node, "checksum": "a" * 64, "system_prompt": system_prompt,
                "edited": True}


def _client(service=None):
    async def authenticate(token):
        return {"admin": ADMIN, "customer": CUSTOMER}.get(token)

    return TestClient(
        create_app(authenticate=authenticate, runtime_config=service or FakeConfigService())
    )


def test_admin_sees_the_live_prompts_personas_and_models():
    body = _client().get(
        "/api/v1/config", headers={"Authorization": "Bearer admin"}
    ).json()

    nodes = {prompt["node"] for prompt in body["prompts"]}
    assert {"dialogue_classifier", "response_generator"} <= nodes
    generator = next(p for p in body["prompts"] if p["node"] == "response_generator")
    assert generator["system_prompt"]
    assert len(generator["checksum"]) == 64
    assert body["personas"][0]["style_prompt"]
    # The strategy vocabulary is surfaced so the panel can show what a prompt is
    # allowed to choose between.
    assert "business_first" in body["choices"]["response_modes"]


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("get", "/api/v1/config", None),
        ("put", "/api/v1/config/prompts/response_generator", {"system_prompt": "x"}),
        ("delete", "/api/v1/config/prompts/response_generator", None),
        ("put", "/api/v1/config/personas/familiar_companion.zh-TW", {"style_prompt": "x"}),
    ],
)
def test_a_customer_token_cannot_read_or_change_the_configuration(method, path, payload):
    service = FakeConfigService()
    client = _client(service)
    kwargs = {"headers": {"Authorization": "Bearer customer"}}
    if payload is not None:
        kwargs["json"] = payload
    response = getattr(client, method)(path, **kwargs)

    assert response.status_code == 403
    assert service.saved == []


def test_an_unknown_node_is_a_404_not_a_silent_write():
    service = FakeConfigService()
    response = _client(service).put(
        "/api/v1/config/prompts/no_such_node",
        headers={"Authorization": "Bearer admin"},
        json={"system_prompt": "x"},
    )
    assert response.status_code == 404
    assert service.saved == []


def test_unexpected_fields_are_rejected():
    response = _client().put(
        "/api/v1/config/prompts/response_generator",
        headers={"Authorization": "Bearer admin"},
        json={"system_prompt": "x", "guardrails": "off"},
    )
    assert response.status_code == 422


class FakeKnowledge:
    """Records writes; the corpus itself is exercised in test_knowledge_sources."""

    def __init__(self):
        self.written = []

    def sources(self, tenant_id):
        return [{"source": "demo", "tenant": tenant_id, "documents": []}]

    def put_document(self, source, source_id, content, tenant_id, version):
        if source == "no_such_source":
            raise KeyError(source)
        self.written.append((source, source_id, content, tenant_id, version))
        return {"source": source, "source_id": source_id, "replaced": False}

    def delete_document(self, source, source_id, tenant_id):
        raise KeyError(source_id)


def _knowledge_client(knowledge):
    async def authenticate(token):
        return {"admin": ADMIN, "customer": CUSTOMER}.get(token)

    return TestClient(
        create_app(
            authenticate=authenticate,
            runtime_config=FakeConfigService(),
            knowledge=knowledge,
        )
    )


def test_an_admin_can_add_a_document_and_the_tenant_comes_from_the_token():
    knowledge = FakeKnowledge()
    response = _knowledge_client(knowledge).put(
        "/api/v1/config/knowledge/demo/groupbuy:tea",
        headers={"Authorization": "Bearer admin"},
        json={"content": "九月茶葉團購：烏龍 NT$500。"},
    )

    assert response.status_code == 200
    # The body never carries a tenant_id; it is bound from the credential.
    assert knowledge.written == [
        ("demo", "groupbuy:tea", "九月茶葉團購：烏龍 NT$500。", "t1", "v1")
    ]


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("get", "/api/v1/config/knowledge", None),
        ("put", "/api/v1/config/knowledge/demo/x", {"content": "the moon is cheese"}),
        ("delete", "/api/v1/config/knowledge/demo/x", None),
    ],
)
def test_a_customer_token_cannot_read_or_change_the_knowledge(method, path, payload):
    # Whatever is in the corpus is stated to customers as fact and cited, so
    # this scope check is the boundary that keeps the assistant honest.
    knowledge = FakeKnowledge()
    kwargs = {"headers": {"Authorization": "Bearer customer"}}
    if payload is not None:
        kwargs["json"] = payload
    response = getattr(_knowledge_client(knowledge), method)(path, **kwargs)

    assert response.status_code == 403
    assert knowledge.written == []


def test_an_unknown_source_is_a_404_and_a_missing_document_is_too():
    client = _knowledge_client(FakeKnowledge())
    headers = {"Authorization": "Bearer admin"}

    assert client.put(
        "/api/v1/config/knowledge/no_such_source/x", headers=headers,
        json={"content": "x"},
    ).status_code == 404
    assert client.delete(
        "/api/v1/config/knowledge/demo/x", headers=headers
    ).status_code == 404


def test_knowledge_editing_is_unavailable_rather_than_crashing_without_a_corpus():
    response = _knowledge_client(None).get(
        "/api/v1/config/knowledge", headers={"Authorization": "Bearer admin"}
    )
    assert response.status_code == 503
