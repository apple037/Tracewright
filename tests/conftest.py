import pytest

from agent_flow.contracts import ResponseDraft
from tests.fakes import FakeModelGateway


@pytest.fixture
def fake_models():
    return FakeModelGateway(
        {
            "dialogue_classifier": [
                {
                    "intent": "order_status",
                    "conversation_mode": "transactional_read",
                    "urgency": "normal",
                    "language": "zh-TW",
                    "emotion": {
                        "category": "stress_exhaustion",
                        "dialogue_stage": "surface",
                        "override": "none",
                        "response_mode": "business_first",
                        "confidence": 0.91,
                        "evidence_spans": ["等很久"],
                        "reason_codes": ["EXPLICIT_EXHAUSTION"],
                    },
                }
            ],
            "strategy_advisor": [
                {
                    "strategy_version": "bootstrap-v1",
                    "response_mode": "business_first",
                    "answer_order": ["verified_fact", "brief_acknowledgment"],
                    "reason_codes": [
                        "TRANSACTIONAL_READ",
                        "VERIFIED_EVIDENCE_AVAILABLE",
                    ],
                }
            ],
            "response_generator": [
                {
                    "text": "訂單仍在運送中，目前沒有可驗證的送達日期。",
                    "citations": ["tool:order.lookup:o1"],
                    "evidence_ids": ["tool-result-1"],
                }
            ],
            "response_judge": [
                {
                    "passed": True,
                    "failed_criteria": [],
                    "confidence": 0.88,
                    "reason_codes": ["GROUNDED"],
                }
            ],
        }
    )


@pytest.fixture
def verified_draft():
    return ResponseDraft(
        text="訂單仍在運送中，目前沒有可驗證的送達日期。",
        citations=("tool:order.lookup:o1",),
        evidence_ids=("tool-result-1",),
    )
