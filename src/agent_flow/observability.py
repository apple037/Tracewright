from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from agent_flow.api.sanitization import sanitize_trace_value
from agent_flow.contracts import (
    DialogueClassification,
    ResponseDraft,
    RiskDecision,
    StrategyProposal,
    ValidatedEvidence,
    ValidationResult,
)


JSONValue = Any


@dataclass(frozen=True)
class NodeTraceContext:
    trace_id: UUID
    span_id: UUID
    tenant_id: str
    node: str
    attempt: int


def _fingerprint(value: Any) -> str:
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class OperationTelemetry:
    """Appends allowlisted operation events bound to the active pipeline node.

    A missing active context is a no-op so standalone probes create no rows.
    """

    def __init__(self, traces) -> None:
        self.traces = traces
        self._current: ContextVar[NodeTraceContext | None] = ContextVar(
            "agent_flow_node_trace", default=None
        )

    @asynccontextmanager
    async def bind_node(self, context: NodeTraceContext):
        token = self._current.set(context)
        try:
            yield
        finally:
            self._current.reset(token)

    async def _append(
        self, *, event_type: str, component: str, status: str, payload: dict[str, Any]
    ) -> None:
        context = self._current.get()
        if context is None:
            return
        full = {"node": context.node, "attempt": context.attempt, **payload}
        await self.traces.append_event(
            trace_id=context.trace_id,
            span_id=context.span_id,
            tenant_id=context.tenant_id,
            event_type=event_type,
            component=component,
            status=status,
            payload=sanitize_trace_value(full),
        )

    async def record_model(
        self,
        *,
        role: str,
        profile: str,
        model: str,
        duration_ms: int,
        input_tokens: int | None,
        output_tokens: int | None,
        finish_reason: str | None,
        status: str,
    ) -> None:
        await self._append(
            event_type="model_call",
            component="model",
            status=status,
            payload={
                "model_role": role,
                "model_profile": profile,
                "model": model,
                "duration_ms": duration_ms,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "finish_reason": finish_reason,
            },
        )

    async def record_rag(
        self,
        *,
        query: str,
        result_count: int,
        source_ids: list[str],
        duration_ms: int,
        status: str,
        freshness_seconds: int | None = None,
    ) -> None:
        await self._append(
            event_type="rag_call",
            component="rag",
            status=status,
            payload={
                "query_fingerprint": _fingerprint(query),
                "result_count": result_count,
                "source_ids": list(source_ids)[:20],
                "duration_ms": duration_ms,
                "freshness_seconds": freshness_seconds,
            },
        )

    async def record_tool(
        self,
        *,
        tool: str,
        arguments: dict[str, Any],
        duration_ms: int,
        status: str,
        result_source_id: str | None = None,
        freshness_seconds: int | None = None,
    ) -> None:
        await self._append(
            event_type="tool_call",
            component="tool",
            status=status,
            payload={
                "tool": tool,
                "argument_fingerprint": _fingerprint(arguments),
                "result_source_id": result_source_id,
                "duration_ms": duration_ms,
                "freshness_seconds": freshness_seconds,
            },
        )


def _bounded_codes(codes) -> list[str]:
    return [str(code)[:256] for code in tuple(codes)[:20]]


def _summarize_classifier(result: DialogueClassification) -> dict[str, JSONValue]:
    return {
        "intent": str(result.intent)[:256],
        "emotion_category": str(result.emotion.category)[:256],
        "reason_codes": _bounded_codes(result.emotion.reason_codes),
    }


def _summarize_risk(result: RiskDecision) -> dict[str, JSONValue]:
    return {
        "decision_summary": (
            "handoff required" if result.requires_handoff else "risk cleared"
        ),
        "reason_codes": _bounded_codes(
            (result.reason_code,) if result.reason_code else ()
        ),
    }


def _summarize_strategy(result: StrategyProposal) -> dict[str, JSONValue]:
    return {"reason_codes": _bounded_codes(result.reason_codes)}


def _summarize_evidence(result: ValidatedEvidence) -> dict[str, JSONValue]:
    return {
        "evidence_ids": [item.evidence_id[:256] for item in result.items][:20],
        "reason_codes": _bounded_codes(result.reason_codes),
    }


def _summarize_response(result: ResponseDraft) -> dict[str, JSONValue]:
    return {
        "citations": [str(value)[:256] for value in result.citations][:20],
        "evidence_ids": [str(value)[:256] for value in result.evidence_ids][:20],
    }


def _summarize_validation(result: ValidationResult) -> dict[str, JSONValue]:
    return {
        "decision_summary": (
            "response accepted" if result.passed else "response rejected"
        ),
        "reason_codes": _bounded_codes(result.reason_codes),
        "assurance": str(result.assurance)[:256],
    }


_NODE_SUMMARIZERS = {
    "dialogue_classifier": _summarize_classifier,
    "risk_precheck": _summarize_risk,
    "strategy_selector": _summarize_strategy,
    "evidence_validator": _summarize_evidence,
    "response_generator": _summarize_response,
    "response_repair": _summarize_response,
    "response_validator": _summarize_validation,
}


def summarize_node_result(node: str, result: Any) -> dict[str, JSONValue]:
    """Return a bounded, allowlisted decision summary for a completed node."""
    handler = _NODE_SUMMARIZERS.get(node)
    if handler is None:
        return {}
    try:
        return handler(result)
    except AttributeError:
        return {}
