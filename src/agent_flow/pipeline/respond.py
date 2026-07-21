from agent_flow.adapters.models import ModelGateway
from agent_flow.contracts import (
    ArtifactRef,
    ConversationMode,
    ConversationSnapshot,
    DialogueClassification,
    PersonaArtifact,
    PromptArtifact,
    ResponseDraft,
    RiskDecision,
    StrategyDecision,
    StrictModel,
    ValidatedEvidence,
    ValidationResult,
)
from agent_flow.pipeline.model_outputs import (
    StrategyProposalResult,
    validate_failed_criteria,
)
from agent_flow.pipeline.policy import invoke_structured_model


class StrategyRequest(StrictModel):
    dialogue_classification: DialogueClassification
    risk_decision: RiskDecision
    validated_evidence: ValidatedEvidence
    prompt_ref: ArtifactRef
    resolved_persona_ref: ArtifactRef | None = None
    persona_directives: tuple[str, ...] = ()


class GenerationRequest(StrictModel):
    conversation_snapshot: ConversationSnapshot
    strategy_decision: StrategyDecision
    validated_evidence: ValidatedEvidence
    prompt_ref: ArtifactRef
    persona: PersonaArtifact | None = None


class RepairRequest(StrictModel):
    draft: ResponseDraft
    failed_criteria: tuple[str, ...]
    evidence: ValidatedEvidence
    prompt_ref: ArtifactRef
    persona: PersonaArtifact | None = None


def _applicable_persona(
    classification: DialogueClassification, persona: PersonaArtifact | None
) -> PersonaArtifact | None:
    companion_modes = {
        ConversationMode.EMOTIONAL_SUPPORT,
        ConversationMode.CASUAL,
    }
    if (
        persona is None
        or classification.conversation_mode not in companion_modes
        or classification.conversation_mode not in persona.applies_to
    ):
        return None
    return persona


async def select_strategy(
    models: ModelGateway,
    classification: DialogueClassification,
    risk: RiskDecision,
    evidence: ValidatedEvidence,
    prompt: PromptArtifact,
    persona: PersonaArtifact | None,
) -> StrategyDecision:
    effective = _applicable_persona(classification, persona)
    request = StrategyRequest(
        dialogue_classification=classification,
        risk_decision=risk,
        validated_evidence=evidence,
        prompt_ref=prompt.ref,
        resolved_persona_ref=effective.ref if effective else None,
        persona_directives=effective.expression_principles if effective else (),
    )
    proposed = await invoke_structured_model(
        models,
        "strategy_advisor", request, StrategyProposalResult
    )
    return StrategyDecision(
        **proposed.model_dump(), persona_ref=effective.ref if effective else None
    )


async def generate_response(
    models: ModelGateway,
    snapshot: ConversationSnapshot,
    strategy: StrategyDecision,
    evidence: ValidatedEvidence,
    prompt: PromptArtifact,
    persona: PersonaArtifact | None,
) -> ResponseDraft:
    """Internal node; Task 8's controller owns caller and persona provenance."""
    effective = (
        persona
        if persona is not None and strategy.persona_ref == persona.ref
        else None
    )
    request = GenerationRequest(
        conversation_snapshot=snapshot,
        strategy_decision=strategy,
        validated_evidence=evidence,
        prompt_ref=prompt.ref,
        persona=effective,
    )
    return await invoke_structured_model(
        models, "response_generator", request, ResponseDraft
    )


async def repair_response(
    models: ModelGateway,
    draft: ResponseDraft,
    validation: ValidationResult,
    strategy: StrategyDecision,
    evidence: ValidatedEvidence,
    prompt: PromptArtifact,
    persona: PersonaArtifact | None,
) -> ResponseDraft:
    """Internal node; Task 8's controller owns caller and persona provenance."""
    effective = (
        persona
        if persona is not None and strategy.persona_ref == persona.ref
        else None
    )
    failed_criteria = validate_failed_criteria(validation.failed_criteria)
    request = RepairRequest(
        draft=draft,
        failed_criteria=failed_criteria,
        evidence=evidence,
        prompt_ref=prompt.ref,
        persona=effective,
    )
    return await invoke_structured_model(
        models, "response_generator", request, ResponseDraft
    )
