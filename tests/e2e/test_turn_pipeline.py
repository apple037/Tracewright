import pytest

from agent_flow.contracts import TurnRequest


@pytest.mark.asyncio
async def test_pipeline_returns_reduced_assurance_reply(pipeline, context, fake_models):
    result = await pipeline.run(context, TurnRequest(session_id="s1", message="查詢訂單 o1"))
    assert result.reply
    assert result.assurance.mode == "reduced_assurance"
    assert result.handoff_status is None
    assert fake_models.calls == ["dialogue_classifier", "strategy_advisor", "response_generator", "response_judge"]
    trace = await pipeline.traces.get_trace(result.trace_id, tenant_id="t1")
    assert trace.spans[0].node == "context_loader"
    strategy = next(e for e in trace.events if e.node == "strategy_selector" and e.kind == "completed")
    generation = next(e for e in trace.events if e.node == "response_generator" and e.kind == "completed")
    assert strategy.metadata["prompt_ref"] == pipeline.artifacts.strategy_prompt.ref.model_dump(mode="json")
    assert strategy.metadata["persona_ref"] is None
    assert generation.metadata["prompt_ref"] == pipeline.artifacts.response_prompt.ref.model_dump(mode="json")
    assert not any("system_rules" in str(e.payload) or "persona_directives" in str(e.payload) for e in trace.events)


@pytest.mark.asyncio
async def test_retry_rebinds_immutable_snapshot_for_retry_of_retry(
    pipeline, context, fake_models
):
    request = TurnRequest(session_id="s1", message="查詢訂單 o1")
    first = await pipeline.run(context, request)

    def replenish():
        fake_models.responses["dialogue_classifier"].append({
            "intent": "order_status", "conversation_mode": "transactional_read",
            "urgency": "normal", "language": "zh-TW",
            "emotion": {"category": "neutral", "dialogue_stage": "not_applicable",
                        "override": "none", "response_mode": "business_first",
                        "confidence": 1, "evidence_spans": [], "reason_codes": ["NO_EMOTIONAL_CONTENT"]},
        })
        fake_models.responses["strategy_advisor"].append({
            "strategy_version": "bootstrap-v1", "response_mode": "business_first",
            "answer_order": ["verified_fact"], "reason_codes": ["TRANSACTIONAL_READ"],
        })
        fake_models.responses["response_generator"].append({
            "text": "訂單仍在運送中。", "citations": ["tool-result-1"],
            "evidence_ids": ["tool-result-1"],
        })
        fake_models.responses["response_judge"].append({
            "passed": True, "failed_criteria": [], "confidence": 1,
            "reason_codes": ["GROUNDED"],
        })

    replenish()
    second = await pipeline.run(context, request, retry_of=first.trace_id)
    replenish()
    third = await pipeline.run(context, request, retry_of=second.trace_id)
    assert third.reply
    assert pipeline.conversations.snapshots[first.trace_id] is pipeline.conversations.snapshots[second.trace_id]
    assert pipeline.conversations.snapshots[second.trace_id] is pipeline.conversations.snapshots[third.trace_id]


@pytest.mark.asyncio
async def test_strategy_trace_uses_effective_not_candidate_persona(pipeline, context):
    from agent_flow.artifacts import RuntimeArtifacts
    from agent_flow.contracts import ConversationMode
    overly_broad = pipeline.artifacts.personas[0].model_copy(
        update={"applies_to": (*pipeline.artifacts.personas[0].applies_to, ConversationMode.TRANSACTIONAL_READ)}
    )
    pipeline.artifacts = RuntimeArtifacts(
        strategy_prompt=pipeline.artifacts.strategy_prompt,
        response_prompt=pipeline.artifacts.response_prompt,
        personas=(overly_broad,),
    )
    result = await pipeline.run(context, TurnRequest(session_id="s1", message="查詢訂單 o1"))
    trace = await pipeline.traces.get_trace(result.trace_id, tenant_id="t1")
    strategy = next(e for e in trace.events if e.node == "strategy_selector" and e.kind == "completed")
    assert strategy.metadata["persona_ref"] is None


@pytest.mark.asyncio
async def test_retry_rebinds_root_artifact_refs_and_rejects_unresolved_version(
    pipeline, context
):
    from agent_flow.artifacts import RuntimeArtifacts
    request = TurnRequest(session_id="s1", message="查詢訂單 o1")
    root = await pipeline.run(context, request)
    root_refs = next(
        e.metadata for e in pipeline.traces.records[root.trace_id].events
        if e.node == "context_loader" and e.kind == "completed"
    )
    changed = pipeline.artifacts.response_prompt.model_copy(update={"checksum": "b" * 64})
    pipeline.artifacts = RuntimeArtifacts(
        strategy_prompt=pipeline.artifacts.strategy_prompt,
        response_prompt=changed,
        personas=pipeline.artifacts.personas,
    )
    retry = await pipeline.run(context, request, retry_of=root.trace_id)
    retry_trace = pipeline.traces.records[retry.trace_id]
    assert retry.handoff.reason_code == "ARTIFACT_VERSION_UNRESOLVED"
    started = next(e for e in retry_trace.events if e.node == "context_loader" and e.kind == "started")
    assert started.metadata == root_refs
    failed = next(e for e in retry_trace.events if e.node == "context_loader" and e.kind == "failed")
    assert failed.error_code == "ARTIFACT_VERSION_UNRESOLVED"
    # The retry lineage never substitutes the newly loaded refs.
    assert root_refs["response_prompt_ref"]["checksum"] != changed.ref.checksum


@pytest.mark.asyncio
async def test_success_postcommit_ack_loss_replays_identical_finalization(
    pipeline, context
):
    original = pipeline.traces.finish_trace
    first = True
    async def committed_then_lost(*args, **kwargs):
        nonlocal first
        await original(*args, **kwargs)
        if first:
            first = False
            raise OSError("ack lost after success commit")
    pipeline.traces.finish_trace = committed_then_lost
    result = await pipeline.run(context, TurnRequest(session_id="s1", message="查詢訂單 o1"))
    trace = pipeline.traces.records[result.trace_id]
    assert result.reply
    assert trace.status == "succeeded"
    assert len(pipeline.conversations.persisted) == 1


@pytest.mark.asyncio
async def test_postcommit_conversation_ack_loss_retries_without_duplicate(
    pipeline, context
):
    original = pipeline.conversations.append_turn
    calls = 0
    async def committed_then_lost(**turn):
        nonlocal calls
        calls += 1
        await original(**turn)
        if calls == 1:
            raise OSError("conversation acknowledgement lost")
    pipeline.conversations.append_turn = committed_then_lost
    result = await pipeline.run(context, TurnRequest(session_id="s1", message="查詢訂單 o1"))
    assert result.reply
    assert calls == 2
    assert len(pipeline.conversations.persisted) == 1
