from pathlib import Path

import httpx
import pytest


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


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
    pipeline.traces.records[result.trace_id].events[0].payload.update(
        {"thinking": "hidden reasoning", "authorization": "Bearer secret"}
    )
    app = app_factory()
    async with client_for(app) as client:
        visible = await client.get(
            f"/api/v1/traces/{result.trace_id}", headers={"Authorization": "Bearer admin"}
        )
        events = await client.get(
            f"/api/v1/traces/{result.trace_id}/events?after_sequence=1",
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
    assert "hidden reasoning" not in visible.text
    assert "Bearer secret" not in visible.text
    assert events.status_code == 200
    assert all(item["sequence"] > 1 for item in events.json()["events"])
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
