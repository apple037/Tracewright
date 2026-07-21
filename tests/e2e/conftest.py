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
        retry_of = scope.get("retry_of_trace_id")
        if retry_of is not None:
            source = self.records.get(retry_of)
            if source is None or (
                source.tenant_id, source.customer_id, source.session_id
            ) != (scope["tenant_id"], scope["customer_id"], scope["session_id"]):
                raise ValueError("retry source trace does not belong to this scope")
        trace_id = uuid4()
        self.records[trace_id] = SimpleNamespace(
            id=trace_id, spans=[], events=[], status="running", primary_failure_event_id=None,
            terminal_outcome=None, issue_summary=None, **scope,
        )
        return trace_id

    async def start_span(self, trace_id, name, *, tenant_id, attempt=1, span_id=None):
        record = self.records[trace_id]
        if record.tenant_id != tenant_id or record.status != "running":
            raise ValueError("trace is not mutable in this tenant")
        span_id = span_id or uuid4()
        existing = next((s for s in record.spans if s.id == span_id), None)
        if existing is not None:
            if (existing.name, existing.attempt) == (name, attempt): return span_id
            raise ValueError("span identity replay conflicts")
        span = SimpleNamespace(id=span_id, node=name, name=name, attempt=attempt, status="running", error_code=None)
        self.records[trace_id].spans.append(span)
        return span.id

    async def append_event(self, **kwargs):
        self._event_id += 1
        record = self.records[kwargs["trace_id"]]
        if record.tenant_id != kwargs["tenant_id"] or record.status != "running":
            raise ValueError("trace is not mutable in this tenant")
        payload = kwargs["payload"]
        lifecycle_id = payload.get("lifecycle_id")
        existing = next((e for e in record.events if e.payload.get("lifecycle_id") == lifecycle_id), None) if lifecycle_id else None
        if existing is not None:
            if (
                existing.span_id, existing.event_type, existing.component,
                existing.kind, existing.error_code, existing.payload
            ) == (
                kwargs["span_id"], kwargs["event_type"], kwargs["component"],
                kwargs["status"], kwargs.get("error_code"), payload,
            ):
                return existing
            raise ValueError("event lifecycle replay conflicts")
        event = SimpleNamespace(
            id=self._event_id, node=payload.get("node"), kind=kwargs["status"],
            event_type=kwargs["event_type"],
            metadata=payload.get("metadata", {}), payload=payload,
            error_code=kwargs.get("error_code"), component=kwargs["component"],
            operation=payload.get("operation"), span_id=kwargs["span_id"],
        )
        record.events.append(event)
        return event

    async def finish_span(self, span_id, status, *, tenant_id, error_code=None):
        span = next(s for r in self.records.values() for s in r.spans if s.id == span_id)
        if span.status != "running":
            if (span.status, span.error_code) == (status, error_code):
                return
            raise ValueError("span is already finished")
        span.status, span.error_code = status, error_code

    async def finish_trace(self, trace_id, status, *, tenant_id, primary_failure_event_id=None, terminal_outcome=None, delivery_disposition=None):
        record = self.records[trace_id]
        terminal = (status, primary_failure_event_id, terminal_outcome, delivery_disposition)
        if record.status != "running":
            existing = (record.status, record.primary_failure_event_id, record.terminal_outcome, getattr(record, "delivery_disposition", None))
            if existing == terminal:
                return
            raise ValueError("trace is already finalized with conflicting values")
        record.status, record.primary_failure_event_id, record.terminal_outcome = status, primary_failure_event_id, terminal_outcome
        record.delivery_disposition = delivery_disposition
        if primary_failure_event_id:
            event = next(e for e in record.events if e.id == primary_failure_event_id)
            record.issue_summary = SimpleNamespace(
                error_code=event.error_code, failed_node=event.node,
                component=event.component, operation=event.operation,
            )

    async def get_trace(self, trace_id, *, tenant_id):
        record = self.records.get(trace_id)
        return record if record is not None and record.tenant_id == tenant_id else None


class MemoryConversations:
    def __init__(self, now, traces):
        self.now, self.traces = now, traces
        self.persisted, self.turns_by_trace, self.snapshots, self.scopes = [], {}, {}, {}

    async def get_snapshot(self, *, tenant_id, customer_id, session_id, trace_id):
        messages = ["prior"]
        for source_trace, turn in self.turns_by_trace.items():
            record = self.traces.records[source_trace]
            if record.status == "succeeded" and (
                record.tenant_id, record.customer_id, record.session_id
            ) == (tenant_id, customer_id, session_id):
                messages.extend((turn["customer_text"], turn["assistant_text"]))
        snapshot = ConversationSnapshot(session_id=session_id, messages=tuple(messages), captured_at=self.now)
        self.snapshots[trace_id] = snapshot
        self.scopes[trace_id] = (tenant_id, customer_id, session_id)
        return snapshot

    async def get_retry_snapshot(self, trace_id, *, tenant_id, customer_id, bind_trace_id=None):
        scope = self.scopes.get(trace_id)
        if scope is None or scope[:2] != (tenant_id, customer_id):
            raise ValueError("retry snapshot does not exist in this scope")
        snapshot = self.snapshots[trace_id]
        if bind_trace_id is not None:
            self.snapshots[bind_trace_id] = snapshot
            self.scopes[bind_trace_id] = scope
        return snapshot

    async def append_turn(self, **turn):
        trace = self.traces.records[turn["trace_id"]]
        if trace.status not in {"running", "succeeded"} or (
            trace.tenant_id, trace.customer_id, trace.session_id
        ) != (turn["tenant_id"], turn["customer_id"], turn["session_id"]):
            raise ValueError("trace does not belong to conversation scope")
        if turn["trace_id"] not in self.turns_by_trace:
            self.turns_by_trace[turn["trace_id"]] = turn
            self.persisted.append(turn)


class MemoryHandoffs:
    def __init__(self): self.items, self.keys = [], set()
    async def enqueue(self, **item):
        key = item["idempotency_key"]
        if key not in self.keys:
            self.keys.add(key)
            self.items.append(item)
        return key


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
    traces = MemoryTraces()
    return TurnPipeline(
        traces=traces, conversations=MemoryConversations(clock.value, traces), handoffs=MemoryHandoffs(),
        models=fake_models, rag=Rag(), tools=Tool(), artifacts=load_runtime_artifacts(__import__("pathlib").Path("config")),
        clock=clock, assurance_mode="bootstrap",
    )
