import asyncio
import hashlib
import json
from datetime import timedelta

import pytest
from pydantic import ValidationError

from agent_flow.contracts import (
    CollectedEvidence,
    ConversationMode,
    DialogueClassification,
    EvidencePlan,
    EvidenceToolCall,
    JudgeVerdict,
    ResponseDraft,
    RiskDecision,
    StrategyProposal,
    ValidatedEvidence,
)
from agent_flow.errors import AgentError
from agent_flow.pipeline.classify import classify_dialogue
from agent_flow.pipeline.evidence import collect_evidence, plan_evidence, validate_evidence
from agent_flow.pipeline.respond import generate_response, repair_response, select_strategy
from agent_flow.pipeline.risk import risk_precheck
from agent_flow.pipeline.validate import validate_response
from tests.fakes import FakeModelGateway


@pytest.mark.asyncio
async def test_classifier_returns_intent_and_emotion_in_one_call(fake_models):
    result = await classify_dialogue(fake_models, ["我很累，訂單也還沒到"])
    assert result.intent == "order_status"
    assert result.emotion.category == "stress_exhaustion"
    assert fake_models.calls == ["dialogue_classifier"]


def test_risk_precheck_handoffs_before_any_evidence_call(classification):
    class NeverEvidence:
        called = False

    clients = (NeverEvidence(), NeverEvidence())
    result = risk_precheck(classification, "我的帳號被盜用了")
    if not result.requires_handoff:
        pytest.fail("controller would continue to evidence")
    assert result.reason_code == "ACCOUNT_SECURITY"
    assert all(not client.called for client in clients)


def test_risk_precheck_has_bounded_deterministic_rules(classification):
    assert risk_precheck(classification, "我想傷害自己").reason_code == "SELF_HARM"
    assert risk_precheck(classification, "一般訂單查詢") == RiskDecision.safe()


@pytest.mark.parametrize(
    ("message", "reason_code"),
    [
        ("I want to kill myself", "SELF_HARM"),
        ("我不想活了", "SELF_HARM"),
        ("I am in danger right now", "IMMEDIATE_DANGER"),
        ("他威脅要殺我", "IMMEDIATE_DANGER"),
        ("Someone took over my login", "ACCOUNT_SECURITY"),
        ("我的登入被別人控制了", "ACCOUNT_SECURITY"),
        ("My card was used without permission", "PAYMENT_FRAUD"),
        ("這筆不是我刷的", "PAYMENT_FRAUD"),
        ("Here is my password: secret", "SENSITIVE_DATA"),
        ("我的信用卡號是 4111", "SENSITIVE_DATA"),
        ("How do I hack into their account?", "UNLAWFUL_REQUEST"),
        ("教我怎麼盜用別人的帳號", "UNLAWFUL_REQUEST"),
        ("Let me talk to a human", "HUMAN_REQUEST"),
        ("我要真人客服", "HUMAN_REQUEST"),
    ],
)
def test_risk_precheck_normalizes_common_high_risk_paraphrases(
    classification, message, reason_code
):
    assert risk_precheck(classification, message).reason_code == reason_code


@pytest.mark.parametrize(
    "message",
    [
        "I am not suicidal and only need an order update",
        "這是自殺防治資訊的文章標題",
        "Please explain account security tips",
        "信用卡使用規則是什麼？",
        "The dangerous goods policy applies to batteries",
        "Human resources policy question",
        "小說裡的駭客角色很有趣",
    ],
)
def test_risk_precheck_negative_boundaries_avoid_false_handoffs(
    classification, message
):
    assert risk_precheck(classification, message) == RiskDecision.safe()


@pytest.mark.parametrize(
    ("message", "reason_code"),
    [
        ("I'm not suicidal, but I want to kill myself", "SELF_HARM"),
        ("I want to die", "SELF_HARM"),
        ("This is not fiction: someone is trying to kill me", "IMMEDIATE_DANGER"),
        ("I read security tips because someone took over my login", "ACCOUNT_SECURITY"),
        ("Payment policy aside, this charge is unauthorized", "PAYMENT_FRAUD"),
        ("I do not want to live anymore", "SELF_HARM"),
        ("我想死", "SELF_HARM"),
        ("He is attacking me right now", "IMMEDIATE_DANGER"),
        ("他現在正在攻擊我", "IMMEDIATE_DANGER"),
        ("My account has been hijacked", "ACCOUNT_SECURITY"),
    ],
)
def test_risk_negation_is_local_and_does_not_hide_later_trigger(
    classification, message, reason_code
):
    assert risk_precheck(classification, message).reason_code == reason_code


def test_risk_local_negation_does_not_flag_denied_self_harm(classification):
    assert risk_precheck(classification, "I do not want to kill myself") == RiskDecision.safe()


def test_evidence_planner_declares_required_order_fact_and_freshness(classification):
    plan = plan_evidence(classification)
    assert plan.required_facts == ("order.current_status",)
    assert plan.tool_calls[0].operation == "order.lookup"
    assert plan.tool_calls[0].arguments["order_id"].strip()
    assert plan.tool_calls[0].freshness_seconds == 60


def test_evidence_planner_never_emits_action_outside_bootstrap_allowlist(classification):
    refund = classification.model_copy(update={"intent": "refund_status"})
    plan = plan_evidence(refund)
    assert plan.required_facts == ("refund.current_status",)
    assert plan.tool_calls == ()


@pytest.mark.asyncio
async def test_collect_evidence_runs_independent_sources_concurrently(
    authorized_context, fresh_collected_evidence
):
    started = 0
    both_started = asyncio.Event()

    class Rag:
        async def search(self, context, request):
            nonlocal started
            started += 1
            if started == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=0.2)
            from agent_flow.contracts import RagSearchResult
            return RagSearchResult(items=())

    class Tools:
        async def call(self, context, request):
            nonlocal started
            started += 1
            if started == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=0.2)
            from agent_flow.contracts import ToolCallResult
            return ToolCallResult(tool=request.tool, evidence=fresh_collected_evidence.items[0])

    plan = EvidencePlan(
        rag_queries=("shipping policy",),
        tool_calls=(EvidenceToolCall(
            operation="order.lookup", arguments={"order_id": "order-1"},
            freshness_seconds=60,
        ),),
    )
    result = await collect_evidence(authorized_context, plan, Rag(), Tools())
    assert result.items == fresh_collected_evidence.items


@pytest.mark.asyncio
async def test_collect_evidence_does_not_swallow_required_source_failure(authorized_context):
    class Rag:
        async def search(self, context, request):
            from agent_flow.contracts import RagSearchResult
            return RagSearchResult(items=())

    class Tools:
        async def call(self, context, request):
            raise LookupError("required source unavailable")

    plan = EvidencePlan(
        required_facts=("order.current_status",),
        tool_calls=(EvidenceToolCall(
            operation="order.lookup", arguments={"order_id": "order-1"},
            freshness_seconds=60,
        ),),
    )
    with pytest.raises(ExceptionGroup) as caught:
        await collect_evidence(authorized_context, plan, Rag(), Tools())
    failures = [error for error in caught.value.exceptions if isinstance(error, AgentError)]
    assert len(failures) == 1
    assert failures[0].error_code == "EVIDENCE_SOURCE_FAILED"
    assert failures[0].failure_stage == "evidence_collector"
    assert failures[0].component == "tool"
    assert failures[0].operation == "order.lookup"


def test_controller_action_policy_is_explicit_and_models_have_no_actions():
    from agent_flow.pipeline.policy import (
        EVIDENCE_COLLECTOR_ALLOWED_ACTIONS,
        MODEL_ALLOWED_ACTIONS,
    )

    assert MODEL_ALLOWED_ACTIONS == {
        "dialogue_classifier": frozenset(),
        "strategy_advisor": frozenset(),
        "response_generator": frozenset(),
        "response_judge": frozenset(),
        "response_judge_zh_verifier": frozenset(),
        "promotion_judge_primary": frozenset(),
        "promotion_judge_secondary": frozenset(),
    }
    assert EVIDENCE_COLLECTOR_ALLOWED_ACTIONS == frozenset({"order.lookup"})


@pytest.mark.parametrize(
    ("tool_calls", "error_code"),
    [
        ((EvidenceToolCall(operation="refund.issue", arguments={}, freshness_seconds=0),),
         "ACTION_NOT_ALLOWED"),
        ((EvidenceToolCall(operation="order.lookup", arguments={}, freshness_seconds=60),),
         "ACTION_ARGUMENT_INVALID"),
        ((
            EvidenceToolCall(operation="order.lookup", arguments={"order_id": "o1"}, freshness_seconds=60),
            EvidenceToolCall(operation="order.lookup", arguments={"order_id": "o1"}, freshness_seconds=60),
         ), "DUPLICATE_ACTION"),
    ],
)
@pytest.mark.asyncio
async def test_invalid_or_duplicate_action_plan_makes_zero_source_calls(
    authorized_context, tool_calls, error_code
):
    class NeverRag:
        calls = 0

        async def search(self, context, request):
            self.calls += 1
            raise AssertionError("RAG must not run")

    class NeverTools:
        calls = 0

        async def call(self, context, request):
            self.calls += 1
            raise AssertionError("tool must not run")

    rag, tools = NeverRag(), NeverTools()
    plan = EvidencePlan(rag_queries=("must not run",), tool_calls=tool_calls)
    with pytest.raises(AgentError) as caught:
        await collect_evidence(authorized_context, plan, rag, tools)
    assert caught.value.error_code == error_code
    assert caught.value.failure_stage == "evidence_collector"
    assert caught.value.retryable is False
    assert (rag.calls, tools.calls) == (0, 0)


@pytest.mark.parametrize(
    "contract,valid",
    [
        (DialogueClassification, {
            "intent": "order_status", "conversation_mode": "transactional_read",
            "urgency": "normal", "language": "zh-TW",
            "emotion": {"category": "neutral", "dialogue_stage": "not_applicable",
                        "override": "none", "response_mode": "business_first",
                        "confidence": 1, "evidence_spans": [], "reason_codes": []},
        }),
        (StrategyProposal, {"strategy_version": "v1", "response_mode": "business_first",
                            "answer_order": [], "reason_codes": []}),
        (ResponseDraft, {"text": "ok"}),
        (JudgeVerdict, {"passed": True, "failed_criteria": [], "confidence": 1,
                        "reason_codes": []}),
    ],
)
def test_all_model_result_contracts_forbid_action_fields(contract, valid):
    with pytest.raises(ValidationError):
        contract.model_validate({**valid, "tool_calls": [{"operation": "order.lookup"}]})


@pytest.mark.asyncio
async def test_classifier_rejects_unbounded_reason_code():
    models = FakeModelGateway({"dialogue_classifier": [{
        "intent": "order_status", "conversation_mode": "transactional_read",
        "urgency": "normal", "language": "zh-TW",
        "emotion": {"category": "neutral", "dialogue_stage": "not_applicable",
                    "override": "none", "response_mode": "business_first",
                    "confidence": 1, "evidence_spans": [],
                    "reason_codes": ["MODEL_INVENTED_CODE"]},
    }]})
    with pytest.raises(ValidationError):
        await classify_dialogue(models, ["訂單呢？"])


@pytest.mark.asyncio
async def test_strategy_rejects_unbounded_reason_code(
    classification, validated_evidence, strategy_prompt
):
    models = FakeModelGateway({"strategy_advisor": [{
        "strategy_version": "v1", "response_mode": "business_first",
        "answer_order": [], "reason_codes": ["MODEL_INVENTED_CODE"],
    }]})
    with pytest.raises(ValidationError):
        await select_strategy(
            models, classification, RiskDecision.safe(), validated_evidence,
            strategy_prompt, None,
        )


@pytest.mark.asyncio
async def test_judge_rejects_unbounded_feedback_code(verified_draft, validated_evidence):
    models = FakeModelGateway({"response_judge": [{
        "passed": False, "failed_criteria": ["MODEL_INVENTED_CRITERION"],
        "confidence": 1, "reason_codes": ["MODEL_INVENTED_CODE"],
    }]})
    with pytest.raises(ValidationError):
        await validate_response(models, verified_draft, validated_evidence, "bootstrap")


@pytest.mark.asyncio
async def test_repair_rejects_invented_feedback_before_model_call(
    verified_draft, repairable_validation, transactional_strategy,
    validated_evidence, response_prompt
):
    invented = repairable_validation.model_copy(
        update={"failed_criteria": ("MODEL_INVENTED_CRITERION",)}
    )
    models = FakeModelGateway({"response_generator": []})
    with pytest.raises(ValidationError):
        await repair_response(
            models, verified_draft, invented, transactional_strategy,
            validated_evidence, response_prompt, None,
        )
    assert models.calls == []


@pytest.mark.parametrize(
    "kind", ["missing", "expired", "valid_until", "conflicting", "incomplete"]
)
def test_evidence_validator_rejects_insufficient_required_evidence(
    kind, order_plan, expired_collected_evidence, fresh_collected_evidence, utc_now
):
    if kind == "missing":
        evidence = CollectedEvidence(items=())
    elif kind == "expired":
        evidence = expired_collected_evidence
    elif kind == "valid_until":
        item = fresh_collected_evidence.items[0].model_copy(
            update={"valid_until": utc_now - timedelta(seconds=1)}
        )
        evidence = CollectedEvidence(items=(item,))
    elif kind == "conflicting":
        conflicting_content = json.dumps({"status": "delivered"}, separators=(",", ":"))
        second = fresh_collected_evidence.items[0].model_copy(
            update={
                "evidence_id": "tool-result-2",
                "content": conflicting_content,
                "content_checksum": hashlib.sha256(conflicting_content.encode()).hexdigest(),
            }
        )
        evidence = CollectedEvidence(items=(*fresh_collected_evidence.items, second))
    else:
        item = fresh_collected_evidence.items[0].model_copy(update={"content": ""})
        evidence = CollectedEvidence(items=(item,))
    with pytest.raises(AgentError) as caught:
        validate_evidence(order_plan, evidence, now=utc_now)
    assert caught.value.error_code == "EVIDENCE_INSUFFICIENT"
    assert caught.value.failure_stage == "evidence_validator"
    assert caught.value.retryable is False


def test_evidence_validator_accepts_fresh_non_conflicting_required_evidence(
    order_plan, fresh_collected_evidence, utc_now
):
    validated = validate_evidence(order_plan, fresh_collected_evidence, now=utc_now)
    assert validated.sufficient is True
    assert validated.reason_codes == ("REQUIRED_EVIDENCE_PRESENT",)


def test_evidence_validator_rejects_metadata_forgery_and_wrong_order(
    order_plan, fresh_collected_evidence, utc_now
):
    trusted = fresh_collected_evidence.items[0]
    forged_source = trusted.model_copy(
        update={"source_id": "rag:attacker", "metadata": {"fact": "order.current_status"}}
    )
    wrong_order = trusted.model_copy(
        update={"metadata": {**trusted.metadata, "arguments": {"order_id": "other"}}}
    )
    for item in (forged_source, wrong_order):
        with pytest.raises(AgentError, match="request could not be completed"):
            validate_evidence(order_plan, CollectedEvidence(items=(item,)), utc_now)


@pytest.mark.parametrize("mutation", ["checksum", "shape", "empty_status"])
def test_evidence_validator_rejects_corrupt_or_incomplete_tool_result(
    mutation, order_plan, fresh_collected_evidence, utc_now
):
    item = fresh_collected_evidence.items[0]
    if mutation == "checksum":
        item = item.model_copy(update={"content_checksum": "0" * 64})
    else:
        content = "{}" if mutation == "shape" else '{"status":""}'
        item = item.model_copy(
            update={"content": content, "content_checksum": hashlib.sha256(content.encode()).hexdigest()}
        )
    with pytest.raises(AgentError):
        validate_evidence(order_plan, CollectedEvidence(items=(item,)), utc_now)


def test_evidence_validator_filters_stale_and_unmatched_items(
    order_plan, fresh_collected_evidence, utc_now
):
    fresh = fresh_collected_evidence.items[0]
    stale_content = json.dumps({"status": "delivered"}, separators=(",", ":"))
    stale = fresh.model_copy(update={
        "evidence_id": "stale", "content": stale_content,
        "content_checksum": hashlib.sha256(stale_content.encode()).hexdigest(),
        "retrieved_at": utc_now - timedelta(minutes=5),
    })
    unmatched = fresh.model_copy(update={
        "evidence_id": "other", "metadata": {**fresh.metadata, "arguments": {"order_id": "other"}}
    })
    validated = validate_evidence(
        order_plan, CollectedEvidence(items=(fresh, stale, unmatched)), utc_now
    )
    assert validated.items == (fresh,)


def test_evidence_validator_rejects_conflicting_fresh_status_values(
    order_plan, fresh_collected_evidence, utc_now
):
    first = fresh_collected_evidence.items[0]
    content = json.dumps({"status": "delivered"}, separators=(",", ":"))
    second = first.model_copy(update={
        "evidence_id": "second", "content": content,
        "content_checksum": hashlib.sha256(content.encode()).hexdigest(),
    })
    with pytest.raises(AgentError):
        validate_evidence(order_plan, CollectedEvidence(items=(first, second)), utc_now)


@pytest.mark.asyncio
async def test_business_strategy_cannot_attach_companion_persona(
    fake_models, classification, validated_evidence, strategy_prompt, companion_persona
):
    result = await select_strategy(
        fake_models, classification, RiskDecision.safe(), validated_evidence,
        strategy_prompt, companion_persona,
    )
    assert result.persona_ref is None
    assert fake_models.requests[0].resolved_persona_ref is None


@pytest.mark.asyncio
async def test_business_strategy_ignores_persona_with_forged_applicability(
    fake_models, classification, validated_evidence, strategy_prompt, companion_persona
):
    forged = companion_persona.model_copy(
        update={"applies_to": (ConversationMode.TRANSACTIONAL_READ,)}
    )
    result = await select_strategy(
        fake_models, classification, RiskDecision.safe(), validated_evidence,
        strategy_prompt, forged,
    )
    assert result.persona_ref is None
    assert fake_models.requests[0].resolved_persona_ref is None


@pytest.mark.asyncio
async def test_emotional_strategy_records_only_controller_persona_ref(
    fake_models, classification, validated_evidence, strategy_prompt, companion_persona
):
    emotional = classification.model_copy(
        update={"conversation_mode": ConversationMode.EMOTIONAL_SUPPORT}
    )
    result = await select_strategy(
        fake_models, emotional, RiskDecision.safe(), validated_evidence,
        strategy_prompt, companion_persona,
    )
    assert result.persona_ref == companion_persona.ref


@pytest.mark.asyncio
async def test_strategy_model_cannot_supply_persona_ref(
    classification, validated_evidence, strategy_prompt, companion_persona
):
    models = FakeModelGateway({"strategy_advisor": [{
        "strategy_version": "bootstrap-v1", "response_mode": "supportive",
        "answer_order": ["acknowledgment"], "reason_codes": ["EMOTIONAL_SUPPORT"],
        "persona_ref": companion_persona.ref.model_dump(mode="json"),
    }]})
    with pytest.raises(ValidationError):
        await select_strategy(
            models,
            classification.model_copy(update={"conversation_mode": ConversationMode.EMOTIONAL_SUPPORT}),
            RiskDecision.safe(), validated_evidence, strategy_prompt, companion_persona,
        )


@pytest.mark.asyncio
async def test_generation_suppresses_mismatched_or_forged_persona(
    fake_models, transactional_strategy, validated_evidence, response_prompt,
    companion_persona, verified_draft
):
    forged = transactional_strategy.model_copy(update={"persona_ref": companion_persona.ref})
    other = companion_persona.model_copy(update={"checksum": "0" * 64})
    result = await generate_response(
        fake_models, _snapshot(), forged, validated_evidence, response_prompt, other
    )
    assert result == verified_draft
    assert fake_models.requests[0].persona is None


@pytest.mark.asyncio
async def test_bootstrap_validator_calls_exactly_one_judge(
    fake_models, verified_draft, validated_evidence
):
    verdict = await validate_response(fake_models, verified_draft, validated_evidence, "bootstrap")
    assert verdict.assurance == "reduced_assurance"
    assert fake_models.calls == ["response_judge"]


@pytest.mark.asyncio
async def test_dual_validator_uses_independent_judges(verified_draft, validated_evidence):
    verdict = {"passed": True, "failed_criteria": [], "confidence": .9,
               "reason_codes": ["GROUNDED"]}
    models = FakeModelGateway({"response_judge": [verdict],
                               "response_judge_zh_verifier": [verdict]})
    result = await validate_response(models, verified_draft, validated_evidence, "dual_judge")
    assert result.assurance == "dual_judge"
    assert models.calls == ["response_judge", "response_judge_zh_verifier"]


@pytest.mark.asyncio
async def test_validator_rejects_invalid_assurance_mode(fake_models, verified_draft, validated_evidence):
    with pytest.raises(ValueError, match="assurance_mode"):
        await validate_response(fake_models, verified_draft, validated_evidence, "single")
    assert fake_models.calls == []


@pytest.mark.asyncio
async def test_deterministic_hard_failure_never_calls_judge(
    fake_models, verified_draft, validated_evidence
):
    unsupported = verified_draft.model_copy(update={"evidence_ids": ("forged",)})
    verdict = await validate_response(fake_models, unsupported, validated_evidence, "bootstrap")
    assert verdict.passed is False
    assert verdict.failed_criteria == ("UNSUPPORTED_EVIDENCE_REFERENCE",)
    assert verdict.repairable is False
    assert fake_models.calls == []


@pytest.mark.parametrize(
    ("update", "criterion"),
    [
        ({"evidence_ids": (), "citations": ()}, "UNSUPPORTED_EVIDENCE_REFERENCE"),
        ({"evidence_ids": ("tool-result-1",), "citations": ()}, "CITATION_MISMATCH"),
        ({"evidence_ids": ("tool-result-1",), "citations": ("forged:citation",)}, "CITATION_MISMATCH"),
        ({"text": "Delivered tomorrow for $1."}, "UNSUPPORTED_DELIVERY_PROMISE"),
    ],
)
@pytest.mark.asyncio
async def test_deterministic_grounding_failures_bypass_judge(
    fake_models, verified_draft, validated_evidence, update, criterion
):
    draft = verified_draft.model_copy(update=update)
    result = await validate_response(fake_models, draft, validated_evidence, "bootstrap")
    assert criterion in result.failed_criteria
    assert fake_models.calls == []


@pytest.mark.asyncio
async def test_no_evidence_emotional_response_can_reach_judge(fake_models, verified_draft):
    no_evidence = ValidatedEvidence(
        items=(), sufficient=True, reason_codes=("NO_EVIDENCE_REQUIRED",)
    )
    draft = verified_draft.model_copy(update={"text": "我在這裡。", "citations": (), "evidence_ids": ()})
    result = await validate_response(fake_models, draft, no_evidence, "bootstrap")
    assert result.passed is True
    assert fake_models.calls == ["response_judge"]


@pytest.mark.parametrize(
    "payload",
    [
        {"passed": True, "failed_criteria": ["CITATION_MISMATCH"],
         "confidence": 1, "reason_codes": ["GROUNDED"]},
        {"passed": False, "failed_criteria": [],
         "confidence": 1, "reason_codes": ["REPAIR_REQUIRED"]},
    ],
)
def test_judge_verdict_rejects_contradictory_pass_and_criteria(payload):
    from agent_flow.pipeline.model_outputs import JudgeVerdictResult
    with pytest.raises(ValidationError):
        JudgeVerdictResult.model_validate(payload)


@pytest.mark.parametrize(
    "role",
    [
        "dialogue_classifier", "strategy_advisor", "response_generator",
        "response_judge", "response_judge_zh_verifier",
        "promotion_judge_primary", "promotion_judge_secondary",
    ],
)
@pytest.mark.asyncio
async def test_structured_model_policy_rejects_attempted_actions_before_gateway(role):
    from agent_flow.pipeline.policy import invoke_structured_model
    models = FakeModelGateway({role: []})
    with pytest.raises(AgentError) as caught:
        await invoke_structured_model(
            models, role, {}, ResponseDraft,
            attempted_actions=frozenset({"order.lookup"}),
        )
    assert caught.value.error_code == "MODEL_ACTION_NOT_ALLOWED"
    assert models.calls == []


@pytest.mark.asyncio
async def test_repair_uses_generator_with_only_failed_criteria(
    verified_draft, repairable_validation, transactional_strategy,
    validated_evidence, response_prompt
):
    models = FakeModelGateway({"response_generator": [{
        "text": "訂單目前仍在運送中。", "citations": [],
        "evidence_ids": ["tool-result-1"],
    }]})
    repaired = await repair_response(
        models, verified_draft, repairable_validation, transactional_strategy,
        validated_evidence, response_prompt, None,
    )
    assert repaired.text != verified_draft.text
    assert models.calls == ["response_generator"]
    request = models.requests[0]
    assert request.failed_criteria == ("UNSUPPORTED_DELIVERY_PROMISE",)
    assert request.persona is None
    assert not hasattr(request, "validation_reason_codes")


def _snapshot():
    from datetime import datetime, timezone
    from agent_flow.contracts import ConversationSnapshot
    return ConversationSnapshot(
        session_id="s1", messages=("訂單呢？",), captured_at=datetime.now(timezone.utc)
    )
