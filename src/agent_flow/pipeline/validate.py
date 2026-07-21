import re

from agent_flow.adapters.models import ModelGateway
from agent_flow.contracts import (
    JudgeVerdict,
    ResponseDraft,
    StrictModel,
    ValidatedEvidence,
    ValidationResult,
)
from agent_flow.pipeline.model_outputs import JudgeVerdictResult
from agent_flow.pipeline.policy import invoke_structured_model


class JudgeRequest(StrictModel):
    draft: ResponseDraft
    verified_evidence: ValidatedEvidence


def _hard_failures(
    draft: ResponseDraft, evidence: ValidatedEvidence
) -> tuple[str, ...]:
    failures: list[str] = []
    known_ids = {item.evidence_id for item in evidence.items}
    if evidence.items and (
        not draft.evidence_ids
        or any(reference not in known_ids for reference in draft.evidence_ids)
    ):
        failures.append("UNSUPPORTED_EVIDENCE_REFERENCE")

    allowed_citations: set[str] = set()
    for item in evidence.items:
        allowed_citations.update((item.evidence_id, item.source_id))
        arguments = item.metadata.get("arguments")
        if isinstance(arguments, dict):
            allowed_citations.update(
                f"{item.source_id}:{value}"
                for value in arguments.values()
                if isinstance(value, (str, int, float))
            )
    if evidence.items and (
        not draft.citations
        or any(citation not in allowed_citations for citation in draft.citations)
    ):
        failures.append("CITATION_MISMATCH")

    text = draft.text.casefold()
    if re.search(
        r"\b(?:deliver(?:ed)?|arriv(?:e|es|ed))\b.{0,20}\b(?:tomorrow|today|on \d)|"
        r"(?:明天|今日|今天|保證|保证).{0,10}(?:送達|送达|到貨|到货)",
        text,
    ):
        failures.append("UNSUPPORTED_DELIVERY_PROMISE")
    if re.search(r"(?:[$€£¥]\s*\d|\b(?:usd|ntd|twd)\s*\d)", text):
        failures.append("UNSUPPORTED_PRICE")
    if re.search(
        r"\b(?:we|i) will (?:refund|cancel|replace|credit)\b|"
        r"(?:已|會|会)(?:替你|幫你|帮你).{0,6}(?:退款|取消|更換|更换)",
        text,
    ):
        failures.append("UNSUPPORTED_ACTION_COMMITMENT")
    return tuple(dict.fromkeys(failures))


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
    primary = await invoke_structured_model(
        models,
        "response_judge", request, JudgeVerdictResult
    )
    if assurance_mode == "bootstrap":
        return _single_result(primary)

    secondary = await invoke_structured_model(
        models,
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
import re
