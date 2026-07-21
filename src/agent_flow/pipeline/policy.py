from types import MappingProxyType
from typing import Final, Mapping


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
