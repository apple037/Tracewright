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
    system_rules: tuple[str, ...] = ()
    resolved_persona_ref: ArtifactRef | None = None
    persona_directives: tuple[str, ...] = ()


class GenerationRequest(StrictModel):
    # What the customer just said. Without this the model is drafting a reply to
    # a message it has never seen.
    customer_message: str
    conversation_snapshot: ConversationSnapshot
    strategy_decision: StrategyDecision
    validated_evidence: ValidatedEvidence
    prompt_ref: ArtifactRef
    system_rules: tuple[str, ...] = ()
    persona: PersonaArtifact | None = None


class RepairRequest(StrictModel):
    customer_message: str
    conversation_snapshot: ConversationSnapshot
    draft: ResponseDraft
    failed_criteria: tuple[str, ...]
    evidence: ValidatedEvidence
    prompt_ref: ArtifactRef
    system_rules: tuple[str, ...] = ()
    persona: PersonaArtifact | None = None


def _reconcile_citations(draft: ResponseDraft) -> ResponseDraft:
    # Models reliably fill evidence_ids but often omit citations. Each
    # evidence_id is itself a valid citation, so mirror them when the model
    # claimed evidence yet cited nothing — a format fix, not invented grounding.
    if draft.evidence_ids and not draft.citations:
        return draft.model_copy(update={"citations": draft.evidence_ids})
    return draft


def _system_prompt(
    prompt: PromptArtifact, persona: PersonaArtifact | None
) -> str | None:
    # The persona's voice is appended to the node's own instructions, so a
    # persona edit changes wording without touching the node's rules.
    parts = [prompt.system_prompt.strip()] if prompt.system_prompt.strip() else []
    if persona is not None and persona.style_prompt.strip():
        parts.append("Voice and tone:\n" + persona.style_prompt.strip())
    return "\n\n".join(parts) if parts else None


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
        system_rules=prompt.system_rules,
        resolved_persona_ref=effective.ref if effective else None,
        persona_directives=effective.expression_principles if effective else (),
    )
    proposed = await invoke_structured_model(
        models,
        "strategy_advisor", request, StrategyProposalResult,
        system_prompt=_system_prompt(prompt, effective),
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
    customer_message: str,
) -> ResponseDraft:
    """Internal node; Task 8's controller owns caller and persona provenance."""
    effective = (
        persona
        if persona is not None and strategy.persona_ref == persona.ref
        else None
    )
    request = GenerationRequest(
        customer_message=customer_message,
        conversation_snapshot=snapshot,
        strategy_decision=strategy,
        validated_evidence=evidence,
        prompt_ref=prompt.ref,
        system_rules=prompt.system_rules,
        persona=effective,
    )
    draft = await invoke_structured_model(
        models, "response_generator", request, ResponseDraft,
        system_prompt=_system_prompt(prompt, effective),
    )
    return _reconcile_citations(draft)


async def repair_response(
    models: ModelGateway,
    draft: ResponseDraft,
    validation: ValidationResult,
    strategy: StrategyDecision,
    evidence: ValidatedEvidence,
    prompt: PromptArtifact,
    persona: PersonaArtifact | None,
    customer_message: str,
    snapshot: ConversationSnapshot,
) -> ResponseDraft:
    """Internal node; Task 8's controller owns caller and persona provenance."""
    effective = (
        persona
        if persona is not None and strategy.persona_ref == persona.ref
        else None
    )
    failed_criteria = validate_failed_criteria(validation.failed_criteria)
    request = RepairRequest(
        customer_message=customer_message,
        conversation_snapshot=snapshot,
        draft=draft,
        failed_criteria=failed_criteria,
        evidence=evidence,
        prompt_ref=prompt.ref,
        system_rules=prompt.system_rules,
        persona=effective,
    )
    draft = await invoke_structured_model(
        models, "response_generator", request, ResponseDraft,
        system_prompt=_system_prompt(prompt, effective),
    )
    return _reconcile_citations(draft)
