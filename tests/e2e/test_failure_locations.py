import pytest

from agent_flow.contracts import TurnRequest
from agent_flow.errors import AgentError


@pytest.mark.asyncio
async def test_turn_input_capture_failure_is_traced_at_first_context_node(
    pipeline, context
):
    async def fail_capture(**kwargs):
        raise OSError("turn input store unavailable")

    pipeline.conversations.capture_turn_input = fail_capture
    result = await pipeline.run(
        context, TurnRequest(session_id="s1", message="where is my order")
    )
    trace = await pipeline.traces.get_trace(result.trace_id, tenant_id="t1")

    assert result.handoff_status == "queued"
    assert [span.node for span in trace.spans] == ["context_loader"]
    assert trace.issue_summary.failed_node == "context_loader"


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
    risk_span = next(s for s in trace.spans if s.node == "risk_precheck")
    assert risk_span.status == "failed"
    assert [e.kind for e in trace.events if e.span_id == risk_span.id][-1] == "failed"
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
async def test_evidence_failure_records_cancelled_sibling_source(
    pipeline, context, monkeypatch
):
    import asyncio
    from agent_flow.contracts import EvidencePlan, EvidenceToolCall
    monkeypatch.setattr(
        "agent_flow.pipeline.turn.plan_evidence",
        lambda classification: EvidencePlan(
            rag_queries=("shipping policy",),
            tool_calls=(EvidenceToolCall(
                operation="order.lookup", arguments={"order_id": "current"},
                freshness_seconds=60,
            ),),
        ),
    )
    class BlockingRag:
        async def search(self, context, request):
            await asyncio.Event().wait()
    class FailedTool:
        async def call(self, context, request):
            raise TimeoutError("tool failed")
    pipeline.rag, pipeline.tools = BlockingRag(), FailedTool()
    result = await pipeline.run(context, TurnRequest(session_id="s1", message="查詢訂單 o1"))
    trace = pipeline.traces.records[result.trace_id]
    child = [e for e in trace.events if e.event_type == "node_child"]
    assert [(e.component, e.kind) for e in child] == [
        ("order_api", "failed"), ("rag", "cancelled")
    ]
    assert trace.primary_failure_event_id == child[0].id


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
    assert [span.status for span in validators] == ["completed", "failed"]
    repair = next(e for e in trace.events if e.node == "response_repair" and e.kind == "completed")
    assert repair.metadata["prompt_ref"] == pipeline.artifacts.response_prompt.ref.model_dump(mode="json")


@pytest.mark.asyncio
async def test_conversation_persistence_failure_never_marks_trace_success(
    pipeline, context
):
    async def fail_append(**turn):
        raise OSError("conversation store unavailable")

    pipeline.conversations.append_turn = fail_append
    result = await pipeline.run(context, TurnRequest(session_id="s1", message="查詢訂單 o1"))
    trace = await pipeline.traces.get_trace(result.trace_id, tenant_id="t1")
    assert result.handoff_status == "queued"
    assert trace.status == "failed"
    assert trace.terminal_outcome == "handoff"
    assert trace.issue_summary.failed_node == "conversation_persistence"


@pytest.mark.asyncio
async def test_handoff_finish_retry_does_not_enqueue_twice(
    pipeline, context, fake_models
):
    fake_models.responses["dialogue_classifier"].clear()
    fake_models.responses["dialogue_classifier"].append({
        "intent": "support", "conversation_mode": "boundary", "urgency": "critical", "language": "zh-TW",
        "emotion": {"category": "fear_avoidance", "dialogue_stage": "surface", "override": "boundary", "response_mode": "brief_acknowledgment", "confidence": 1, "evidence_spans": [], "reason_codes": ["EXPLICIT_FEAR_AVOIDANCE"]},
    })
    original = pipeline.traces.finish_trace
    attempts = 0

    async def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("lost finish acknowledgement")
        await original(*args, **kwargs)

    pipeline.traces.finish_trace = fail_once
    result = await pipeline.run(context, TurnRequest(session_id="s1", message="我現在有危險"))
    assert result.handoff_status == "queued"
    assert len(pipeline.handoffs.items) == 1
    assert pipeline.handoffs.items[0]["idempotency_key"] == str(result.trace_id)


@pytest.mark.asyncio
async def test_handoff_postcommit_ack_loss_replays_without_duplicate(
    pipeline, context, fake_models
):
    fake_models.responses["dialogue_classifier"].clear()
    fake_models.responses["dialogue_classifier"].append({
        "intent": "support", "conversation_mode": "boundary", "urgency": "critical", "language": "zh-TW",
        "emotion": {"category": "fear_avoidance", "dialogue_stage": "surface", "override": "boundary", "response_mode": "brief_acknowledgment", "confidence": 1, "evidence_spans": [], "reason_codes": ["EXPLICIT_FEAR_AVOIDANCE"]},
    })
    original = pipeline.traces.finish_trace
    first = True
    async def committed_then_lost(*args, **kwargs):
        nonlocal first
        await original(*args, **kwargs)
        if first:
            first = False
            raise OSError("ack lost after commit")
    pipeline.traces.finish_trace = committed_then_lost
    result = await pipeline.run(context, TurnRequest(session_id="s1", message="我現在有危險"))
    assert result.handoff_status == "queued"
    assert len(pipeline.handoffs.items) == 1


@pytest.mark.asyncio
async def test_handoff_precommit_enqueue_failure_retries_same_key(
    pipeline, context, fake_models
):
    fake_models.responses["dialogue_classifier"].clear()
    fake_models.responses["dialogue_classifier"].append({
        "intent": "support", "conversation_mode": "boundary", "urgency": "critical", "language": "zh-TW",
        "emotion": {"category": "fear_avoidance", "dialogue_stage": "surface", "override": "boundary", "response_mode": "brief_acknowledgment", "confidence": 1, "evidence_spans": [], "reason_codes": ["EXPLICIT_FEAR_AVOIDANCE"]},
    })
    original = pipeline.handoffs.enqueue
    calls = []
    async def fail_before_commit(**item):
        calls.append(item["idempotency_key"])
        if len(calls) == 1:
            raise OSError("precommit failure")
        return await original(**item)
    pipeline.handoffs.enqueue = fail_before_commit
    result = await pipeline.run(context, TurnRequest(session_id="s1", message="我現在有危險"))
    assert result.handoff_status == "queued"
    assert calls == [str(result.trace_id), str(result.trace_id)]
    assert len(pipeline.handoffs.items) == 1


@pytest.mark.asyncio
async def test_handoff_postcommit_enqueue_ack_loss_is_deduplicated(
    pipeline, context, fake_models
):
    fake_models.responses["dialogue_classifier"].clear()
    fake_models.responses["dialogue_classifier"].append({
        "intent": "support", "conversation_mode": "boundary", "urgency": "critical", "language": "zh-TW",
        "emotion": {"category": "fear_avoidance", "dialogue_stage": "surface", "override": "boundary", "response_mode": "brief_acknowledgment", "confidence": 1, "evidence_spans": [], "reason_codes": ["EXPLICIT_FEAR_AVOIDANCE"]},
    })
    original = pipeline.handoffs.enqueue
    calls = 0
    async def commit_then_lose(**item):
        nonlocal calls
        calls += 1
        result = await original(**item)
        if calls == 1:
            raise OSError("enqueue acknowledgement lost")
        return result
    pipeline.handoffs.enqueue = commit_then_lose
    result = await pipeline.run(context, TurnRequest(session_id="s1", message="我現在有危險"))
    assert result.handoff_status == "queued"
    assert calls == 2
    assert len(pipeline.handoffs.items) == 1


@pytest.mark.asyncio
async def test_external_cancellation_finalizes_trace_and_reraises(pipeline, context):
    async def cancel_model(role, request, response_type):
        raise __import__("asyncio").CancelledError
    pipeline.models.structured = cancel_model
    with pytest.raises(__import__("asyncio").CancelledError):
        await pipeline.run(context, TurnRequest(session_id="s1", message="查詢訂單 o1"))
    trace = next(iter(pipeline.traces.records.values()))
    assert trace.status == "failed"
    assert trace.terminal_outcome == "cancelled"
    classifier = next(s for s in trace.spans if s.node == "dialogue_classifier")
    assert classifier.status == "cancelled"


@pytest.mark.asyncio
async def test_cancellation_during_trace_cleanup_is_retried_bounded(pipeline, context):
    import asyncio
    async def cancel_model(role, request, response_type):
        raise asyncio.CancelledError
    pipeline.models.structured = cancel_model
    original = pipeline.traces.finish_trace
    calls = 0
    async def cleanup_cancel_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise asyncio.CancelledError
        return await original(*args, **kwargs)
    pipeline.traces.finish_trace = cleanup_cancel_once
    with pytest.raises(asyncio.CancelledError):
        await pipeline.run(context, TurnRequest(session_id="s1", message="查詢訂單 o1"))
    trace = next(iter(pipeline.traces.records.values()))
    assert calls == 2
    assert trace.status == "failed"


@pytest.mark.asyncio
async def test_external_evidence_cancellation_records_each_running_child(
    pipeline, context, monkeypatch
):
    import asyncio
    from agent_flow.contracts import EvidencePlan, EvidenceToolCall
    monkeypatch.setattr(
        "agent_flow.pipeline.turn.plan_evidence",
        lambda classification: EvidencePlan(
            rag_queries=("shipping policy",),
            tool_calls=(EvidenceToolCall(operation="order.lookup", arguments={"order_id": "current"}, freshness_seconds=60),),
        ),
    )
    both_started = asyncio.Event()
    count = 0
    async def block():
        nonlocal count
        count += 1
        if count == 2: both_started.set()
        await asyncio.Event().wait()
    class Rag:
        async def search(self, context, request): await block()
    class Tool:
        async def call(self, context, request): await block()
    pipeline.rag, pipeline.tools = Rag(), Tool()
    task = asyncio.create_task(pipeline.run(context, TurnRequest(session_id="s1", message="查詢訂單 o1")))
    await asyncio.wait_for(both_started.wait(), .2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    trace = next(iter(pipeline.traces.records.values()))
    child = [e for e in trace.events if e.event_type == "node_child"]
    assert [(e.component, e.kind) for e in child] == [("order_api", "cancelled"), ("rag", "cancelled")]


@pytest.mark.asyncio
async def test_external_evidence_cancellation_retains_child_failure_race(
    pipeline, context, monkeypatch
):
    import asyncio
    from agent_flow.contracts import EvidencePlan, EvidenceToolCall
    monkeypatch.setattr(
        "agent_flow.pipeline.turn.plan_evidence",
        lambda classification: EvidencePlan(
            rag_queries=("shipping policy",),
            tool_calls=(EvidenceToolCall(operation="order.lookup", arguments={"order_id": "current"}, freshness_seconds=60),),
        ),
    )
    both_started = asyncio.Event()
    count = 0
    async def started():
        nonlocal count
        count += 1
        if count == 2: both_started.set()
    class Rag:
        async def search(self, context, request):
            await started(); await asyncio.Event().wait()
    class Tool:
        async def call(self, context, request):
            await started()
            try: await asyncio.Event().wait()
            except asyncio.CancelledError: raise OSError("failure raced cancellation")
    pipeline.rag, pipeline.tools = Rag(), Tool()
    task = asyncio.create_task(pipeline.run(context, TurnRequest(session_id="s1", message="查詢訂單 o1")))
    await asyncio.wait_for(both_started.wait(), .2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    trace = next(iter(pipeline.traces.records.values()))
    child = [e for e in trace.events if e.event_type == "node_child"]
    assert [(e.component, e.kind) for e in child] == [("order_api", "failed"), ("rag", "cancelled")]
