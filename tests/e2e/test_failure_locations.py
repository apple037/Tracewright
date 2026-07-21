import pytest

from agent_flow.contracts import TurnRequest
from agent_flow.errors import AgentError


@pytest.mark.asyncio
async def test_high_risk_handoffs_before_evidence_and_names_risk_node(pipeline, context, fake_models):
    fake_models.responses["dialogue_classifier"].clear()
    fake_models.responses["dialogue_classifier"].append({
        "intent": "support", "conversation_mode": "boundary", "urgency": "critical", "language": "zh-TW",
        "emotion": {"category": "fear_avoidance", "dialogue_stage": "surface", "override": "boundary", "response_mode": "brief_acknowledgment", "confidence": 1, "evidence_spans": [], "reason_codes": ["EXPLICIT_FEAR_AVOIDANCE"]},
    })
    result = await pipeline.run(context, TurnRequest(session_id="s1", message="我現在有危險"))
    trace = await pipeline.traces.get_trace(result.trace_id, tenant_id="t1")
    assert result.reply is None
    assert result.handoff_status == "queued"
    assert trace.issue_summary.error_code == "IMMEDIATE_DANGER"
    assert trace.issue_summary.failed_node == "risk_precheck"
    assert [s.node for s in trace.spans] == ["context_loader", "dialogue_classifier", "risk_precheck"]


@pytest.mark.asyncio
async def test_tool_timeout_trace_names_exact_operation(pipeline, context):
    class TimeoutTool:
        async def call(self, context, request):
            raise TimeoutError("order service timeout")

    pipeline.tools = TimeoutTool()
    result = await pipeline.run(context, TurnRequest(session_id="s1", message="查詢訂單 o1"))
    trace = await pipeline.traces.get_trace(result.trace_id, tenant_id="t1")
    assert result.handoff_status == "queued"
    assert trace.issue_summary.failed_node == "evidence_collector"
    assert trace.issue_summary.component == "order_api"
    assert trace.issue_summary.operation == "order.lookup"
    assert trace.primary_failure_event_id == next(
        e.id for e in trace.events if e.node == "evidence_collector" and e.kind == "failed"
    )


@pytest.mark.asyncio
async def test_insufficient_evidence_handoffs_at_validator(pipeline, context):
    original = pipeline.clock.value
    pipeline.clock.value = original.replace(hour=14)
    result = await pipeline.run(context, TurnRequest(session_id="s1", message="查詢訂單 o1"))
    trace = await pipeline.traces.get_trace(result.trace_id, tenant_id="t1")
    assert result.handoff_status == "queued"
    assert result.reply is None
    assert trace.issue_summary.error_code == "EVIDENCE_INSUFFICIENT"
    assert trace.issue_summary.failed_node == "evidence_validator"


@pytest.mark.asyncio
async def test_second_validation_failure_handoffs_without_persisting_draft(
    pipeline, context, fake_models
):
    failed = {
        "passed": False, "failed_criteria": ["UNCLEAR_RESPONSE"],
        "confidence": .7, "reason_codes": ["REPAIR_REQUIRED"],
    }
    fake_models.responses["response_judge"].clear()
    fake_models.responses["response_judge"].extend([failed, failed])
    fake_models.responses["response_generator"].append({
        "text": "訂單狀態仍在運送中。", "citations": ["tool-result-1"],
        "evidence_ids": ["tool-result-1"],
    })
    result = await pipeline.run(context, TurnRequest(session_id="s1", message="查詢訂單 o1"))
    trace = await pipeline.traces.get_trace(result.trace_id, tenant_id="t1")
    validators = [span for span in trace.spans if span.node == "response_validator"]
    assert result.handoff_status == "queued"
    assert result.reply is None
    assert pipeline.conversations.persisted == []
    assert trace.issue_summary.error_code == "VALIDATION_EXHAUSTED"
    assert trace.issue_summary.failed_node == "response_validator"
    assert [span.attempt for span in validators] == [1, 2]
    repair = next(e for e in trace.events if e.node == "response_repair" and e.kind == "completed")
    assert repair.metadata["prompt_ref"] == pipeline.artifacts.response_prompt.ref.model_dump(mode="json")
