import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from inspect import isawaitable
from typing import Any, TypeVar
from uuid import UUID

from agent_flow.artifacts import RuntimeArtifacts, resolve_persona
from agent_flow.auth import AuthorizedCustomerContext
from agent_flow.contracts import (
    AssuranceMetadata,
    ArtifactRef,
    HandoffEvent,
    TurnRequest,
    TurnResult,
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


def _causal_error(error: BaseException) -> BaseException:
    if isinstance(error, BaseExceptionGroup):
        for child in error.exceptions:
            causal = _causal_error(child)
            if isinstance(causal, AgentError):
                return causal
        if error.exceptions:
            return _causal_error(error.exceptions[0])
    return error


def _error_details(error: BaseException, node: str) -> tuple[str, str, str | None]:
    causal = _causal_error(error)
    if isinstance(causal, AgentError):
        component = causal.component or node
        if component == "tool" and causal.operation == "order.lookup":
            component = "order_api"
        return causal.error_code, component, causal.operation
    if isinstance(causal, asyncio.CancelledError):
        return "CANCELLED", node, None
    return "UNEXPECTED_ERROR", node, None


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
        payload: dict[str, Any] = {"node": node, "attempt": attempt}
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
        span_id = await self.traces.start_span(
            state.trace_id, name, tenant_id=state.context.tenant_id, attempt=attempt
        )
        state.spans[f"{name}:{attempt}"] = span_id
        await self._event(state, span_id, name, "started", attempt=attempt)
        try:
            pending_or_value = operation()
            value = await pending_or_value if isawaitable(pending_or_value) else pending_or_value
        except asyncio.CancelledError as error:
            await self._event(state, span_id, name, "cancelled", attempt=attempt, error=error)
            await self.traces.finish_span(span_id, "cancelled", tenant_id=state.context.tenant_id, error_code="CANCELLED")
            raise
        except Exception as error:
            causal = _causal_error(error)
            event = await self._event(state, span_id, name, "failed", attempt=attempt, error=causal)
            code, _, _ = _error_details(causal, name)
            await self.traces.finish_span(span_id, "failed", tenant_id=state.context.tenant_id, error_code=code)
            if state.primary_failure_event_id is None:
                state.primary_failure_event_id = event.id
            raise causal
        await self.traces.finish_span(span_id, "completed", tenant_id=state.context.tenant_id)
        await self._event(state, span_id, name, "completed", attempt=attempt, metadata=metadata)
        return value

    def _artifact_metadata(self) -> dict[str, JSONValue]:
        if self.artifacts is None:
            return {}
        return {
            "strategy_prompt_ref": self.artifacts.strategy_prompt.ref.model_dump(mode="json"),
            "response_prompt_ref": self.artifacts.response_prompt.ref.model_dump(mode="json"),
            "persona_refs": [p.ref.model_dump(mode="json") for p in self.artifacts.personas],
        }

    async def run(
        self, context: AuthorizedCustomerContext, request: TurnRequest, retry_of: UUID | None = None
    ) -> TurnResult:
        trace_id = await self.traces.start_trace(
            tenant_id=context.tenant_id, customer_id=context.customer_id,
            session_id=request.session_id, retry_of_trace_id=retry_of,
            retry_initiator="api" if retry_of else None,
            retry_reason="full_turn_retry" if retry_of else None,
        )
        state = TurnState(trace_id=trace_id, context=context, request=request)
        try:
            state.snapshot = await self.run_node(
                state, "context_loader",
                lambda: self.conversations.get_retry_snapshot(
                    retry_of, tenant_id=context.tenant_id, customer_id=context.customer_id,
                    bind_trace_id=trace_id,
                ) if retry_of is not None else self.conversations.get_snapshot(
                    tenant_id=context.tenant_id, customer_id=context.customer_id,
                    session_id=request.session_id, trace_id=trace_id,
                ),
                trace_metadata=self._artifact_metadata(),
            )
            state.classification = await self.run_node(
                state, "dialogue_classifier",
                lambda: classify_dialogue(self.models, (*state.snapshot.messages, request.message)),
            )
            state.risk = await self.run_node(
                state, "risk_precheck", lambda: risk_precheck(state.classification, request.message)
            )
            if state.risk.requires_handoff:
                return await self._handoff(
                    state, state.risk.reason_code or "HIGH_RISK", failed_node="risk_precheck"
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
                    "persona_ref": state.persona.ref.model_dump(mode="json") if state.persona else None,
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
                lambda: validate_response(self.models, state.draft, state.evidence, self.assurance_mode),
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
                    lambda: validate_response(self.models, state.draft, state.evidence, self.assurance_mode),
                    attempt=2,
                )
            if not state.validation.passed:
                return await self._handoff(state, "VALIDATION_EXHAUSTED", failed_node="response_validator", attempt=2 if "response_validator:2" in state.spans else 1)
            return await self._finalize(state)
        except AgentError as error:
            return await self._handoff(
                state, error.error_code, failed_node=error.failure_stage or "pipeline",
                existing_failure=True,
            )
        except Exception:
            return await self._handoff(
                state, "UNEXPECTED_ERROR", failed_node="pipeline", existing_failure=True
            )

    async def _mark_failure(self, state: TurnState, reason: str, failed_node: str, attempt: int = 1) -> int:
        span_id = state.spans.get(f"{failed_node}:{attempt}")
        created_span = span_id is None
        if span_id is None:
            span_id = await self.traces.start_span(
                state.trace_id, failed_node, tenant_id=state.context.tenant_id, attempt=attempt
            )
        error = AgentError.validation(reason, failure_stage=failed_node)
        event = await self._event(state, span_id, failed_node, "failed", attempt=attempt, error=error)
        if created_span:
            await self.traces.finish_span(
                span_id, "failed", tenant_id=state.context.tenant_id,
                error_code=reason,
            )
        state.primary_failure_event_id = event.id
        return event.id

    async def _handoff(
        self, state: TurnState, reason: str, *, failed_node: str,
        attempt: int = 1, existing_failure: bool = False,
    ) -> TurnResult:
        if not existing_failure or state.primary_failure_event_id is None:
            await self._mark_failure(state, reason, failed_node, attempt)
        safe_message = "A human specialist will review this request."
        if self.handoffs is not None:
            await self.handoffs.enqueue(
                trace_id=state.trace_id, tenant_id=state.context.tenant_id,
                customer_id=state.context.customer_id, session_id=state.request.session_id,
                reason_code=reason,
            )
        await self.traces.finish_trace(
            state.trace_id, "failed", tenant_id=state.context.tenant_id,
            primary_failure_event_id=state.primary_failure_event_id,
            terminal_outcome="handoff", delivery_disposition="suppressed",
        )
        return TurnResult(
            trace_id=state.trace_id, text=None,
            handoff=HandoffEvent(required=True, reason_code=reason, safe_message=safe_message),
            assurance=self._assurance(),
        )

    def _assurance(self) -> AssuranceMetadata:
        return AssuranceMetadata(
            mode="reduced_assurance" if self.assurance_mode == "bootstrap" else "dual_judge",
            judges=("response_judge",) if self.assurance_mode == "bootstrap" else ("response_judge", "response_judge_zh_verifier"),
        )

    async def _finalize(self, state: TurnState) -> TurnResult:
        await self.traces.finish_trace(
            state.trace_id, "succeeded", tenant_id=state.context.tenant_id,
            terminal_outcome="reply", delivery_disposition="deliver",
        )
        await self.conversations.append_turn(
            tenant_id=state.context.tenant_id, customer_id=state.context.customer_id,
            session_id=state.request.session_id, trace_id=state.trace_id,
            customer_text=state.request.message, assistant_text=state.draft.text,
            citations=state.draft.citations,
        )
        return TurnResult(
            trace_id=state.trace_id, text=state.draft.text,
            citations=state.draft.citations, assurance=self._assurance(),
        )
