import re
import unicodedata

from agent_flow.contracts import DialogueClassification, RiskDecision


_RISK_RULES: tuple[tuple[str, re.Pattern[str], re.Pattern[str] | None], ...] = (
    (
        "SELF_HARM",
        re.compile(
            r"(?:kill|hurt|harm).{0,12}myself|(?<!not )suicidal|suicide(?! prevention)|"
            r"end my life|i want to die|i do not want to live(?: anymore)?|"
            r"(?:傷害|伤害|殺害|杀害|結束|结束).{0,6}(?:自己|生命)|"
            r"自殺(?!防治)|自杀(?!防治)|不想活(?:了|下去)?|我想死"
        ),
        None,
    ),
    (
        "IMMEDIATE_DANGER",
        re.compile(
            r"(?:i am|i'm|we are).{0,10}(?:in danger|being threatened)|"
            r"(?:danger|threat).{0,12}(?:right now|now|immediate)|"
            r"(?:he|she|they|someone).{0,12}(?:threaten|trying).{0,12}(?:kill|hurt|attack) me|"
            r"(?:he|she|they|someone).{0,12}(?:is )?(?:attack(?:ing)?|hurt(?:ing)?|threaten(?:ing)?) me(?: right now)?|"
            r"(?:現在|现在|馬上|马上|立刻).{0,8}(?:危險|危险|被威脅|被威胁)|"
            r"(?:他|她|有人).{0,8}(?:威脅|威胁|要|正在).{0,6}(?:殺|杀|傷害|伤害|攻擊|攻击)我"
        ),
        None,
    ),
    (
        "UNLAWFUL_REQUEST",
        re.compile(
            r"how (?:do|can) i .{0,10}(?:hack|steal|break into)|"
            r"(?:teach|show) me .{0,12}(?:hack|steal|break into)|"
            r"教我.{0,10}(?:駭|骇|入侵|盜用|盗用|偷|破解).{0,12}(?:帳號|账号|account)?"
        ),
        None,
    ),
    (
        "ACCOUNT_SECURITY",
        re.compile(
            r"(?:account|login).{0,16}(?:hack|stol|taken over|compromis|hijack)|"
            r"(?:someone|they).{0,12}(?:took over|control).{0,10}(?:account|login)|"
            r"(?:帳號|账号|登入|登錄|登录).{0,12}(?:被盜|被盗|入侵|駭|骇|控制)|"
            r"(?:別人|别人|有人).{0,8}(?:控制|接管).{0,8}(?:帳號|账号|登入|登录)?"
        ),
        None,
    ),
    (
        "PAYMENT_FRAUD",
        re.compile(
            r"(?:card|payment|charge).{0,18}(?:fraud|without (?:my )?permission|not mine|unauthori[sz]ed)|"
            r"unrecogni[sz]ed (?:charge|payment)|"
            r"(?:信用卡|銀行卡|银行卡|付款|交易).{0,12}(?:盜刷|盗刷|詐騙|诈骗|不是我的)|"
            r"(?:這筆|这笔).{0,6}(?:不是我刷|非本人)"
        ),
        None,
    ),
    (
        "SENSITIVE_DATA",
        re.compile(
            r"(?:my |here is (?:my )?)(?:password|passcode|pin|credit card number).{0,8}(?::|is)\s*\S+|"
            r"(?:我的|這是我的|这是我的)(?:密碼|密码|驗證碼|验证码|信用卡號|信用卡号).{0,6}(?:是|:|：)\s*\S+"
        ),
        None,
    ),
    (
        "HUMAN_REQUEST",
        re.compile(
            r"(?:talk|speak|connect|transfer).{0,10}(?:human|person|agent|representative)|"
            r"(?:真人|人工)(?:客服|服務|服务|專員|专员)|(?:我要|轉接|转接).{0,6}(?:真人|人工|客服專員|客服专员)"
        ),
        None,
    ),
)


def _normalize(message: str) -> str:
    normalized = unicodedata.normalize("NFKC", message).casefold()
    return re.sub(r"\s+", " ", normalized).strip()


def risk_precheck(
    classification: DialogueClassification, message: str
) -> RiskDecision:
    normalized = _normalize(message)
    for reason_code, pattern, exclusion in _RISK_RULES:
        candidate = normalized
        if reason_code == "SELF_HARM":
            candidate = re.sub(
                r"\b(?:i )?(?:do not|don't|dont|never) (?:want to )?"
                r"(?:kill|hurt|harm) myself\b",
                "",
                candidate,
            )
        if pattern.search(candidate) and not (
            exclusion is not None and exclusion.search(candidate)
        ):
            return RiskDecision(requires_handoff=True, reason_code=reason_code)
    if classification.urgency == "critical":
        return RiskDecision(requires_handoff=True, reason_code="CRITICAL_URGENCY")
    return RiskDecision.safe()
