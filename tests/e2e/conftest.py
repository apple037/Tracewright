import hashlib
import json
from datetime import datetime, timedelta, timezone
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
        max_retry_count = scope.pop("max_retry_count", None)
        channel = scope.pop("channel", None)
        external_message_id = scope.pop("external_message_id", None)
        retry_of = scope.get("retry_of_trace_id")
        if retry_of is not None:
            source = self.records.get(retry_of)
            if source is None or (
                source.tenant_id, source.customer_id, source.session_id
            ) != (scope["tenant_id"], scope["customer_id"], scope["session_id"]):
                raise ValueError("retry source trace does not belong to this scope")
        trace_id = uuid4()
        root_trace_id = source.root_trace_id if retry_of is not None else trace_id
        retry_sequence = (
            max(
                (record.retry_sequence for record in self.records.values()
                 if record.root_trace_id == root_trace_id),
                default=0,
            ) + 1
            if retry_of is not None else 0
        )
        if max_retry_count is not None and retry_sequence > max_retry_count:
            raise ValueError("retry lineage limit reached")
        self.records[trace_id] = SimpleNamespace(
            id=trace_id, spans=[], events=[], status="running", primary_failure_event_id=None,
            terminal_outcome=None, issue_summary=None, root_trace_id=root_trace_id,
            retry_sequence=retry_sequence, created_at=datetime.now(timezone.utc),
            finished_at=None, channel=channel,
            external_message_id=external_message_id, **scope,
        )
        return trace_id

    async def reserve_for_test(
        self,
        *,
        tenant_id,
        customer_id,
        session_id,
        retry_of_trace_id=None,
        retry_initiator=None,
        retry_reason=None,
        delivery_disposition=None,
    ):
        trace_id = await self.start_trace(
            tenant_id=tenant_id,
            customer_id=customer_id,
            session_id=session_id,
            retry_of_trace_id=retry_of_trace_id,
            retry_initiator=retry_initiator,
            retry_reason=retry_reason,
            delivery_disposition=delivery_disposition,
        )
        self.records[trace_id].status = "queued"
        return trace_id

    async def activate_trace(
        self, trace_id, *, tenant_id, expected_retry_of
    ):
        record = self.records.get(trace_id)
        if record is None or record.tenant_id != tenant_id:
            raise ValueError("trace does not exist")
        if record.retry_of_trace_id != expected_retry_of:
            raise ValueError("trace retry lineage does not match")
        if record.status == "running" and record.finished_at is None:
            return
        if record.status != "queued" or record.finished_at is not None:
            raise ValueError("trace cannot be activated")
        record.status = "running"

    async def start_span(self, trace_id, name, *, tenant_id, attempt=1, span_id=None):
        record = self.records[trace_id]
        if record.tenant_id != tenant_id or record.status != "running":
            raise ValueError("trace is not mutable in this tenant")
        span_id = span_id or uuid4()
        existing = next((s for s in record.spans if s.id == span_id), None)
        if existing is not None:
            if (existing.name, existing.attempt) == (name, attempt):
                return span_id
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
            sequence=len(record.events) + 1, created_at=datetime.now(timezone.utc),
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
        record.finished_at = datetime.now(timezone.utc)
        record.delivery_disposition = delivery_disposition
        if primary_failure_event_id:
            event = next(e for e in record.events if e.id == primary_failure_event_id)
            record.issue_summary = SimpleNamespace(
                error_code=event.error_code, failed_node=event.node,
                component=event.component, operation=event.operation,
            )

    async def get_trace(self, trace_id, *, tenant_id, customer_id=None):
        record = self.records.get(trace_id)
        return (
            record
            if record is not None
            and record.tenant_id == tenant_id
            and (customer_id is None or record.customer_id == customer_id)
            else None
        )

    async def events_after(self, trace_id, *, tenant_id, after_sequence, customer_id=None):
        record = await self.get_trace(
            trace_id, tenant_id=tenant_id, customer_id=customer_id
        )
        if record is None:
            return ()
        return tuple(event for event in record.events if event.sequence > after_sequence)

    async def count_retries(self, root_trace_id, *, tenant_id, customer_id):
        return sum(
            record.retry_sequence > 0
            for record in self.records.values()
            if record.root_trace_id == root_trace_id
            and record.tenant_id == tenant_id
            and record.customer_id == customer_id
        )

    async def list_traces(
        self, *, tenant_id, customer_id, status, before_created_at, before_id, limit,
    ):
        items = [
            record
            for record in self.records.values()
            if record.tenant_id == tenant_id
            and (customer_id is None or record.customer_id == customer_id)
            and (status is None or record.status == status)
        ]
        items.sort(key=lambda record: (record.created_at, str(record.id)), reverse=True)
        if before_created_at is not None:
            items = [
                record for record in items
                if (record.created_at, str(record.id))
                < (before_created_at, str(before_id))
            ]
        return tuple(items[:limit])


class MemoryConversations:
    def __init__(self, now, traces):
        self.now, self.traces = now, traces
        self.persisted, self.turns_by_trace, self.snapshots, self.scopes = [], {}, {}, {}
        self.inputs = {}

    async def capture_turn_input(
        self, *, tenant_id, customer_id, session_id, trace_id, request,
        captured_at=None,
    ):
        from agent_flow.contracts import CapturedTurnInput
        value = CapturedTurnInput(
            request=request,
            captured_at=captured_at or self.now,
            expires_at=(captured_at or self.now) + timedelta(days=30),
        )
        existing = self.inputs.get(trace_id)
        if existing is not None and existing != value:
            raise ValueError("turn input binding conflicts")
        self.inputs[trace_id] = value
        self.scopes[trace_id] = (tenant_id, customer_id, session_id)
        return value

    async def get_retry_turn_input(
        self, trace_id, *, tenant_id, customer_id, bind_trace_id=None,
    ):
        scope = self.scopes.get(trace_id)
        if scope is None or scope[:2] != (tenant_id, customer_id):
            raise ValueError("turn input does not exist in this scope")
        value = self.inputs.get(trace_id)
        if value is None:
            raise ValueError("turn input does not exist in this scope")
        if bind_trace_id is not None:
            existing = self.inputs.get(bind_trace_id)
            if existing is not None and existing != value:
                raise ValueError("turn input binding conflicts")
            self.inputs[bind_trace_id] = value
            self.scopes[bind_trace_id] = scope
        return value

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
        if (
            scope is None
            or scope[:2] != (tenant_id, customer_id)
            or trace_id not in self.snapshots
        ):
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


class MemorySubmissions:
    """In-memory queue that executes the reserved turn inline, like the worker."""

    def __init__(self, traces, pipeline):
        self.traces = traces
        self.pipeline = pipeline
        self.records = {}
        self.by_key = {}

    async def enqueue(
        self, context, message, *, retry_of_trace_id=None, retry_initiator=None,
        retry_reason=None, delivery_disposition=None,
    ):
        from agent_flow.repositories.submissions import SubmissionRecord
        from agent_flow.contracts import SubmissionResult

        payload = {
            "message": message.model_dump(mode="json"),
            "retry": {
                "retry_of_trace_id": (
                    str(retry_of_trace_id) if retry_of_trace_id is not None else None
                ),
                "retry_initiator": retry_initiator,
                "retry_reason": retry_reason,
                "delivery_disposition": delivery_disposition,
            },
        }
        key = (context.tenant_id, message.idempotency_key)
        if key in self.by_key:
            existing = self.records[self.by_key[key]]
            if existing.customer_id != context.customer_id or existing.payload != payload:
                raise ValueError("submission idempotency conflict")
            return existing
        trace_id = await self.traces.reserve_for_test(
            tenant_id=context.tenant_id, customer_id=context.customer_id,
            session_id=message.session_id, retry_of_trace_id=retry_of_trace_id,
            retry_initiator=retry_initiator, retry_reason=retry_reason,
            delivery_disposition=delivery_disposition,
        )
        record = self.traces.records[trace_id]
        record.channel = message.channel
        record.external_message_id = message.external_message_id
        submission_id = uuid4()
        worker_context = AuthorizedCustomerContext(
            subject_id="turn-worker:test", tenant_id=context.tenant_id,
            customer_id=context.customer_id,
        )
        result = await self.pipeline.run(
            worker_context, message.to_turn_request(),
            retry_of=retry_of_trace_id, trace_id=trace_id,
            retry_initiator=retry_initiator, retry_reason=retry_reason,
            delivery_disposition=delivery_disposition,
            suppress_handoff=(delivery_disposition == "review_required"),
            max_retry_count=3 if retry_of_trace_id is not None else None,
        )
        safe = SubmissionResult(
            submission_id=submission_id, trace_id=result.trace_id,
            status="completed", text=result.text, citations=result.citations,
            handoff=result.handoff,
        )
        now = datetime.now(timezone.utc)
        submission = SubmissionRecord(
            id=submission_id, trace_id=result.trace_id, tenant_id=context.tenant_id,
            customer_id=context.customer_id, status="completed", attempts=1,
            payload=payload, result=safe.model_dump(mode="json"),
            last_error_code=None, last_error_component=None, lease_expires_at=None,
            claim_token=None, created_at=now, finished_at=now,
            retry_of_trace_id=retry_of_trace_id,
        )
        self.records[submission_id] = submission
        self.by_key[key] = submission_id
        return submission

    async def get(self, submission_id, *, tenant_id, customer_id):
        record = self.records.get(submission_id)
        if record is None or record.tenant_id != tenant_id or record.customer_id != customer_id:
            return None
        return record

    async def get_by_trace(self, trace_id, *, tenant_id, customer_id):
        for record in self.records.values():
            if (
                record.trace_id == trace_id
                and record.tenant_id == tenant_id
                and record.customer_id == customer_id
            ):
                return record
        return None


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


@pytest.fixture
def invalid_artifact_root(tmp_path):
    root = tmp_path / "invalid-artifacts"
    (root / "prompts").mkdir(parents=True)
    (root / "personas").mkdir()
    (root / "prompts" / "strategy_selector.v1.yaml").write_text("schema_version: nope", encoding="utf-8")
    return root


@pytest.fixture
def app_factory(pipeline):
    from agent_flow.auth import AuthenticatedPrincipal
    from agent_flow.main import create_app

    principals = {
        "customer": AuthenticatedPrincipal(
            subject_id="customer-u1", tenant_id="t1", customer_id="c1",
            scopes=frozenset({"turn:write", "trace:read"}),
        ),
        "admin": AuthenticatedPrincipal(
            subject_id="admin-u1", tenant_id="t1", customer_id=None,
            scopes=frozenset(
                {"customer:act_as", "trace:read", "trace:retry", "trace:admin"}
            ),
        ),
        "other-tenant": AuthenticatedPrincipal(
            subject_id="admin-u2", tenant_id="t2", customer_id=None,
            scopes=frozenset({"customer:act_as", "trace:read", "trace:retry"}),
        ),
        "internal-c2": AuthenticatedPrincipal(
            subject_id="internal-u2", tenant_id="t1", customer_id="c2",
            scopes=frozenset({"trace:internal"}),
        ),
        "trace-only": AuthenticatedPrincipal(
            subject_id="trace-u1", tenant_id="t1", customer_id="c1",
            scopes=frozenset({"trace:read"}),
        ),
        "customer-reader": AuthenticatedPrincipal(
            subject_id="customer-r1", tenant_id="t1", customer_id="c1",
            scopes=frozenset({"turn:write", "trace:read", "trace:retry"}),
        ),
    }

    async def authenticate(token):
        return principals.get(token)

    def factory(
        *, artifact_root=__import__("pathlib").Path("config"),
        pipeline_override=None, traces_override=None,
        dependency_checks=None,
    ):
        from agent_flow.inbound import InboundMessageService

        selected = pipeline_override or pipeline
        submissions = MemorySubmissions(selected.traces, selected)
        inbound = InboundMessageService(submissions, poll_interval=0.001)
        return create_app(
            pipeline=selected,
            traces=traces_override or selected.traces,
            conversations=selected.conversations,
            authenticate=authenticate,
            artifact_root=artifact_root,
            submissions=submissions,
            inbound=inbound,
            legacy_turn_timeout_seconds=5.0,
            dependency_checks=(
                {"database": "ok", "models": "ok"}
                if dependency_checks is None else dependency_checks
            ),
        )

    return factory
