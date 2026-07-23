import copy
from pathlib import Path

import httpx
import pytest


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def submission_payload(**overrides):
    payload = {
        "channel": "console",
        "external_message_id": "m1",
        "session_id": "s1",
        "text": "你好",
        "idempotency_key": "m1",
        "metadata": {"source": "trace-console"},
    }
    payload.update(overrides)
    return payload


async def create_submission(client, *, token):
    response = await client.post(
        "/api/v1/submissions",
        headers={"Authorization": f"Bearer {token}"},
        json=submission_payload(),
    )
    assert response.status_code == 202
    return response.json()


@pytest.mark.asyncio
async def test_submission_uses_customer_from_bearer_token(app_factory):
    app = app_factory()
    async with client_for(app) as client:
        response = await client.post(
            "/api/v1/submissions",
            headers={"Authorization": "Bearer customer"},
            json=submission_payload(),
        )
    assert response.status_code == 202
    assert set(response.json()) == {"submission_id", "trace_id", "status"}


@pytest.mark.asyncio
async def test_submission_rejects_missing_scope(app_factory):
    app = app_factory()
    async with client_for(app) as client:
        response = await client.post(
            "/api/v1/submissions",
            headers={"Authorization": "Bearer trace-only"},
            json=submission_payload(),
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_submission_replay_returns_same_receipt(app_factory):
    app = app_factory()
    payload = submission_payload(idempotency_key="same")
    async with client_for(app) as client:
        first = await client.post(
            "/api/v1/submissions",
            headers={"Authorization": "Bearer customer"}, json=payload,
        )
        second = await client.post(
            "/api/v1/submissions",
            headers={"Authorization": "Bearer customer"}, json=payload,
        )
    assert second.status_code == 202
    assert second.json() == first.json()


@pytest.mark.asyncio
async def test_other_customer_cannot_read_submission(app_factory):
    app = app_factory()
    async with client_for(app) as client:
        created = await create_submission(client, token="customer")
        response = await client.get(
            f"/api/v1/submissions/{created['submission_id']}",
            headers={"Authorization": "Bearer internal-c2"},
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_trace_list_is_tenant_and_customer_scoped(app_factory):
    app = app_factory()
    async with client_for(app) as client:
        await create_submission(client, token="customer")
        response = await client.get(
            "/api/v1/traces", headers={"Authorization": "Bearer internal-c2"},
        )
    assert response.status_code == 200
    assert response.json()["items"] == []


@pytest.mark.asyncio
async def test_turn_customer_is_bound_before_pipeline(app_factory, pipeline):
    app = app_factory()
    async with client_for(app) as client:
        response = await client.post(
            "/api/v1/turns",
            headers={"Authorization": "Bearer customer"},
            json={"customer_id": "c2", "session_id": "s1", "message": "hello"},
        )
    assert response.status_code == 403
    assert pipeline.traces.records == {}


@pytest.mark.asyncio
async def test_turn_runs_for_bound_customer(app_factory):
    app = app_factory()
    async with client_for(app) as client:
        response = await client.post(
            "/api/v1/turns",
            headers={"Authorization": "Bearer customer"},
            json={"session_id": "s1", "message": "where is my order"},
        )
    assert response.status_code == 200
    assert response.json()["assurance"]["mode"] == "reduced_assurance"


@pytest.mark.asyncio
async def test_trace_and_incremental_events_are_customer_scoped(app_factory, pipeline, context):
    from agent_flow.contracts import TurnRequest

    result = await pipeline.run(context, TurnRequest(session_id="trace-s", message="order status"))
    sensitive = {
        "token": "t", "access-token": "a", "refresh_token": "r",
        "cookie": "c", "set-cookie": "sc", "credential": "cred",
        "privateKey": "pk", "client_secret": "cs",
        "thinking": "think", "chain-of-thought": "cot", "rawPrompt": "prompt",
        "native_reasoning": "native", "hidden-reasoning": "hidden",
        "reasoningContent": "content",
        "decision_summary": {"reason_codes": ["SAFE_STRUCTURED_SUMMARY"]},
        "reason_codes": ["SAFE_TOP_LEVEL_REASON_CODE"],
        "nested": [{
            "authorization": "Bearer secret", "password": "pw",
            "db_password": "db-pw", "order-api-token": "order-token",
            "xApiKey": "api-key", "session_cookie": "session-cookie",
            "proxyAuthorization": "proxy-auth",
            "deeper": [{"model_reasoning": "hidden model reasoning"}],
        }],
    }
    pipeline.traces.records[result.trace_id].events[0].payload["adversarial"] = sensitive
    stored_before = copy.deepcopy(
        pipeline.traces.records[result.trace_id].events[0].payload
    )
    app = app_factory()
    async with client_for(app) as client:
        visible = await client.get(
            f"/api/v1/traces/{result.trace_id}", headers={"Authorization": "Bearer admin"}
        )
        events = await client.get(
            f"/api/v1/traces/{result.trace_id}/events?after_sequence=0",
            headers={"Authorization": "Bearer admin"},
        )
        customer_hidden = await client.get(
            f"/api/v1/traces/{result.trace_id}",
            headers={"Authorization": "Bearer customer"},
        )
        hidden = await client.get(
            f"/api/v1/traces/{result.trace_id}",
            headers={"Authorization": "Bearer other-tenant"},
        )
    assert visible.status_code == 200
    assert visible.json()["customer_id"] == "c1"
    for secret in ("Bearer secret", '"token":"t"', '"thinking":"think"', '"reasoningContent":"content"'):
        assert secret not in visible.text
        assert secret not in events.text
    assert "SAFE_STRUCTURED_SUMMARY" in visible.text
    assert "SAFE_STRUCTURED_SUMMARY" in events.text
    expected_filtered = {
        "decision_summary": {"reason_codes": ["SAFE_STRUCTURED_SUMMARY"]},
        "reason_codes": ["SAFE_TOP_LEVEL_REASON_CODE"],
        "nested": [{"deeper": [{}]}],
    }
    assert visible.json()["events"][0]["payload"]["adversarial"] == expected_filtered
    assert events.json()["events"][0]["payload"]["adversarial"] == expected_filtered
    assert pipeline.traces.records[result.trace_id].events[0].payload == stored_before
    assert events.status_code == 200
    assert all(item["sequence"] > 0 for item in events.json()["events"])
    assert customer_hidden.status_code == 403
    assert hidden.status_code == 404


@pytest.mark.asyncio
async def test_health_distinguishes_live_missing_invalid_and_ready(app_factory, invalid_artifact_root, tmp_path):
    cases = (
        (tmp_path, 503, "missing"),
        (invalid_artifact_root, 503, "invalid"),
        (Path("config"), 200, "ok"),
    )
    for root, expected_status, artifact_check in cases:
        app = app_factory(artifact_root=root)
        async with client_for(app) as client:
            live = await client.get("/health/live")
            ready = await client.get("/health/ready")
        assert live.status_code == 200
        assert ready.status_code == expected_status
        payload = ready.json()
        assert payload["checks"]["runtime_artifacts"] == artifact_check
        assert "secret" not in str(payload).lower()


@pytest.mark.asyncio
async def test_trace_scope_is_checked_before_fetch_and_customer_scope_reaches_repository(
    app_factory, pipeline, context
):
    from agent_flow.contracts import TurnRequest

    result = await pipeline.run(
        context, TurnRequest(session_id="scope-s", message="order status")
    )

    class SpyTraces:
        def __init__(self, delegate):
            self.delegate = delegate
            self.calls = []

        async def get_trace(self, trace_id, **scope):
            self.calls.append(("get_trace", scope))
            return await self.delegate.get_trace(trace_id, **scope)

        async def events_after(self, trace_id, **scope):
            self.calls.append(("events_after", scope))
            return await self.delegate.events_after(trace_id, **scope)

    spy = SpyTraces(pipeline.traces)
    app = app_factory(traces_override=spy)
    async with client_for(app) as client:
        forbidden = await client.get(
            f"/api/v1/traces/{result.trace_id}",
            headers={"Authorization": "Bearer customer"},
        )
        mismatch = await client.get(
            f"/api/v1/traces/{result.trace_id}",
            headers={"Authorization": "Bearer internal-c2"},
        )
        admin = await client.get(
            f"/api/v1/traces/{result.trace_id}",
            headers={"Authorization": "Bearer admin"},
        )
        mismatch_events = await client.get(
            f"/api/v1/traces/{result.trace_id}/events",
            headers={"Authorization": "Bearer internal-c2"},
        )
        admin_events = await client.get(
            f"/api/v1/traces/{result.trace_id}/events",
            headers={"Authorization": "Bearer admin"},
        )

    assert forbidden.status_code == 403
    assert mismatch.status_code == 404
    assert admin.status_code == 200
    assert mismatch_events.status_code == 404
    assert admin_events.status_code == 200
    assert spy.calls == [
        ("get_trace", {"tenant_id": "t1", "customer_id": "c2"}),
        ("get_trace", {"tenant_id": "t1", "customer_id": None}),
        ("get_trace", {"tenant_id": "t1", "customer_id": "c2"}),
        ("get_trace", {"tenant_id": "t1", "customer_id": None}),
        (
            "events_after",
            {"tenant_id": "t1", "customer_id": None, "after_sequence": 0},
        ),
    ]


@pytest.mark.asyncio
async def test_readiness_allowlists_names_and_normalizes_diagnostics(app_factory):
    app = app_factory(dependency_checks={
        "database": "postgres://user:password@host/db",
        "models": "ok",
        "webhook_secret": "top-secret",
        "arbitrary_url": "https://user:pass@example.test",
    })
    async with client_for(app) as client:
        response = await client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["checks"] == {
        "runtime_artifacts": "ok",
        "pipeline": "ok",
        "trace_repository": "ok",
        "database": "unavailable",
        "models": "ok",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dependency_checks", "expected_database", "expected_models"),
    (({}, "unavailable", "unavailable"), ({"database": "ok"}, "ok", "unavailable")),
)
async def test_readiness_requires_every_bootstrap_dependency(
    app_factory, dependency_checks, expected_database, expected_models
):
    app = app_factory(dependency_checks=dependency_checks)
    async with client_for(app) as client:
        response = await client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["database"] == expected_database
    assert response.json()["checks"]["models"] == expected_models
