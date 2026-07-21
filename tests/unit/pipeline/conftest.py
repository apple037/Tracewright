import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from uuid import uuid4

from agent_flow.artifacts import ArtifactRegistry
from agent_flow.auth import AuthorizedCustomerContext
from agent_flow.contracts import (
    CollectedEvidence,
    ConversationMode,
    DialogueClassification,
    EmotionAssessment,
    EvidenceItem,
    EvidencePlan,
    EvidenceToolCall,
    ResponseMode,
    StrategyDecision,
    ValidatedEvidence,
    ValidationResult,
)


class TraceSpy:
    def __init__(self):
        self.completed_nodes = []
        self.events = []
        self.finished = []

    async def start_span(self, trace_id, name, *, tenant_id, attempt=1):
        return uuid4()

    async def append_event(self, **kwargs):
        from types import SimpleNamespace
        event = SimpleNamespace(
            id=len(self.events) + 1,
            metadata=kwargs["payload"].get("metadata", {}),
            **kwargs,
        )
        self.events.append(event)
        return event

    async def finish_span(self, span_id, status, *, tenant_id, error_code=None):
        self.finished.append((span_id, status, error_code))
        if status == "completed":
            started = next(e for e in reversed(self.events) if e.span_id == span_id)
            self.completed_nodes.append(started.payload["node"])


@pytest.fixture
def trace_spy():
    return TraceSpy()


def _item(
    evidence_id: str,
    content: str,
    retrieved_at: datetime,
    *,
    fact: str = "order.current_status",
    valid_until: datetime | None = None,
) -> EvidenceItem:
    structured_content = json.dumps(
        {"status": content}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return EvidenceItem(
        evidence_id=evidence_id,
        source_id="tool:order.lookup",
        version="v1",
        content=structured_content,
        content_checksum=hashlib.sha256(structured_content.encode()).hexdigest(),
        retrieved_at=retrieved_at,
        valid_until=valid_until,
        metadata={
            "fact": fact,
            "tool": "order.lookup",
            "arguments": {"order_id": "order-1"},
        },
    )


@pytest.fixture
def utc_now():
    return datetime(2026, 7, 21, 12, tzinfo=timezone.utc)


@pytest.fixture
def classification():
    return DialogueClassification(
        intent="order_status",
        conversation_mode=ConversationMode.TRANSACTIONAL_READ,
        urgency="normal",
        language="zh-TW",
        emotion=EmotionAssessment(
            category="stress_exhaustion",
            dialogue_stage="surface",
            override="none",
            response_mode=ResponseMode.BUSINESS_FIRST,
            confidence=0.9,
            evidence_spans=("很累",),
            reason_codes=("EXPLICIT_EXHAUSTION",),
        ),
    )


@pytest.fixture
def order_plan():
    return EvidencePlan(
        required_facts=("order.current_status",),
        tool_calls=(
            EvidenceToolCall(
                operation="order.lookup",
                arguments={"order_id": "order-1"},
                freshness_seconds=60,
            ),
        ),
    )


@pytest.fixture
def fresh_collected_evidence(utc_now):
    return CollectedEvidence(
        items=(_item("tool-result-1", "in_transit", utc_now - timedelta(seconds=10)),)
    )


@pytest.fixture
def expired_collected_evidence(utc_now):
    return CollectedEvidence(
        items=(_item("tool-result-1", "in_transit", utc_now - timedelta(seconds=61)),)
    )


@pytest.fixture
def validated_evidence(fresh_collected_evidence):
    return ValidatedEvidence(
        items=fresh_collected_evidence.items,
        sufficient=True,
        reason_codes=("REQUIRED_EVIDENCE_PRESENT",),
    )


@pytest.fixture
def strategy_prompt():
    return ArtifactRegistry(Path("config/prompts")).load_prompt(
        "strategy_selector.v1.yaml"
    )


@pytest.fixture
def response_prompt():
    return ArtifactRegistry(Path("config/prompts")).load_prompt(
        "response_generator.v1.yaml"
    )


@pytest.fixture
def companion_persona():
    return ArtifactRegistry(Path("config/personas")).load_persona(
        "familiar_companion.zh-TW.v1.yaml"
    )


@pytest.fixture
def transactional_strategy():
    return StrategyDecision(
        strategy_version="bootstrap-v1",
        response_mode="business_first",
        answer_order=["verified_fact"],
        reason_codes=["TRANSACTIONAL_READ"],
    )


@pytest.fixture
def repairable_validation():
    return ValidationResult(
        passed=False,
        failed_criteria=("UNSUPPORTED_DELIVERY_PROMISE",),
        confidence=0.9,
        reason_codes=("UNSUPPORTED_CLAIM",),
        assurance="reduced_assurance",
        repairable=True,
    )


@pytest.fixture
def authorized_context():
    return AuthorizedCustomerContext(
        subject_id="subject-1", tenant_id="tenant-1", customer_id="customer-1"
    )
