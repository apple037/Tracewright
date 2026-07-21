from uuid import uuid4

import pytest

from agent_flow.auth import AuthorizedCustomerContext
from agent_flow.contracts import TurnRequest
from agent_flow.pipeline.turn import TurnPipeline, TurnState


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
