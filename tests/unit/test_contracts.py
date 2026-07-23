from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent_flow.contracts import (
    ArtifactRef,
    ConversationMode,
    EmotionAssessment,
    InboundMessage,
    StrategyDecision,
    StrategyProposal,
    SubmissionResult,
    TurnRequest,
)
from agent_flow.errors import AgentError


def test_strategy_proposal_rejects_controller_owned_persona_reference():
    with pytest.raises(ValidationError):
        StrategyProposal.model_validate(
            {
                "strategy_version": "bootstrap-v1",
                "response_mode": "business_first",
                "answer_order": ["verified_fact"],
                "reason_codes": ["TRANSACTIONAL_READ"],
                "persona_ref": {
                    "artifact_id": "familiar_companion.zh-TW",
                    "version": "1.0.0",
                    "checksum": "a" * 64,
                },
            }
        )


def test_strategy_decision_accepts_controller_owned_persona_reference():
    reference = ArtifactRef(
        artifact_id="familiar_companion.zh-TW",
        version="1.0.0",
        checksum="a" * 64,
    )

    decision = StrategyDecision(
        strategy_version="bootstrap-v1",
        response_mode="natural_follow",
        answer_order=("acknowledgment",),
        reason_codes=("EMOTIONAL_SUPPORT",),
        persona_ref=reference,
    )

    assert decision.persona_ref == reference


def test_model_facing_strategy_collections_remain_json_lists():
    proposal = StrategyProposal(
        strategy_version="bootstrap-v1",
        response_mode="business_first",
        answer_order=["verified_fact", "brief_acknowledgment"],
        reason_codes=["TRANSACTIONAL_READ"],
    )

    assert proposal.answer_order == ["verified_fact", "brief_acknowledgment"]
    assert proposal.reason_codes == ["TRANSACTIONAL_READ"]


def test_artifact_reference_is_immutable_and_validates_checksum():
    reference = ArtifactRef(
        artifact_id="strategy_selector",
        version="1.0.0",
        checksum="a" * 64,
    )

    with pytest.raises(ValidationError):
        reference.version = "2.0.0"
    with pytest.raises(ValidationError):
        ArtifactRef(
            artifact_id="strategy_selector",
            version="1.0.0",
            checksum="not-a-checksum",
        )


def test_emotion_assessment_rejects_unknown_category():
    with pytest.raises(ValidationError):
        EmotionAssessment(
            category="invented_emotion",
            dialogue_stage="surface",
            override="none",
            response_mode="business_first",
            confidence=0.5,
            evidence_spans=(),
            reason_codes=(),
        )


def test_emotion_assessment_rejects_confidence_above_one():
    with pytest.raises(ValidationError):
        EmotionAssessment(
            category="neutral",
            dialogue_stage="surface",
            override="none",
            response_mode="business_first",
            confidence=1.2,
            evidence_spans=(),
            reason_codes=(),
        )


def test_turn_request_rejects_unknown_fields_and_empty_message():
    with pytest.raises(ValidationError):
        TurnRequest(session_id="s1", message="")
    with pytest.raises(ValidationError):
        TurnRequest.model_validate(
            {"session_id": "s1", "message": "hello", "customer_id": "c1"}
        )


def test_conversation_mode_is_finite():
    assert ConversationMode("casual") is ConversationMode.CASUAL
    with pytest.raises(ValueError):
        ConversationMode("free-form-mode")


def test_agent_error_location_is_immutable_after_creation():
    error = AgentError.validation(
        "EVIDENCE_INSUFFICIENT",
        failure_stage="evidence_validator",
        component="pipeline",
        operation="validate",
    )

    with pytest.raises(AttributeError):
        error.failure_stage = "response_validator"


def test_inbound_message_forbids_identity_override_and_unknown_metadata():
    message = InboundMessage(
        channel="console",
        external_message_id="console-01",
        session_id="session-01",
        text="請幫我查詢訂單",
        idempotency_key="console-01",
        metadata={"source": "trace-console"},
    )

    assert message.to_turn_request() == TurnRequest(
        session_id="session-01", message="請幫我查詢訂單"
    )
    assert not hasattr(message, "customer_id")
    with pytest.raises(ValidationError):
        InboundMessage.model_validate(
            {
                **message.model_dump(),
                "customer_id": "customer-01",
            }
        )
    with pytest.raises(ValidationError):
        InboundMessage.model_validate(
            {
                **message.model_dump(),
                "metadata": {"untrusted": "value"},
            }
        )


def test_inbound_message_accepts_future_bounded_channel_names():
    message = InboundMessage(
        channel="web_chat-v2",
        external_message_id="web-01",
        session_id="session-01",
        text="hello",
        idempotency_key="web-01",
        metadata={"locale": "en-US"},
    )

    assert message.channel == "web_chat-v2"
    with pytest.raises(ValidationError):
        InboundMessage.model_validate(
            {
                **message.model_dump(),
                "channel": "Uppercase",
            }
        )


def test_submission_result_contains_only_safe_terminal_fields():
    result = SubmissionResult.model_validate(
        {
            "submission_id": str(uuid4()),
            "trace_id": str(uuid4()),
            "status": "completed",
            "text": "訂單已送達",
            "citations": [],
            "handoff": None,
        }
    )

    assert "reasoning" not in result.model_dump()
    with pytest.raises(ValidationError):
        SubmissionResult.model_validate(
            {
                **result.model_dump(),
                "reasoning": "private chain of thought",
            }
        )
