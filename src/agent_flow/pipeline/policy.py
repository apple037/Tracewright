from types import MappingProxyType
from typing import Final, Mapping, TypeVar

from pydantic import BaseModel

from agent_flow.adapters.models import ModelGateway
from agent_flow.errors import AgentError


T = TypeVar("T", bound=BaseModel)


MODEL_ALLOWED_ACTIONS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "dialogue_classifier": frozenset(),
        "strategy_advisor": frozenset(),
        "response_generator": frozenset(),
        "response_judge": frozenset(),
        "response_judge_zh_verifier": frozenset(),
        "promotion_judge_primary": frozenset(),
        "promotion_judge_secondary": frozenset(),
    }
)

EVIDENCE_COLLECTOR_ALLOWED_ACTIONS: Final[frozenset[str]] = frozenset(
    {"order.lookup"}
)


async def invoke_structured_model(
    models: ModelGateway,
    role: str,
    request: object,
    response_type: type[T],
    *,
    attempted_actions: frozenset[str] = frozenset(),
    system_prompt: str | None = None,
) -> T:
    allowed = MODEL_ALLOWED_ACTIONS.get(role)
    if allowed is None:
        raise AgentError.validation(
            "MODEL_ROLE_POLICY_MISSING", retryable=False, component=role
        )
    if not attempted_actions.issubset(allowed):
        raise AgentError.validation(
            "MODEL_ACTION_NOT_ALLOWED", retryable=False, component=role
        )
    return await models.structured(
        role, request, response_type, system_prompt=system_prompt
    )
