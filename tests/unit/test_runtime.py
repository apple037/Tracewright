import json

import httpx
import pytest

from agent_flow.auth import AuthenticatedPrincipal, DemoTokenAuthenticator
from agent_flow.adapters.evidence import MockToolClient
from agent_flow.adapters.knowledge import KnowledgeSources
from agent_flow.config import Settings
from agent_flow.pipeline.turn import TurnPipeline
from agent_flow.runtime import build_demo_components, create_runtime_app


class FakePool:
    def __init__(self):
        self.open_calls = 0
        self.close_calls = 0

    async def open(self):
        self.open_calls += 1

    async def close(self):
        self.close_calls += 1


class FakeInventoryProbe:
    def __init__(self):
        self.failure = None

    async def probe_all(self):
        if self.failure is not None:
            raise self.failure
        return {}


@pytest.fixture
def settings():
    return Settings(database_url="postgresql://unused:unused@invalid/demo")


@pytest.fixture
def fake_pool():
    return FakePool()


@pytest.fixture
def opened_pool(fake_pool):
    return fake_pool


@pytest.fixture
def fake_inventory_probe():
    return FakeInventoryProbe()


@pytest.fixture
def runtime_app(settings, fake_pool, fake_inventory_probe):
    return create_runtime_app(
        settings=settings, pool=fake_pool, inventory_probe=fake_inventory_probe
    )


async def call_ready(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/health/ready")


async def test_demo_authenticator_uses_constant_time_static_tokens(settings):
    auth = DemoTokenAuthenticator.from_settings(settings)
    principal = await auth(settings.demo_customer_token.get_secret_value())
    assert principal == AuthenticatedPrincipal(
        subject_id="demo-customer",
        tenant_id=settings.demo_tenant_id,
        customer_id=settings.demo_customer_id,
        scopes=frozenset({"turn:write", "trace:read", "trace:retry"}),
    )
    assert await auth("wrong") is None


async def test_demo_admin_can_submit_and_inspect_as_demo_customer(settings):
    # Single-page demo: the admin token must both send chat (turn:write, bound to
    # the demo customer) and read raw reasoning (trace:admin).
    auth = DemoTokenAuthenticator.from_settings(settings)
    admin = await auth(settings.demo_admin_token.get_secret_value())
    assert admin == AuthenticatedPrincipal(
        subject_id="demo-admin",
        tenant_id=settings.demo_tenant_id,
        customer_id=settings.demo_customer_id,
        scopes=frozenset({"turn:write", "trace:read", "trace:retry", "trace:admin"}),
    )


async def test_production_mode_rejects_demo_tokens(settings):
    production = settings.model_copy(update={"app_runtime_mode": "production"})
    with pytest.raises(RuntimeError, match="demo authentication is disabled"):
        DemoTokenAuthenticator.from_settings(production)


async def test_demo_lifespan_opens_and_closes_pool_once(runtime_app, fake_pool):
    async with runtime_app.router.lifespan_context(runtime_app):
        assert fake_pool.open_calls == 1
        assert runtime_app.state.services.pipeline is not None
    assert fake_pool.close_calls == 1


async def test_runtime_builds_real_pipeline_and_mock_evidence_adapters(
    settings, opened_pool, fake_inventory_probe
):
    components = await build_demo_components(
        settings, opened_pool, inventory_probe=fake_inventory_probe
    )
    assert isinstance(components.pipeline, TurnPipeline)
    # Knowledge comes from config/knowledge.yaml, which is a set of sources
    # rather than one fixture file.
    assert isinstance(components.pipeline.rag, KnowledgeSources)
    assert isinstance(components.pipeline.tools, MockToolClient)
    # The model name is operator config; assert the role resolves through the
    # configured profile rather than pinning whichever model is in the file.
    registry = components.pipeline.models.registry
    resolved = registry.resolve("response_generator")
    profile = registry.config.profiles[registry.config.roles["response_generator"]]
    assert resolved.model == profile.model
    assert resolved.base_url.endswith("/v1")


async def test_required_probe_failure_marks_models_not_ready_without_content(
    runtime_app, fake_inventory_probe
):
    fake_inventory_probe.failure = RuntimeError("private model output must not appear")
    async with runtime_app.router.lifespan_context(runtime_app):
        response = await call_ready(runtime_app)
    assert response.status_code == 503
    body = response.json()
    assert body["checks"]["models"]["error_code"] == "MODEL_CAPABILITY_FAILED"
    assert "private model output" not in json.dumps(body)
