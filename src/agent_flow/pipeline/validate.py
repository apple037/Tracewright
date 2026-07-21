from agent_flow.adapters.models import ModelGateway
from agent_flow.contracts import (
    JudgeVerdict,
    ResponseDraft,
    StrictModel,
    ValidatedEvidence,
    ValidationResult,
)
from agent_flow.pipeline.model_outputs import JudgeVerdictResult


class JudgeRequest(StrictModel):
    draft: ResponseDraft
    verified_evidence: ValidatedEvidence


def _hard_failures(
    draft: ResponseDraft, evidence: ValidatedEvidence
) -> tuple[str, ...]:
    known_ids = {item.evidence_id for item in evidence.items}
    if any(reference not in known_ids for reference in draft.evidence_ids):
        return ("UNSUPPORTED_EVIDENCE_REFERENCE",)
    return ()


def _single_result(verdict: JudgeVerdict) -> ValidationResult:
    return ValidationResult(
        **verdict.model_dump(),
        assurance="reduced_assurance",
        repairable=not verdict.passed,
    )


async def validate_response(
    models: ModelGateway,
    draft: ResponseDraft,
    evidence: ValidatedEvidence,
    assurance_mode: str,
) -> ValidationResult:
    if assurance_mode not in {"bootstrap", "dual_judge"}:
        raise ValueError("assurance_mode must be 'bootstrap' or 'dual_judge'")
    hard = _hard_failures(draft, evidence)
    if hard:
        return ValidationResult(
            passed=False,
            failed_criteria=hard,
            confidence=1.0,
            reason_codes=("DETERMINISTIC_HARD_FAILURE",),
            assurance=(
                "reduced_assurance" if assurance_mode == "bootstrap" else "dual_judge"
            ),
            repairable=False,
        )

    request = JudgeRequest(draft=draft, verified_evidence=evidence)
    primary = await models.structured(
        "response_judge", request, JudgeVerdictResult
    )
    if assurance_mode == "bootstrap":
        return _single_result(primary)

    secondary = await models.structured(
        "response_judge_zh_verifier", request, JudgeVerdictResult
    )
    failed = tuple(dict.fromkeys((*primary.failed_criteria, *secondary.failed_criteria)))
    reasons = tuple(dict.fromkeys((*primary.reason_codes, *secondary.reason_codes)))
    passed = primary.passed and secondary.passed
    return ValidationResult(
        passed=passed,
        failed_criteria=failed,
        confidence=min(primary.confidence, secondary.confidence),
        reason_codes=reasons,
        assurance="dual_judge",
        repairable=not passed,
    )
