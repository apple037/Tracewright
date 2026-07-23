import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_flow.contracts import ResponseDraft, ValidationResult
from agent_flow.observability import (
    NodeTraceContext,
    OperationTelemetry,
    summarize_node_result,
)


class FakeTraces:
    def __init__(self):
        self.events = []

    async def append_event(self, **kwargs):
        event = SimpleNamespace(**kwargs)
        self.events.append(event)
        return event


@pytest.fixture
def telemetry():
    return OperationTelemetry(FakeTraces())


@pytest.fixture
def trace_context():
    return NodeTraceContext(
        trace_id=uuid4(),
        span_id=uuid4(),
        tenant_id="t1",
        node="response_generator",
        attempt=1,
    )


async def test_model_telemetry_records_profile_tokens_and_duration_only(
    telemetry, trace_context
):
    async with telemetry.bind_node(trace_context):
        await telemetry.record_model(
            role="response_generator",
            profile="local_generator",
            model="Qwen/Qwen3-8B-AWQ",
            duration_ms=42,
            input_tokens=120,
            output_tokens=36,
            finish_reason="stop",
            status="completed",
        )
    event = telemetry.traces.events[-1]
    assert event.event_type == "model_call"
    assert event.payload["model_role"] == "response_generator"
    assert event.payload["duration_ms"] == 42
    assert "text" not in event.payload
    assert "reasoning" not in json.dumps(event.payload)


async def test_tool_telemetry_hashes_arguments_without_storing_values(
    telemetry, trace_context
):
    async with telemetry.bind_node(trace_context):
        await telemetry.record_tool(
            tool="order.lookup",
            arguments={"order_id": "private-order-1"},
            duration_ms=12,
            status="completed",
            freshness_seconds=60,
        )
    payload = telemetry.traces.events[-1].payload
    assert payload["tool"] == "order.lookup"
    assert len(payload["argument_fingerprint"]) == 64
    assert "private-order-1" not in json.dumps(payload)


async def test_rag_telemetry_hashes_query_and_records_sources(
    telemetry, trace_context
):
    async with telemetry.bind_node(trace_context):
        await telemetry.record_rag(
            query="private customer question",
            result_count=2,
            source_ids=["policy:refund"],
            duration_ms=9,
            status="completed",
            freshness_seconds=120,
        )
    payload = telemetry.traces.events[-1].payload
    assert payload["result_count"] == 2
    assert payload["source_ids"] == ["policy:refund"]
    assert len(payload["query_fingerprint"]) == 64
    assert "private customer question" not in json.dumps(payload)


async def test_missing_active_context_is_a_noop(telemetry):
    await telemetry.record_model(
        role="response_generator",
        profile="local_generator",
        model="Qwen/Qwen3-8B-AWQ",
        duration_ms=1,
        input_tokens=1,
        output_tokens=1,
        finish_reason="stop",
        status="completed",
    )
    assert telemetry.traces.events == []


def test_node_summary_contains_decision_codes_but_not_draft_text():
    summary = summarize_node_result(
        "response_validator",
        ValidationResult(
            passed=True,
            failed_criteria=(),
            confidence=0.9,
            reason_codes=("GROUNDED",),
            assurance="reduced_assurance",
        ),
    )
    assert summary["decision_summary"] == "response accepted"
    assert summary["reason_codes"] == ["GROUNDED"]
    assert "text" not in summary


def test_response_generator_summary_keeps_ids_not_text():
    summary = summarize_node_result(
        "response_generator",
        ResponseDraft(
            text="private draft text",
            citations=("policy:refund",),
            evidence_ids=("rag:policy:refund:abc",),
        ),
    )
    assert summary["citations"] == ["policy:refund"]
    assert summary["evidence_ids"] == ["rag:policy:refund:abc"]
    assert "private draft text" not in json.dumps(summary)


def test_unknown_node_returns_empty_summary():
    assert summarize_node_result("context_loader", object()) == {}
