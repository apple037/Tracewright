import re

from agent_flow.contracts import DialogueClassification, RiskDecision


_RISK_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "SELF_HARM",
        re.compile(r"(?:傷害|杀害|殺害|結束|结束).{0,6}(?:自己|生命)|自殺|自杀", re.I),
    ),
    (
        "ACCOUNT_SECURITY",
        re.compile(
            r"帳號.{0,8}(?:被盜|被盗|入侵|駭)|账号.{0,8}(?:被盗|入侵)|account.{0,12}(?:hack|stol)",
            re.I,
        ),
    ),
    (
        "PAYMENT_FRAUD",
        re.compile(r"(?:信用卡|银行卡|銀行卡|payment).{0,10}(?:盜刷|盗刷|fraud)", re.I),
    ),
    (
        "IMMEDIATE_DANGER",
        re.compile(r"(?:現在|马上|立刻|immediate).{0,8}(?:危險|危险|danger)", re.I),
    ),
)


def risk_precheck(
    classification: DialogueClassification, message: str
) -> RiskDecision:
    for reason_code, pattern in _RISK_RULES:
        if pattern.search(message):
            return RiskDecision(requires_handoff=True, reason_code=reason_code)
    if classification.urgency == "critical":
        return RiskDecision(requires_handoff=True, reason_code="CRITICAL_URGENCY")
    return RiskDecision.safe()
