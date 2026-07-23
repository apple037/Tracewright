from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from agent_flow.contracts import AssuranceMetadata, TurnRequest, TurnResult


class RecordingRetryPipeline:
    def __init__(self, original):
        self.traces = original.traces
        self.conversations = original.conversations
        self.artifacts = original.artifacts
        self.calls = []

    async def run(self, context, request, **options):
        self.calls.append((context, request, options))
        trace_id = await self.traces.start_trace(
            tenant_id=context.tenant_id, customer_id=context.customer_id,
            session_id=request.session_id, retry_of_trace_id=options["retry_of"],
            retry_initiator=options["retry_initiator"], retry_reason=options["retry_reason"],
            delivery_disposition=options["delivery_disposition"],
        )
        await self.traces.finish_trace(
            trace_id, "succeeded", tenant_id=context.tenant_id,
            terminal_outcome="reply", delivery_disposition=options["delivery_disposition"],
        )
        return TurnResult(
            trace_id=trace_id, text="review me",
            assurance=AssuranceMetadata(mode="reduced_assurance", judges=("response_judge",)),
        )


class LineageRacePipeline(RecordingRetryPipeline):
    async def run(self, context, request, **options):
        raise ValueError("retry lineage limit reached")


async def seed_source(pipeline, *, status="succeeded", age_days=0, retry_sequence=0):
    trace_id = await pipeline.traces.start_trace(
        tenant_id="t1", customer_id="c1", session_id="retry-s"
    )
    captured_at = pipeline.clock.now() - timedelta(days=age_days)
    await pipeline.conversations.capture_turn_input(
        tenant_id="t1", customer_id="c1", session_id="retry-s", trace_id=trace_id,
        request=TurnRequest(session_id="retry-s", message="where is my order"),
        captured_at=captured_at,
    )
    await pipeline.conversations.get_snapshot(
        tenant_id="t1", customer_id="c1", session_id="retry-s", trace_id=trace_id
    )
    pipeline.conversations.snapshots[trace_id] = pipeline.conversations.snapshots[trace_id].model_copy(
        update={"captured_at": captured_at}
    )
    refs = pipeline._artifact_metadata()
    span_id = await pipeline.traces.start_span(trace_id, "context_loader", tenant_id="t1")
    await pipeline.traces.append_event(
        trace_id=trace_id, span_id=span_id, tenant_id="t1", event_type="node",
        component="context_loader", status="started", payload={"node": "context_loader", "metadata": refs},
    )
    record = pipeline.traces.records[trace_id]
    record.retry_sequence = retry_sequence
    if status != "running":
        await pipeline.traces.finish_span(span_id, "completed", tenant_id="t1")
        await pipeline.traces.finish_trace(
            trace_id, status, tenant_id="t1", terminal_outcome="reply",
            delivery_disposition="deliver",
        )
    return trace_id


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_manual_retry_is_admin_only_and_review_required(app_factory, pipeline):
    source = await seed_source(pipeline)
    app = app_factory()
    body = {"reason": "operator verified transient failure"}
    async with client_for(app) as client:
        forbidden = await client.post(
            f"/api/v1/traces/{source}/retry", headers={"Authorization": "Bearer customer"}, json=body
        )
        accepted = await client.post(
            f"/api/v1/traces/{source}/retry", headers={"Authorization": "Bearer admin"}, json=body
        )
    assert forbidden.status_code == 403
    assert accepted.status_code == 202
    payload = accepted.json()
    assert payload["retry_of_trace_id"] == str(source)
    assert payload["delivery_disposition"] == "review_required"
    replay = pipeline.traces.records[__import__("uuid").UUID(payload["trace_id"])]
    assert replay.retry_of_trace_id == source
    assert replay.retry_initiator == "admin-u1"
    assert replay.retry_reason == "operator verified transient failure"
    assert replay.delivery_disposition == "review_required"
    assert replay.id not in pipeline.conversations.turns_by_trace
    assert pipeline.conversations.inputs[replay.id] == pipeline.conversations.inputs[source]
    assert pipeline.handoffs.items == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "expected"),
    (("active", 409), ("expired", 409), ("limit", 409), ("blank", 422)),
)
async def test_manual_retry_rejects_invalid_source(app_factory, pipeline, case, expected):
    source = await seed_source(
        pipeline,
        status="running" if case == "active" else "succeeded",
        age_days=31 if case == "expired" else 0,
        retry_sequence=3 if case == "limit" else 0,
    )
    if case == "limit":
        pipeline.traces.records[source].retry_sequence = 0
        for _ in range(3):
            sibling = await pipeline.traces.start_trace(
                tenant_id="t1", customer_id="c1", session_id="retry-s",
                retry_of_trace_id=source,
            )
            await pipeline.traces.finish_trace(
                sibling, "failed", tenant_id="t1", terminal_outcome="handoff",
                delivery_disposition="review_required",
            )
    recording = RecordingRetryPipeline(pipeline)
    app = app_factory(pipeline_override=recording)
    reason = " " if case == "blank" else "reviewed"
    async with client_for(app) as client:
        response = await client.post(
            f"/api/v1/traces/{source}/retry",
            headers={"Authorization": "Bearer admin"},
            json={"reason": reason},
        )
    assert response.status_code == expected
    assert recording.calls == []


@pytest.mark.asyncio
async def test_manual_retry_rejects_cross_tenant_and_unresolved_artifacts(app_factory, pipeline):
    source = await seed_source(pipeline)
    recording = RecordingRetryPipeline(pipeline)
    app = app_factory(pipeline_override=recording)
    body = {"reason": "reviewed"}
    async with client_for(app) as client:
        hidden = await client.post(
            f"/api/v1/traces/{source}/retry",
            headers={"Authorization": "Bearer other-tenant"}, json=body,
        )
        source_event = pipeline.traces.records[source].events[0]
        source_event.metadata["response_prompt_ref"]["checksum"] = "f" * 64
        unresolved = await client.post(
            f"/api/v1/traces/{source}/retry",
            headers={"Authorization": "Bearer admin"}, json=body,
        )
    assert hidden.status_code == 404
    assert unresolved.status_code == 409
    assert recording.calls == []


@pytest.mark.asyncio
async def test_manual_retry_maps_atomic_lineage_limit_race_to_conflict(app_factory, pipeline):
    source = await seed_source(pipeline)
    app = app_factory(pipeline_override=LineageRacePipeline(pipeline))
    async with client_for(app) as client:
        response = await client.post(
            f"/api/v1/traces/{source}/retry",
            headers={"Authorization": "Bearer admin"},
            json={"reason": "reviewed"},
        )
    assert response.status_code == 409
