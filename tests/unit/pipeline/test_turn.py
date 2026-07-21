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


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["failed_event", "failed_finish"])
async def test_ordinary_failure_bookkeeping_replays_postcommit_ack_loss(
    pipeline_for_run_node, trace_state, trace_spy, boundary
):
    original_event, original_finish = trace_spy.append_event, trace_spy.finish_span
    fired = False
    async def event(**kwargs):
        nonlocal fired
        result = await original_event(**kwargs)
        if boundary == "failed_event" and kwargs["status"] == "failed" and not fired:
            fired = True; raise OSError("failed event ack lost")
        return result
    async def finish(*args, **kwargs):
        nonlocal fired
        await original_finish(*args, **kwargs)
        if boundary == "failed_finish" and args[1] == "failed" and not fired:
            fired = True; raise OSError("failed finish ack lost")
    trace_spy.append_event, trace_spy.finish_span = event, finish
    error = AgentError.validation("BOOM", failure_stage="risk_precheck")
    with pytest.raises(AgentError):
        await pipeline_for_run_node.run_node(
            trace_state, "risk_precheck", lambda: (_ for _ in ()).throw(error)
        )
    failed = [e for e in trace_spy.events if e.status == "failed"]
    assert len(failed) == 1
    assert trace_state.primary_failure_event_id == failed[0].id


@pytest.mark.asyncio
async def test_group_child_event_ack_loss_preserves_exact_primary(
    pipeline_for_run_node, trace_state, trace_spy
):
    original = trace_spy.append_event
    fired = False
    async def event(**kwargs):
        nonlocal fired
        result = await original(**kwargs)
        if kwargs["event_type"] == "node_child" and not fired:
            fired = True; raise OSError("child ack lost")
        return result
    trace_spy.append_event = event
    causal = AgentError.dependency("SOURCE", failure_stage="evidence_collector", component="rag", operation="q")
    async def grouped(): raise ExceptionGroup("sources", [causal, ValueError("other")])
    with pytest.raises(AgentError):
        await pipeline_for_run_node.run_node(trace_state, "evidence_collector", grouped)
    primary = next(e for e in trace_spy.events if e.id == trace_state.primary_failure_event_id)
    assert primary.component == "rag"


@pytest.mark.asyncio
async def test_noncooperative_cleanup_is_owned_after_deadline_and_registry_drains(
    pipeline_for_run_node
):
    release = asyncio.Event()
    async def noncooperative():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
            raise RuntimeError("late cleanup failure")
    started = asyncio.get_running_loop().time()
    assert await pipeline_for_run_node._bounded_cleanup(noncooperative(), timeout=.01) is None
    assert asyncio.get_running_loop().time() - started < .1
    assert len(pipeline_for_run_node._cleanup_tasks) == 1
    release.set()
    for _ in range(10):
        if not pipeline_for_run_node._cleanup_tasks: break
        await asyncio.sleep(0)
    assert not pipeline_for_run_node._cleanup_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["ordinary_event", "ordinary_finish", "group_child_event", "group_finish"]
)
async def test_settle_failure_completes_via_bounded_cleanup_after_cancellation_exhausts_retry(
    pipeline_for_run_node, trace_state, trace_spy, mode
):
    original_event, original_finish = trace_spy.append_event, trace_spy.finish_span
    raises_left = 2  # exhaust _retry_idempotent's two attempts, forcing escape to _settle_failure_safely

    async def event(**kwargs):
        nonlocal raises_left
        hit = (
            (mode == "ordinary_event" and kwargs["event_type"] == "node" and kwargs["status"] == "failed")
            or (mode == "group_child_event" and kwargs["event_type"] == "node_child")
        )
        if hit and raises_left > 0:
            raises_left -= 1
            raise asyncio.CancelledError
        return await original_event(**kwargs)

    async def finish(*args, **kwargs):
        nonlocal raises_left
        if mode in {"ordinary_finish", "group_finish"} and args[1] == "failed" and raises_left > 0:
            raises_left -= 1
            raise asyncio.CancelledError
        return await original_finish(*args, **kwargs)

    trace_spy.append_event, trace_spy.finish_span = event, finish

    if mode in {"ordinary_event", "ordinary_finish"}:
        node_name = "risk_precheck"
        causal = AgentError.validation("BOOM", failure_stage="risk_precheck")
        async def operation():
            raise causal
    else:
        node_name = "evidence_collector"
        causal = AgentError.dependency("SOURCE", failure_stage="evidence_collector", component="rag", operation="q")
        async def operation():
            raise ExceptionGroup("sources", [causal, ValueError("other")])

    with pytest.raises(asyncio.CancelledError):
        await pipeline_for_run_node.run_node(trace_state, node_name, operation)

    assert raises_left == 0  # the injected cancellation actually fired and was exhausted
    failed = [e for e in trace_spy.events if e.status == "failed"]
    assert trace_state.primary_failure_event_id is not None
    primary = next(e for e in trace_spy.events if e.id == trace_state.primary_failure_event_id)
    if mode in {"ordinary_event", "ordinary_finish"}:
        assert len(failed) == 1
        assert primary is failed[0]
    else:
        child = [e for e in trace_spy.events if e.event_type == "node_child"]
        assert len(child) == 2  # no duplicate child events from the retried settlement
        assert primary.component == "rag"
    assert trace_spy.finished[-1][1:] == (
        "failed", "BOOM" if mode in {"ordinary_event", "ordinary_finish"} else "SOURCE"
    )


@pytest.mark.asyncio
async def test_mark_failure_allocates_synthetic_span_once_and_reuses_it(
    pipeline_for_run_node, trace_state, trace_spy
):
    start_calls = []
    original_start = trace_spy.start_span
    async def start(*args, **kwargs):
        start_calls.append(kwargs.get("span_id"))
        return await original_start(*args, **kwargs)
    trace_spy.start_span = start

    event_id = await pipeline_for_run_node._mark_failure(trace_state, "UNEXPECTED_ERROR", "pipeline")
    span_id = trace_state.spans["pipeline:1"]

    assert len(start_calls) == 1
    started = [e for e in trace_spy.events if e.status == "started" and e.span_id == span_id]
    failed = [e for e in trace_spy.events if e.status == "failed" and e.span_id == span_id]
    assert len(started) == 1
    assert len(failed) == 1 and failed[0].id == event_id
    assert trace_spy.finished[-1] == (span_id, "failed", "UNEXPECTED_ERROR")

    second_id = await pipeline_for_run_node._mark_failure(trace_state, "UNEXPECTED_ERROR", "pipeline")

    assert trace_state.spans["pipeline:1"] == span_id
    assert len(start_calls) == 1  # reused, not re-created
    assert second_id == event_id  # idempotent replay dedups to the same event


@pytest.mark.asyncio
async def test_single_leaf_group_emits_node_child_not_plain_node_event(
    pipeline_for_run_node, trace_state, trace_spy
):
    causal = AgentError.dependency(
        "SOURCE", failure_stage="evidence_collector", component="rag", operation="q"
    )
    async def grouped():
        raise BaseExceptionGroup("sources", [causal])
    with pytest.raises(AgentError):
        await pipeline_for_run_node.run_node(trace_state, "evidence_collector", grouped)
    node_failed = [e for e in trace_spy.events if e.event_type == "node" and e.status == "failed"]
    child = [e for e in trace_spy.events if e.event_type == "node_child"]
    assert node_failed == []
    assert len(child) == 1
    assert trace_state.primary_failure_event_id == child[0].id


@pytest.mark.asyncio
async def test_mark_failure_never_refinishes_a_reused_span_with_conflicting_values(
    pipeline_for_run_node, trace_state, trace_spy
):
    await pipeline_for_run_node._mark_failure(trace_state, "FIRST_REASON", "pipeline")
    span_id = trace_state.spans["pipeline:1"]
    assert trace_spy.finished == [(span_id, "failed", "FIRST_REASON")]

    await pipeline_for_run_node._mark_failure(trace_state, "SECOND_REASON", "pipeline")

    assert trace_state.spans["pipeline:1"] == span_id
    assert trace_spy.finished == [(span_id, "failed", "FIRST_REASON")]
