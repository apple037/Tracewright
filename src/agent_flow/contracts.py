from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator


SEMVER_PATTERN = r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
SemanticVersion = Annotated[str, Field(pattern=SEMVER_PATTERN)]
Sha256Checksum = Annotated[str, Field(pattern=SHA256_PATTERN)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConversationMode(StrEnum):
    INFORMATIONAL = "informational"
    TRANSACTIONAL_READ = "transactional_read"
    COMPLAINT = "complaint"
    EMOTIONAL_SUPPORT = "emotional_support"
    CASUAL = "casual"
    BOUNDARY = "boundary"
    UNKNOWN = "unknown"


class EmotionCategory(StrEnum):
    SELF_DOUBT = "self_doubt"
    INSECURITY = "insecurity"
    GRIEF_LOSS = "grief_loss"
    STRESS_EXHAUSTION = "stress_exhaustion"
    FEAR_AVOIDANCE = "fear_avoidance"
    POSITIVE_SHIFT = "positive_shift"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"


class DialogueStage(StrEnum):
    SURFACE = "surface"
    MIDDLE = "middle"
    DEEP = "deep"
    POSITIVE_CLOSE = "positive_close"
    NOT_APPLICABLE = "not_applicable"


class EmotionOverride(StrEnum):
    DENIAL = "denial"
    BOUNDARY = "boundary"
    POSITIVE_CLOSE = "positive_close"
    HUMOR_OR_CHALLENGE = "humor_or_challenge"
    NO_EMOTIONAL_CONTENT = "no_emotional_content"
    EXPLICIT_POSITIVE = "explicit_positive"
    LIGHT_TOPIC = "light_topic"
    NONE = "none"


class ResponseMode(StrEnum):
    NATURAL_FOLLOW = "natural_follow"
    BRIEF_ACKNOWLEDGMENT = "brief_acknowledgment"
    OPEN_PROBE = "open_probe"
    DIRECT_LABEL = "direct_label"
    QUOTE_AND_LABEL = "quote_and_label"
    BUSINESS_FIRST = "business_first"
    SUPPORTIVE = "supportive"


class ArtifactRef(StrictModel):
    artifact_id: str = Field(min_length=1, max_length=128)
    version: SemanticVersion
    checksum: Sha256Checksum


class PersonaGuardrails(StrictModel):
    business_modes: tuple[ConversationMode, ...]
    never_override_business_request: bool
    never_suppress_verified_facts: bool
    never_change_risk_or_tool_route: bool
    global_two_sentence_limit: bool
    global_do_not_solve_objective: bool


class PersonaArtifact(StrictModel):
    schema_version: Literal[1]
    artifact_id: str = Field(min_length=1, max_length=128)
    version: SemanticVersion
    locale: str = Field(min_length=2, max_length=32)
    source_reference: str = Field(min_length=1)
    applies_to: tuple[ConversationMode, ...]
    expression_principles: tuple[str, ...]
    override_order: tuple[EmotionOverride, ...]
    guardrails: PersonaGuardrails
    checksum: Sha256Checksum | None = Field(default=None, exclude=True)

    @computed_field
    @property
    def ref(self) -> ArtifactRef:
        if self.checksum is None:
            raise ValueError("artifact checksum has not been computed")
        return ArtifactRef(
            artifact_id=self.artifact_id,
            version=self.version,
            checksum=self.checksum,
        )


class PromptArtifact(StrictModel):
    schema_version: Literal[1]
    artifact_id: str = Field(min_length=1, max_length=128)
    version: SemanticVersion
    node: str = Field(min_length=1, max_length=128)
    system_rules: tuple[str, ...]
    required_inputs: tuple[str, ...]
    output_contract: str = Field(min_length=1, max_length=128)
    checksum: Sha256Checksum | None = Field(default=None, exclude=True)

    @computed_field
    @property
    def ref(self) -> ArtifactRef:
        if self.checksum is None:
            raise ValueError("artifact checksum has not been computed")
        return ArtifactRef(
            artifact_id=self.artifact_id,
            version=self.version,
            checksum=self.checksum,
        )


class TraceIdentifiers(StrictModel):
    trace_id: UUID
    span_id: UUID | None = None
    parent_span_id: UUID | None = None


class EmotionAssessment(StrictModel):
    category: EmotionCategory
    dialogue_stage: DialogueStage
    override: EmotionOverride
    response_mode: ResponseMode
    confidence: float = Field(ge=0, le=1)
    evidence_spans: tuple[str, ...]
    reason_codes: tuple[str, ...]


class DialogueClassification(StrictModel):
    intent: str = Field(min_length=1, max_length=128)
    conversation_mode: ConversationMode
    urgency: Literal["low", "normal", "high", "critical"]
    language: str = Field(min_length=2, max_length=32)
    emotion: EmotionAssessment
    # The knowledge_catalog source_id this turn needs, or None when the message
    # is not covered by the corpus (greetings, chit-chat). Drives whether the
    # planner retrieves at all.
    knowledge_topic: str | None = Field(default=None, max_length=128)


class RiskDecision(StrictModel):
    requires_handoff: bool
    reason_code: str | None = None

    @classmethod
    def safe(cls) -> "RiskDecision":
        return cls(requires_handoff=False)


class StrategyProposal(StrictModel):
    strategy_version: str = Field(min_length=1, max_length=128)
    response_mode: ResponseMode
    answer_order: list[str] = Field(max_length=12)
    reason_codes: list[str] = Field(max_length=20)


class StrategyDecision(StrategyProposal):
    persona_ref: ArtifactRef | None = None


class ResponseDraft(StrictModel):
    text: str = Field(min_length=1)
    citations: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()


class EvidenceItem(StrictModel):
    evidence_id: str
    source_id: str
    version: str
    content: str
    content_checksum: Sha256Checksum
    retrieved_at: datetime
    effective_at: datetime | None = None
    valid_until: datetime | None = None
    score: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvidenceToolCall(StrictModel):
    operation: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    freshness_seconds: int = Field(ge=0)


class EvidencePlan(StrictModel):
    required_facts: tuple[str, ...] = ()
    rag_queries: tuple[str, ...] = ()
    tool_calls: tuple[EvidenceToolCall, ...] = ()


class CollectedEvidence(StrictModel):
    items: tuple[EvidenceItem, ...]


class ValidatedEvidence(StrictModel):
    items: tuple[EvidenceItem, ...]
    sufficient: bool
    reason_codes: tuple[str, ...]


class JudgeVerdict(StrictModel):
    passed: bool
    failed_criteria: tuple[str, ...]
    confidence: float = Field(ge=0, le=1)
    reason_codes: tuple[str, ...]


class ValidationResult(JudgeVerdict):
    assurance: Literal["reduced_assurance", "dual_judge"]
    repairable: bool = False


class HandoffEvent(StrictModel):
    required: bool
    reason_code: str
    safe_message: str


class AssuranceMetadata(StrictModel):
    mode: Literal["reduced_assurance", "dual_judge"]
    judges: tuple[str, ...]


class TurnRequest(StrictModel):
    session_id: str = Field(min_length=1, max_length=256)
    message: str = Field(min_length=1, max_length=20_000)
    case_id: str | None = None


SubmissionStatus = Literal["queued", "running", "completed", "failed"]


class InboundMessage(StrictModel):
    channel: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    external_message_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)
    text: str = Field(min_length=1, max_length=20_000)
    case_id: str | None = Field(default=None, max_length=256)
    idempotency_key: str = Field(min_length=1, max_length=256)
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def validate_metadata_keys(cls, value: dict[str, str]) -> dict[str, str]:
        unknown_keys = value.keys() - {"source", "locale"}
        if unknown_keys:
            raise ValueError(
                f"unsupported metadata keys: {', '.join(sorted(unknown_keys))}"
            )
        return value

    def to_turn_request(self) -> TurnRequest:
        return TurnRequest(
            session_id=self.session_id,
            message=self.text,
            case_id=self.case_id,
        )


class SubmissionReceipt(StrictModel):
    submission_id: UUID
    trace_id: UUID
    status: SubmissionStatus


class SubmissionResult(SubmissionReceipt):
    text: str | None = None
    citations: tuple[str, ...] = ()
    handoff: HandoffEvent | None = None
    error_code: str | None = None
    error_component: str | None = None


class CapturedTurnInput(StrictModel):
    request: TurnRequest
    captured_at: datetime
    expires_at: datetime


class TurnResult(StrictModel):
    trace_id: UUID
    text: str | None
    citations: tuple[str, ...] = ()
    handoff: HandoffEvent | None = None
    assurance: AssuranceMetadata

    @property
    def reply(self) -> str | None:
        """Compatibility name used by turn-controller clients."""
        return self.text

    @property
    def handoff_status(self) -> Literal["queued"] | None:
        return "queued" if self.handoff is not None and self.handoff.required else None


class ConversationSnapshot(StrictModel):
    session_id: str
    messages: tuple[str, ...]
    captured_at: datetime


class RagSearchRequest(StrictModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=5, ge=1, le=50)


class RagSearchResult(StrictModel):
    items: tuple[EvidenceItem, ...]


class ToolCallRequest(StrictModel):
    tool: str
    arguments: dict[str, Any]


class ToolCallResult(StrictModel):
    tool: str
    evidence: EvidenceItem
