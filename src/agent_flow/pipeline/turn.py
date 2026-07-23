import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from inspect import isawaitable
from typing import Any, TypeVar
from uuid import UUID, uuid4

from agent_flow.artifacts import RuntimeArtifacts, resolve_persona
from agent_flow.auth import AuthorizedCustomerContext
from agent_flow.contracts import (
    AssuranceMetadata,
    ArtifactRef,
    HandoffEvent,
    TurnRequest,
    TurnResult,
    ConversationMode,
)
from agent_flow.errors import AgentError
from agent_flow.pipeline.classify import classify_dialogue
from agent_flow.pipeline.evidence import collect_evidence, plan_evidence, validate_evidence
from agent_flow.pipeline.respond import generate_response, repair_response, select_strategy
from agent_flow.pipeline.risk import risk_precheck
from agent_flow.pipeline.validate import validate_response


T = TypeVar("T")
JSONValue = Any
NodeOperation = Callable[[], T | Awaitable[T]]


def _validated_trace_metadata(
    metadata: Mapping[str, JSONValue] | None,
) -> dict[str, JSONValue]:
    values = dict(metadata or {})
    allowed = {
        "prompt_ref", "persona_ref", "strategy_prompt_ref",
        "response_prompt_ref", "persona_refs",
    }
    if set(values) - allowed:
        raise ValueError("trace metadata may contain only immutable artifact references")
    normalized: dict[str, JSONValue] = {}
    for key, value in values.items():
        if value is None:
            normalized[key] = None
            continue
        if key == "persona_refs":
            if not isinstance(value, list):
                raise ValueError("trace metadata persona_refs must be a list")
            normalized[key] = [
                ArtifactRef.model_validate(item).model_dump(mode="json")
                for item in value
            ]
        else:
            normalized[key] = ArtifactRef.model_validate(value).model_dump(mode="json")
    return normalized


@dataclass
class TurnState:
    trace_id: UUID
    context: AuthorizedCustomerContext
    request: TurnRequest
    spans: dict[str, UUID] = field(default_factory=dict)
    spans_finished: set[UUID] = field(default_factory=set)
    primary_failure_event_id: int | None = None
    snapshot: Any = None
    classification: Any = None
    risk: Any = None
    persona: Any = None
    evidence_plan: Any = None
    collected_evidence: Any = None
    evidence: Any = None
    strategy: Any = None
    draft: Any = None
    validation: Any = None
    handoff_enqueued: bool = False
    handoff_reason: str | None = None
    delivery_disposition: str | None = None
    suppress_handoff: bool = False


def _causal_error(error: BaseException) -> BaseException:
    if isinstance(error, BaseExceptionGroup):
        for child in error.exceptions:
            causal = _causal_error(child)
            if isinstance(causal, AgentError):
                return causal
        if error.exceptions:
            return _causal_error(error.exceptions[0])
    return error


def _flatten_errors(error: BaseException) -> list[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        return [leaf for child in error.exceptions for leaf in _flatten_errors(child)]
    return [error]


def _error_details(error: BaseException, node: str) -> tuple[str, str, str | None]:
    causal = _causal_error(error)
    if isinstance(causal, AgentError):
        component = causal.component or node
        if component == "tool" and causal.operation == "order.lookup":
            component = "order_api"
        return causal.error_code, component, causal.operation
    if isinstance(causal, asyncio.CancelledError):
        component = getattr(causal, "component", node)
        operation = getattr(causal, "operation", None)
        if component == "tool" and operation == "order.lookup":
            component = "order_api"
        return "CANCELLED", component, operation
    return "UNEXPECTED_ERROR", node, None


def _risk_or_raise(classification, message):
    decision = risk_precheck(classification, message)
    if decision.requires_handoff:
        raise AgentError.validation(
            decision.reason_code or "HIGH_RISK", failure_stage="risk_precheck"
        )
    return decision


async def _validate_or_raise(models, draft, evidence, assurance_mode, *, final: bool):
    result = await validate_response(models, draft, evidence, assurance_mode)
    if not result.passed and (final or not result.repairable):
        raise AgentError.validation(
            "VALIDATION_EXHAUSTED", failure_stage="response_validator"
        )
    return result


class TurnPipeline:
    def __init__(
        self, *, traces, conversations, handoffs, models, rag, tools,
        artifacts: RuntimeArtifacts | None, clock, assurance_mode: str,
    ) -> None:
        if assurance_mode not in {"bootstrap", "dual_judge"}:
            raise ValueError("assurance_mode must be 'bootstrap' or 'dual_judge'")
        self.traces = traces
        self.conversations = conversations
        self.handoffs = handoffs
        self.models = models
        self.rag = rag
        self.tools = tools
        self.artifacts = artifacts
        self.clock = clock
        self.assurance_mode = assurance_mode
        self._cleanup_tasks: set[asyncio.Task] = set()

    async def _event(
        self, state: TurnState, span_id: UUID, node: str, status: str, *,
        attempt: int, metadata: Mapping[str, JSONValue] | None = None,
        error: BaseException | None = None,
    ):
        error_code = None
        component = node
        operation = None
        if error is not None:
            error_code, component, operation = _error_details(error, node)
        payload: dict[str, Any] = {
            "node": node, "attempt": attempt,
            "lifecycle_id": f"{span_id}:{status}",
        }
        if metadata:
            payload["metadata"] = dict(metadata)
        if error is not None:
            payload["failure_stage"] = node
            payload["operation"] = operation
        return await self.traces.append_event(
            trace_id=state.trace_id,
            span_id=span_id,
            tenant_id=state.context.tenant_id,
            event_type="node",
            component=component,
            status=status,
            error_code=error_code,
            payload=payload,
        )

    async def run_node(
        self, state: TurnState, name: str, operation: NodeOperation[T], attempt: int = 1,
        trace_metadata: Mapping[str, JSONValue] | None = None,
    ) -> T:
        metadata = _validated_trace_metadata(trace_metadata)
        span_id = uuid4()
        state.spans[f"{name}:{attempt}"] = span_id
        operation_done = False
        try:
            await self._retry_idempotent(
                lambda: self.traces.start_span(
                    state.trace_id, name, tenant_id=state.context.tenant_id,
                    attempt=attempt, span_id=span_id,
                )
            )
            await self._retry_idempotent(
                lambda: self._event(
                    state, span_id, name, "started", attempt=attempt,
                    metadata=metadata,
                )
            )
            pending_or_value = operation()
            value = await pending_or_value if isawaitable(pending_or_value) else pending_or_value
            operation_done = True
            await self._retry_idempotent(
                lambda: self.traces.finish_span(
                    span_id, "completed", tenant_id=state.context.tenant_id
                )
            )
            await self._retry_idempotent(
                lambda: self._event(
                    state, span_id, name, "completed", attempt=attempt,
                    metadata=metadata,
                )
            )
        except asyncio.CancelledError as error:
            event = await self._bounded_cleanup(
                self._settle_cancelled_node(
                    state, span_id, name, attempt, error, metadata,
                    operation_done=operation_done,
                )
            )
            if event is not None and state.primary_failure_event_id is None:
                state.primary_failure_event_id = event.id
            raise
        except BaseExceptionGroup as group:
            leaves = _flatten_errors(group)[:20]
            ordered = sorted(
                leaves,
                key=lambda value: (
                    isinstance(value, asyncio.CancelledError),
                    _error_details(value, name)[1],
                    _error_details(value, name)[2] or "",
                    _error_details(value, name)[0],
                ),
            )
            causal = next(
                (value for value in ordered if isinstance(value, AgentError)),
                next((value for value in ordered if not isinstance(value, asyncio.CancelledError)), ordered[0]),
            )
            if isinstance(causal, asyncio.CancelledError):
                await self._settle_failure_safely(
                    state, span_id, name, attempt, ordered, causal,
                    terminal_status="cancelled", is_group=True,
                )
                raise causal
            await self._settle_failure_safely(
                state, span_id, name, attempt, ordered, causal, is_group=True
            )
            raise causal
        except Exception as error:
            causal = _causal_error(error)
            await self._settle_failure_safely(
                state, span_id, name, attempt, [causal], causal
            )
            raise causal
        return value

    async def _settle_failure_safely(
        self, state, span_id, name, attempt, outcomes, causal, *,
        terminal_status="failed", ensure_span=False, is_group=False,
    ):
        operation = lambda: self._settle_failure(
            state, span_id, name, attempt, outcomes, causal,
            terminal_status=terminal_status, ensure_span=ensure_span, is_group=is_group,
        )
        try:
            return await operation()
        except asyncio.CancelledError:
            await self._bounded_cleanup(operation())
            raise

    async def _settle_failure(
        self, state, span_id, name, attempt, outcomes, causal, *,
        terminal_status="failed", ensure_span=False, is_group=False,
    ):
        if ensure_span:
            await self._retry_idempotent(
                lambda: self.traces.start_span(
                    state.trace_id, name, tenant_id=state.context.tenant_id,
                    attempt=attempt, span_id=span_id,
                ), retry_cancellation=True,
            )
            await self._retry_idempotent(
                lambda: self._event(
                    state, span_id, name, "started", attempt=attempt
                ), retry_cancellation=True,
            )
        primary = None
        if is_group:
            for index, child in enumerate(outcomes[:20]):
                event = await self._retry_idempotent(
                    lambda child=child, index=index: self._child_event(
                        state, span_id, name, attempt, child, index
                    ), retry_cancellation=True,
                )
                if child is causal:
                    primary = event
        else:
            primary = await self._retry_idempotent(
                lambda: self._event(
                    state, span_id, name, terminal_status, attempt=attempt,
                    error=causal,
                ), retry_cancellation=True,
            )
        if primary is not None and state.primary_failure_event_id is None:
            state.primary_failure_event_id = primary.id
        if span_id not in state.spans_finished:
            code, _, _ = _error_details(causal, name)
            await self._retry_idempotent(
                lambda: self.traces.finish_span(
                    span_id, terminal_status, tenant_id=state.context.tenant_id,
                    error_code=code,
                ), retry_cancellation=True,
            )
            state.spans_finished.add(span_id)
        return primary

    async def _settle_cancelled_node(
        self, state, span_id, name, attempt, error, metadata, *, operation_done
    ):
        await self._retry_idempotent(
            lambda: self.traces.start_span(
                state.trace_id, name, tenant_id=state.context.tenant_id,
                attempt=attempt, span_id=span_id,
            ), retry_cancellation=True,
        )
        if operation_done:
            await self._retry_idempotent(
                lambda: self.traces.finish_span(
                    span_id, "completed", tenant_id=state.context.tenant_id
                ), retry_cancellation=True,
            )
            await self._retry_idempotent(
                lambda: self._event(
                    state, span_id, name, "completed", attempt=attempt,
                    metadata=metadata,
                ), retry_cancellation=True,
            )
            return None
        return await self._retry_idempotent(
            lambda: self._cancel_node(state, span_id, name, attempt, error),
            retry_cancellation=True,
        )

    async def _child_event(self, state, span_id, name, attempt, error, index):
        code, component, operation = _error_details(error, name)
        status = "cancelled" if isinstance(error, asyncio.CancelledError) else "failed"
        return await self.traces.append_event(
            trace_id=state.trace_id, span_id=span_id,
            tenant_id=state.context.tenant_id, event_type="node_child",
            component=component, status=status, error_code=code,
            payload={
                "node": name, "attempt": attempt, "child_index": index,
                "failure_stage": name, "operation": operation,
                "lifecycle_id": f"{span_id}:child:{index}",
            },
        )

    async def _cancel_node(self, state, span_id, name, attempt, error):
        outcomes = getattr(error, "outcomes", ())
        ordered = sorted(
            outcomes[:20],
            key=lambda child: (
                _error_details(child, name)[1],
                _error_details(child, name)[2] or "",
                _error_details(child, name)[0],
            ),
        )
        for index, child in enumerate(ordered):
            await self._child_event(
                state, span_id, name, attempt, child, index
            )
        event = await self._event(
            state, span_id, name, "cancelled", attempt=attempt, error=error
        )
        await self.traces.finish_span(
            span_id, "cancelled", tenant_id=state.context.tenant_id,
            error_code="CANCELLED",
        )
        return event

    async def _bounded_cleanup(self, pending, timeout: float = 1.0):
        task = asyncio.create_task(pending)
        self._cleanup_tasks.add(task)
        def consume(done):
            self._cleanup_tasks.discard(done)
            try:
                done.result()
            except BaseException:
                pass
        task.add_done_callback(consume)
        deadline = asyncio.get_running_loop().time() + timeout
        while not task.done():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                task.cancel()
                await asyncio.wait({task}, timeout=min(.01, timeout))
                return None
            try:
                await asyncio.wait_for(asyncio.shield(task), remaining)
            except asyncio.CancelledError:
                if task.done():
                    break
                continue
            except TimeoutError:
                task.cancel()
                await asyncio.wait({task}, timeout=min(.01, timeout))
                return None
            except Exception:
                return None
        result = await asyncio.gather(task, return_exceptions=True)
        return None if isinstance(result[0], BaseException) else result[0]

    async def _retry_idempotent(
        self, operation, attempts: int = 2, *, retry_cancellation: bool = False
    ):
        last_error = None
        for _ in range(attempts):
            try:
                return await operation()
            except Exception as error:
                last_error = error
            except asyncio.CancelledError as error:
                if not retry_cancellation:
                    raise
                last_error = error
        raise last_error

    def _artifact_metadata(self) -> dict[str, JSONValue]:
        if self.artifacts is None:
            return {}
        return {
            "strategy_prompt_ref": self.artifacts.strategy_prompt.ref.model_dump(mode="json"),
            "response_prompt_ref": self.artifacts.response_prompt.ref.model_dump(mode="json"),
            "persona_refs": [p.ref.model_dump(mode="json") for p in self.artifacts.personas],
        }

    async def _retry_artifact_metadata(self, state: TurnState, retry_of: UUID):
        source = await self.traces.get_trace(
            retry_of, tenant_id=state.context.tenant_id
        )
        if (
            source is None
            or source.customer_id != state.context.customer_id
            or source.session_id != state.request.session_id
        ):
            raise AgentError.validation(
                "RETRY_SCOPE_MISMATCH", failure_stage="context_loader"
            )
        event = next(
            (
                value for value in source.events
                if value.node == "context_loader"
                and value.kind in {"started", "completed"}
                and value.metadata
            ),
            None,
        )
        if event is None:
            raise AgentError.validation(
                "ARTIFACT_SNAPSHOT_MISSING", failure_stage="context_loader"
            )
        return _validated_trace_metadata(event.metadata)

    async def _load_context(
        self, state: TurnState, retry_of: UUID | None,
        frozen_artifacts: Mapping[str, JSONValue],
    ):
        if retry_of is not None:
            captured = await self.conversations.get_retry_turn_input(
                retry_of, tenant_id=state.context.tenant_id,
                customer_id=state.context.customer_id, bind_trace_id=state.trace_id,
            )
            if captured.request != state.request:
                raise AgentError.validation(
                    "RETRY_INPUT_MISMATCH", failure_stage="context_loader"
                )
            snapshot = await self.conversations.get_retry_snapshot(
                retry_of, tenant_id=state.context.tenant_id,
                customer_id=state.context.customer_id, bind_trace_id=state.trace_id,
            )
        else:
            await self.conversations.capture_turn_input(
                tenant_id=state.context.tenant_id,
                customer_id=state.context.customer_id,
                session_id=state.request.session_id,
                trace_id=state.trace_id,
                request=state.request,
            )
            snapshot = await self.conversations.get_snapshot(
                tenant_id=state.context.tenant_id,
                customer_id=state.context.customer_id,
                session_id=state.request.session_id, trace_id=state.trace_id,
            )
        if dict(frozen_artifacts) != self._artifact_metadata():
            raise AgentError.validation(
                "ARTIFACT_VERSION_UNRESOLVED", failure_stage="context_loader"
            )
        return snapshot

    async def run(
        self, context: AuthorizedCustomerContext, request: TurnRequest,
        retry_of: UUID | None = None, *, retry_initiator: str | None = None,
        retry_reason: str | None = None, delivery_disposition: str | None = None,
        suppress_handoff: bool = False, max_retry_count: int | None = None,
    ) -> TurnResult:
        trace_id = await self.traces.start_trace(
            tenant_id=context.tenant_id, customer_id=context.customer_id,
            session_id=request.session_id, retry_of_trace_id=retry_of,
            retry_initiator=(retry_initiator or "api") if retry_of else None,
            retry_reason=(retry_reason or "full_turn_retry") if retry_of else None,
            delivery_disposition=delivery_disposition,
            max_retry_count=max_retry_count,
        )
        state = TurnState(
            trace_id=trace_id, context=context, request=request,
            delivery_disposition=delivery_disposition,
            suppress_handoff=suppress_handoff,
        )
        try:
            frozen_artifacts = (
                await self._retry_artifact_metadata(state, retry_of)
                if retry_of is not None else self._artifact_metadata()
            )
            state.snapshot = await self.run_node(
                state, "context_loader",
                lambda: self._load_context(state, retry_of, frozen_artifacts),
                trace_metadata=frozen_artifacts,
            )
            state.classification = await self.run_node(
                state, "dialogue_classifier",
                lambda: classify_dialogue(self.models, (*state.snapshot.messages, request.message)),
            )
            state.risk = await self.run_node(
                state, "risk_precheck", lambda: _risk_or_raise(state.classification, request.message)
            )
            state.persona = resolve_persona(
                state.classification.conversation_mode, self.artifacts.personas
            )
            state.evidence_plan = await self.run_node(
                state, "evidence_planner", lambda: plan_evidence(state.classification)
            )
            state.collected_evidence = await self.run_node(
                state, "evidence_collector",
                lambda: collect_evidence(context, state.evidence_plan, self.rag, self.tools),
            )
            state.evidence = await self.run_node(
                state, "evidence_validator",
                lambda: validate_evidence(state.evidence_plan, state.collected_evidence, self.clock.now()),
            )
            state.strategy = await self.run_node(
                state, "strategy_selector",
                lambda: select_strategy(
                    self.models, state.classification, state.risk, state.evidence,
                    self.artifacts.strategy_prompt, state.persona,
                ),
                trace_metadata={
                    "prompt_ref": self.artifacts.strategy_prompt.ref.model_dump(mode="json"),
                    "persona_ref": (
                        state.persona.ref.model_dump(mode="json")
                        if state.persona is not None and state.classification.conversation_mode
                        in {ConversationMode.EMOTIONAL_SUPPORT, ConversationMode.CASUAL}
                        else None
                    ),
                },
            )
            state.draft = await self.run_node(
                state, "response_generator",
                lambda: generate_response(
                    self.models, state.snapshot, state.strategy, state.evidence,
                    self.artifacts.response_prompt, state.persona,
                ),
                trace_metadata={
                    "prompt_ref": self.artifacts.response_prompt.ref.model_dump(mode="json"),
                    "persona_ref": state.strategy.persona_ref.model_dump(mode="json") if state.strategy.persona_ref else None,
                },
            )
            state.validation = await self.run_node(
                state, "response_validator",
                lambda: _validate_or_raise(
                    self.models, state.draft, state.evidence, self.assurance_mode,
                    final=False,
                ),
            )
            if not state.validation.passed and state.validation.repairable:
                state.draft = await self.run_node(
                    state, "response_repair",
                    lambda: repair_response(
                        self.models, state.draft, state.validation, state.strategy,
                        state.evidence, self.artifacts.response_prompt, state.persona,
                    ),
                    trace_metadata={
                        "prompt_ref": self.artifacts.response_prompt.ref.model_dump(mode="json"),
                        "persona_ref": state.strategy.persona_ref.model_dump(mode="json") if state.strategy.persona_ref else None,
                    },
                )
                state.validation = await self.run_node(
                    state, "response_validator",
                    lambda: _validate_or_raise(
                        self.models, state.draft, state.evidence, self.assurance_mode,
                        final=True,
                    ),
                    attempt=2,
                )
            return await self._finalize(state)
        except asyncio.CancelledError:
            await self._bounded_cleanup(
                self._retry_idempotent(
                    lambda: self.traces.finish_trace(
                        state.trace_id, "failed", tenant_id=state.context.tenant_id,
                        primary_failure_event_id=state.primary_failure_event_id,
                        terminal_outcome="cancelled", delivery_disposition="suppressed",
                    ),
                    retry_cancellation=True,
                )
            )
            raise
        except AgentError as error:
            return await self._handoff(
                state, error.error_code, failed_node=error.failure_stage or "pipeline",
                existing_failure=True,
            )
        except Exception:
            return await self._handoff(
                state, state.handoff_reason or "UNEXPECTED_ERROR",
                failed_node="pipeline", existing_failure=True,
            )

    async def _mark_failure(self, state: TurnState, reason: str, failed_node: str, attempt: int = 1) -> int:
        span_id = state.spans.get(f"{failed_node}:{attempt}")
        created_span = span_id is None
        if span_id is None:
            span_id = uuid4()
            state.spans[f"{failed_node}:{attempt}"] = span_id
        error = AgentError.validation(reason, failure_stage=failed_node)
        event = await self._settle_failure_safely(
            state, span_id, failed_node, attempt, [error], error,
            ensure_span=created_span,
        )
        return event.id

    async def _handoff(
        self, state: TurnState, reason: str, *, failed_node: str,
        attempt: int = 1, existing_failure: bool = False,
    ) -> TurnResult:
        if not existing_failure or state.primary_failure_event_id is None:
            await self._mark_failure(state, reason, failed_node, attempt)
        safe_message = "A human specialist will review this request."
        state.handoff_reason = state.handoff_reason or reason
        if (
            self.handoffs is not None
            and not state.handoff_enqueued
            and not state.suppress_handoff
        ):
            await self._retry_idempotent(
                lambda: self.handoffs.enqueue(
                    trace_id=state.trace_id, tenant_id=state.context.tenant_id,
                    customer_id=state.context.customer_id, session_id=state.request.session_id,
                    reason_code=state.handoff_reason,
                    idempotency_key=str(state.trace_id),
                    primary_failure_event_id=state.primary_failure_event_id,
                    delivery_disposition=state.delivery_disposition or "suppressed",
                )
            )
            state.handoff_enqueued = True
        await self._retry_idempotent(
            lambda: self.traces.finish_trace(
                state.trace_id, "failed", tenant_id=state.context.tenant_id,
                primary_failure_event_id=state.primary_failure_event_id,
                terminal_outcome="handoff",
                delivery_disposition=state.delivery_disposition or "suppressed",
            )
        )
        return TurnResult(
            trace_id=state.trace_id, text=None,
            handoff=HandoffEvent(required=True, reason_code=state.handoff_reason, safe_message=safe_message),
            assurance=self._assurance(),
        )

    def _assurance(self) -> AssuranceMetadata:
        return AssuranceMetadata(
            mode="reduced_assurance" if self.assurance_mode == "bootstrap" else "dual_judge",
            judges=("response_judge",) if self.assurance_mode == "bootstrap" else ("response_judge", "response_judge_zh_verifier"),
        )

    async def _finalize(self, state: TurnState) -> TurnResult:
        await self.run_node(
            state,
            "conversation_persistence",
            lambda: (
                None
                if state.delivery_disposition == "review_required"
                else self._retry_idempotent(
                    lambda: self.conversations.append_turn(
                        tenant_id=state.context.tenant_id,
                        customer_id=state.context.customer_id,
                        session_id=state.request.session_id,
                        trace_id=state.trace_id,
                        customer_text=state.request.message,
                        assistant_text=state.draft.text,
                        citations=state.draft.citations,
                    )
                )
            ),
        )
        await self._retry_idempotent(
            lambda: self.traces.finish_trace(
                state.trace_id, "succeeded", tenant_id=state.context.tenant_id,
                terminal_outcome="reply",
                delivery_disposition=state.delivery_disposition or "deliver",
            )
        )
        return TurnResult(
            trace_id=state.trace_id, text=state.draft.text,
            citations=state.draft.citations, assurance=self._assurance(),
        )
