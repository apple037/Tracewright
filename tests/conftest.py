import pytest

from agent_flow.contracts import ResponseDraft
from tests.fakes import FakeModelGateway


def pytest_addoption(parser):
    parser.addoption(
        "--run-live-model",
        action="store_true",
        default=False,
        help="run tests that require the configured local model endpoint",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "live_model: requires an explicitly enabled live model endpoint"
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-live-model"):
        return
    skip = pytest.mark.skip(reason="requires --run-live-model")
    for item in items:
        if "live_model" in item.keywords:
            item.add_marker(skip)


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
                    "knowledge_topic": None,
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
                    "citations": ["tool:order.lookup:order-1"],
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
        citations=("tool:order.lookup:order-1",),
        evidence_ids=("tool-result-1",),
    )
