from uuid import uuid4

import pytest
import asyncio

from agent_flow.auth import AuthorizedCustomerContext
from agent_flow.contracts import TurnRequest
from agent_flow.pipeline.turn import TurnPipeline, TurnState
from agent_flow.errors import AgentError


@pytest.fixture
def trace_state():
    return TurnState(
        trace_id=uuid4(),
        context=AuthorizedCustomerContext(subject_id="u1", tenant_id="t1", customer_id="c1"),
        request=TurnRequest(session_id="s1", message="hello"),
    )


@pytest.fixture
def pipeline_for_run_node(trace_spy):
    return TurnPipeline(
        traces=trace_spy, conversations=None, handoffs=None, models=None,
        rag=None, tools=None, artifacts=None, clock=None, assurance_mode="bootstrap",
    )


def test_turn_pipeline_is_exported_from_pipeline_package():
    from agent_flow.pipeline import TurnPipeline as ExportedTurnPipeline

    assert ExportedTurnPipeline is TurnPipeline


@pytest.mark.asyncio
async def test_run_node_wraps_explicit_sync_and_async_closures(
    pipeline_for_run_node, trace_state, trace_spy
):
    seen = []

    def sync_operation():
        seen.append("sync")
        return "risk-result"

    async def async_operation():
        seen.append("async")
        return "model-result"

    assert await pipeline_for_run_node.run_node(trace_state, "risk_precheck", sync_operation) == "risk-result"
    assert await pipeline_for_run_node.run_node(
        trace_state,
        "dialogue_classifier",
        async_operation,
        trace_metadata={"prompt_ref": {"artifact_id": "classifier", "version": "1.0.0", "checksum": "a" * 64}},
    ) == "model-result"
    assert seen == ["sync", "async"]
    assert trace_spy.completed_nodes == ["risk_precheck", "dialogue_classifier"]
    assert trace_spy.events[-1].metadata["prompt_ref"]["checksum"] == "a" * 64


@pytest.mark.asyncio
async def test_run_node_rejects_prompt_bodies_from_trace_metadata(
    pipeline_for_run_node, trace_state
):
    with pytest.raises(ValueError, match="trace metadata"):
        await pipeline_for_run_node.run_node(
            trace_state, "response_generator", lambda: "draft",
            trace_metadata={"full_prompt": "hidden controller prompt"},
        )

    with pytest.raises(ValueError, match="trace metadata"):
        await pipeline_for_run_node.run_node(
            trace_state, "context_loader", lambda: "snapshot",
            trace_metadata={"snapshot_ref": {"persona": "hidden body"}},
        )


@pytest.mark.asyncio
async def test_run_node_copies_nested_artifact_metadata_before_operation(
    pipeline_for_run_node, trace_state, trace_spy
):
    ref = {"artifact_id": "response", "version": "1.0.0", "checksum": "a" * 64}

    def operation():
        ref["checksum"] = "b" * 64
        return "draft"

    await pipeline_for_run_node.run_node(
        trace_state, "response_generator", operation,
        trace_metadata={"prompt_ref": ref},
    )
    assert trace_spy.events[-1].metadata["prompt_ref"]["checksum"] == "a" * 64


@pytest.mark.asyncio
async def test_run_node_shields_cancelled_span_cleanup(
    pipeline_for_run_node, trace_state, trace_spy
):
    async def cancelled():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await pipeline_for_run_node.run_node(trace_state, "dialogue_classifier", cancelled)
    assert trace_spy.events[-1].status == "cancelled"
    assert trace_spy.finished[-1][1:] == ("cancelled", "CANCELLED")


@pytest.mark.asyncio
async def test_run_node_preserves_nested_concurrent_child_outcomes(
    pipeline_for_run_node, trace_state, trace_spy
):
    tool = AgentError.dependency(
        "EVIDENCE_SOURCE_FAILED", failure_stage="evidence_collector",
        component="tool", operation="order.lookup",
    )
    rag = AgentError.dependency(
        "EVIDENCE_SOURCE_FAILED", failure_stage="evidence_collector",
        component="rag", operation="shipping policy",
    )
    async def grouped():
        raise BaseExceptionGroup(
            "sources", [ExceptionGroup("failures", [tool, rag]), asyncio.CancelledError()]
        )
    with pytest.raises(AgentError) as caught:
        await pipeline_for_run_node.run_node(trace_state, "evidence_collector", grouped)
    assert caught.value.component == "tool"  # normalized component key is deterministic
    child = [e for e in trace_spy.events if e.event_type == "node_child"]
    assert [(e.component, e.status) for e in child] == [
        ("order_api", "failed"), ("rag", "failed"),
        ("evidence_collector", "cancelled"),
    ]
    assert trace_state.primary_failure_event_id == child[0].id


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["start_span", "started", "finish_span", "completed"])
async def test_run_node_recovers_cancellation_at_each_lifecycle_boundary(
    pipeline_for_run_node, trace_state, trace_spy, boundary
):
    original_start = trace_spy.start_span
    original_event = trace_spy.append_event
    original_finish = trace_spy.finish_span
    fired = False
    async def start(*args, **kwargs):
        nonlocal fired
        value = await original_start(*args, **kwargs)
        if boundary == "start_span" and not fired:
            fired = True
            raise asyncio.CancelledError
        return value
    async def event(**kwargs):
        nonlocal fired
        value = await original_event(**kwargs)
        if kwargs["status"] == boundary and not fired:
            fired = True
            raise asyncio.CancelledError
        return value
    async def finish(*args, **kwargs):
        nonlocal fired
        await original_finish(*args, **kwargs)
        if boundary == "finish_span" and args[1] == "completed" and not fired:
            fired = True
            raise asyncio.CancelledError
    trace_spy.start_span, trace_spy.append_event, trace_spy.finish_span = start, event, finish
    with pytest.raises(asyncio.CancelledError):
        await pipeline_for_run_node.run_node(trace_state, "risk_precheck", lambda: "ok")
    terminals = [e.status for e in trace_spy.events if e.status in {"completed", "cancelled"}]
    assert terminals == (["completed"] if boundary in {"finish_span", "completed"} else ["cancelled"])


@pytest.mark.asyncio
async def test_bounded_cleanup_cancels_and_awaits_hanging_task(pipeline_for_run_node):
    finished = asyncio.Event()
    async def hanging():
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()
    assert await pipeline_for_run_node._bounded_cleanup(hanging(), timeout=.01) is None
    assert finished.is_set()
