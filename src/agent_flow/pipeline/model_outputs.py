from typing import Literal

from pydantic import Field, TypeAdapter, model_validator

from agent_flow.contracts import (
    DialogueClassification,
    EmotionAssessment,
    JudgeVerdict,
    StrategyProposal,
)


EmotionReasonCode = Literal[
    "EXPLICIT_EXHAUSTION",
    "EXPLICIT_SELF_DOUBT",
    "EXPLICIT_INSECURITY",
    "EXPLICIT_GRIEF_LOSS",
    "EXPLICIT_FEAR_AVOIDANCE",
    "EXPLICIT_POSITIVE_SHIFT",
    "NO_EMOTIONAL_CONTENT",
    "DENIAL_OVERRIDE",
    "BOUNDARY_OVERRIDE",
    "POSITIVE_CLOSE_OVERRIDE",
    "HUMOR_OR_CHALLENGE_OVERRIDE",
    "LIGHT_TOPIC_OVERRIDE",
    "LOW_CONFIDENCE",
    "UNKNOWN_EMOTION",
]
StrategyReasonCode = Literal[
    "TRANSACTIONAL_READ",
    "INFORMATIONAL",
    "COMPLAINT",
    "EMOTIONAL_SUPPORT",
    "CASUAL",
    "BOUNDARY",
    "UNKNOWN_MODE",
    "VERIFIED_EVIDENCE_AVAILABLE",
    "NO_EVIDENCE_REQUIRED",
    "HANDOFF_REQUIRED",
    "BUSINESS_FIRST",
    "PERSONA_APPLIED",
]
JudgeCriterion = Literal[
    "UNSUPPORTED_EVIDENCE_REFERENCE",
    "UNSUPPORTED_DELIVERY_PROMISE",
    "UNSUPPORTED_ACTION_COMMITMENT",
    "UNSUPPORTED_PRICE",
    "UNSUPPORTED_DATE",
    "MISSING_REQUIRED_FACT",
    "CITATION_MISMATCH",
    "PERSONA_POLICY_OVERRIDE",
    "RISK_POLICY_VIOLATION",
    "LANGUAGE_MISMATCH",
    "UNCLEAR_RESPONSE",
]
JudgeReasonCode = Literal[
    "GROUNDED",
    "UNSUPPORTED_CLAIM",
    "CITATIONS_VERIFIED",
    "EVIDENCE_ID_MISMATCH",
    "BUSINESS_POLICY_PRESERVED",
    "RISK_POLICY_PRESERVED",
    "REPAIR_REQUIRED",
    "JUDGE_UNCERTAIN",
]


class BoundedEmotionAssessment(EmotionAssessment):
    reason_codes: tuple[EmotionReasonCode, ...] = Field(max_length=20)


class DialogueClassificationResult(DialogueClassification):
    emotion: BoundedEmotionAssessment


class StrategyProposalResult(StrategyProposal):
    reason_codes: list[StrategyReasonCode] = Field(max_length=20)


class JudgeVerdictResult(JudgeVerdict):
    failed_criteria: tuple[JudgeCriterion, ...] = Field(max_length=20)
    reason_codes: tuple[JudgeReasonCode, ...] = Field(max_length=20)

    @model_validator(mode="after")
    def validate_consistency(self):
        if self.passed and self.failed_criteria:
            raise ValueError("passed verdict cannot contain failed criteria")
        if not self.passed and not self.failed_criteria:
            raise ValueError("failed verdict requires failed criteria")
        return self


_FAILED_CRITERIA_ADAPTER = TypeAdapter(tuple[JudgeCriterion, ...])


def validate_failed_criteria(values: tuple[str, ...]) -> tuple[JudgeCriterion, ...]:
    return _FAILED_CRITERIA_ADAPTER.validate_python(values)
