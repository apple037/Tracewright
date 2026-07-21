import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_flow.artifacts import load_runtime_artifacts
from agent_flow.auth import AuthorizedCustomerContext
from agent_flow.contracts import ConversationSnapshot, EvidenceItem, ToolCallResult


class MemoryTraces:
    def __init__(self):
        self.records = {}
        self._event_id = 0

    async def start_trace(self, **scope):
        trace_id = uuid4()
        self.records[trace_id] = SimpleNamespace(
            id=trace_id, spans=[], events=[], status="running", primary_failure_event_id=None,
            terminal_outcome=None, issue_summary=None, **scope,
        )
        return trace_id

    async def start_span(self, trace_id, name, *, tenant_id, attempt=1):
        span = SimpleNamespace(id=uuid4(), node=name, name=name, attempt=attempt, status="running", error_code=None)
        self.records[trace_id].spans.append(span)
        return span.id

    async def append_event(self, **kwargs):
        self._event_id += 1
        record = self.records[kwargs["trace_id"]]
        payload = kwargs["payload"]
        event = SimpleNamespace(
            id=self._event_id, node=payload.get("node"), kind=kwargs["status"],
            metadata=payload.get("metadata", {}), payload=payload,
            error_code=kwargs.get("error_code"), component=kwargs["component"],
            operation=payload.get("operation"), span_id=kwargs["span_id"],
        )
        record.events.append(event)
        return event

    async def finish_span(self, span_id, status, *, tenant_id, error_code=None):
        span = next(s for r in self.records.values() for s in r.spans if s.id == span_id)
        span.status, span.error_code = status, error_code

    async def finish_trace(self, trace_id, status, *, tenant_id, primary_failure_event_id=None, terminal_outcome=None, delivery_disposition=None):
        record = self.records[trace_id]
        record.status, record.primary_failure_event_id, record.terminal_outcome = status, primary_failure_event_id, terminal_outcome
        if primary_failure_event_id:
            event = next(e for e in record.events if e.id == primary_failure_event_id)
            record.issue_summary = SimpleNamespace(
                error_code=event.error_code, failed_node=event.node,
                component=event.component, operation=event.operation,
            )

    async def get_trace(self, trace_id, *, tenant_id):
        return self.records.get(trace_id)


class MemoryConversations:
    def __init__(self, now):
        self.now, self.persisted, self.snapshots = now, [], {}

    async def get_snapshot(self, *, tenant_id, customer_id, session_id, trace_id):
        snapshot = ConversationSnapshot(session_id=session_id, messages=("prior",), captured_at=self.now)
        self.snapshots[trace_id] = snapshot
        return snapshot

    async def get_retry_snapshot(self, trace_id, *, tenant_id, customer_id, bind_trace_id=None):
        snapshot = self.snapshots[trace_id]
        if bind_trace_id is not None:
            self.snapshots[bind_trace_id] = snapshot
        return snapshot

    async def append_turn(self, **turn):
        self.persisted.append(turn)


class MemoryHandoffs:
    def __init__(self): self.items = []
    async def enqueue(self, **item): self.items.append(item)


class Clock:
    def __init__(self): self.value = datetime(2026, 7, 21, 12, tzinfo=timezone.utc)
    def now(self): return self.value


class Tool:
    async def call(self, context, request):
        content = json.dumps({"status": "in_transit"}, separators=(",", ":"))
        item = EvidenceItem(
            evidence_id="tool-result-1", source_id="tool:order.lookup", version="v1",
            content=content, content_checksum=hashlib.sha256(content.encode()).hexdigest(),
            retrieved_at=datetime(2026, 7, 21, 12, tzinfo=timezone.utc),
            metadata={"fact": "order.current_status", "tool": "order.lookup", "arguments": {"order_id": "current"}},
        )
        return ToolCallResult(tool=request.tool, evidence=item)


class Rag:
    async def search(self, context, request): raise AssertionError("unexpected RAG")


@pytest.fixture
def context(): return AuthorizedCustomerContext(subject_id="u1", tenant_id="t1", customer_id="c1")

@pytest.fixture
def pipeline(fake_models):
    from agent_flow.pipeline.turn import TurnPipeline
    fake_models.responses["response_generator"].clear()
    fake_models.responses["response_generator"].append({
        "text": "訂單仍在運送中，目前沒有可驗證的送達日期。",
        "citations": ["tool-result-1"], "evidence_ids": ["tool-result-1"],
    })
    clock = Clock()
    return TurnPipeline(
        traces=MemoryTraces(), conversations=MemoryConversations(clock.value), handoffs=MemoryHandoffs(),
        models=fake_models, rag=Rag(), tools=Tool(), artifacts=load_runtime_artifacts(__import__("pathlib").Path("config")),
        clock=clock, assurance_mode="bootstrap",
    )
