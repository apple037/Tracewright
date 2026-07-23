from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_flow.api.dependencies import principal, require_scope, services
from agent_flow.api.sanitization import sanitize_trace_value
from agent_flow.auth import AuthenticatedPrincipal, bind_customer_context
from agent_flow.errors import AgentError


router = APIRouter(prefix="/api/v1")


class ManualRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason must not be blank")
        return value


def _trace_customer_scope(authenticated: AuthenticatedPrincipal) -> str | None:
    require_scope(
        authenticated, "trace:internal", "trace:admin", "trace:retry"
    )
    return authenticated.customer_id


def _trace_access(authenticated: AuthenticatedPrincipal, trace) -> Any:
    try:
        return bind_customer_context(authenticated, trace.customer_id, trace.customer_id)
    except AgentError as error:
        raise HTTPException(status_code=404, detail="trace not found") from error


async def _scoped_trace(request: Request, trace_id: UUID, authenticated):
    customer_id = _trace_customer_scope(authenticated)
    repository = services(request).traces
    if repository is None:
        raise HTTPException(status_code=503, detail="trace repository unavailable")
    trace = await repository.get_trace(
        trace_id, tenant_id=authenticated.tenant_id, customer_id=customer_id
    )
    if trace is None:
        raise HTTPException(status_code=404, detail="trace not found")
    _trace_access(authenticated, trace)
    return trace


def _public_values(value) -> dict[str, Any]:
    raw = vars(value).copy()
    raw.pop("issue_summary", None)
    return jsonable_encoder(sanitize_trace_value(raw))


@router.get("/traces/{trace_id}")
async def get_trace(
    trace_id: UUID,
    request: Request,
    authenticated: AuthenticatedPrincipal = Depends(principal),
):
    trace = await _scoped_trace(request, trace_id, authenticated)
    payload = _public_values(trace)
    payload["spans"] = [_public_values(item) for item in trace.spans]
    payload["events"] = [_public_values(item) for item in trace.events]
    issue = getattr(trace, "issue_summary", None)
    payload["issue_summary"] = _public_values(issue) if issue is not None else None
    return payload


@router.get("/traces/{trace_id}/events")
async def incremental_events(
    trace_id: UUID,
    request: Request,
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    authenticated: AuthenticatedPrincipal = Depends(principal),
):
    trace = await _scoped_trace(request, trace_id, authenticated)
    events = await services(request).traces.events_after(
        trace.id, tenant_id=authenticated.tenant_id,
        customer_id=_trace_customer_scope(authenticated),
        after_sequence=after_sequence,
    )
    return {"trace_id": trace.id, "events": [_public_values(item) for item in events]}


def _artifact_refs(trace) -> dict[str, Any] | None:
    event = next(
        (
            item for item in trace.events
            if item.node == "context_loader"
            and item.kind in {"started", "completed"}
            and item.metadata
        ),
        None,
    )
    return dict(event.metadata) if event is not None else None


def _runtime_artifact_refs(pipeline) -> dict[str, Any]:
    artifacts = pipeline.artifacts
    return {
        "strategy_prompt_ref": artifacts.strategy_prompt.ref.model_dump(mode="json"),
        "response_prompt_ref": artifacts.response_prompt.ref.model_dump(mode="json"),
        "persona_refs": [item.ref.model_dump(mode="json") for item in artifacts.personas],
    }


@router.post("/traces/{trace_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def manual_retry(
    trace_id: UUID,
    payload: ManualRetryRequest,
    request: Request,
    authenticated: AuthenticatedPrincipal = Depends(principal),
):
    require_scope(authenticated, "trace:retry")
    trace = await _scoped_trace(request, trace_id, authenticated)
    if trace.status == "running" or trace.finished_at is None:
        raise HTTPException(status_code=409, detail="active traces cannot be retried")
    app_services = services(request)
    retry_count = await app_services.traces.count_retries(
        trace.root_trace_id, tenant_id=trace.tenant_id, customer_id=trace.customer_id
    )
    if retry_count >= 3:
        raise HTTPException(status_code=409, detail="retry lineage limit reached")
    try:
        captured = await app_services.conversations.get_retry_turn_input(
            trace.id, tenant_id=trace.tenant_id, customer_id=trace.customer_id
        )
    except ValueError as error:
        message = str(error)
        code = 409 if "expired" in message else 404
        raise HTTPException(status_code=code, detail="retry input unavailable") from error
    try:
        snapshot = await app_services.conversations.get_retry_snapshot(
            trace.id, tenant_id=trace.tenant_id, customer_id=trace.customer_id
        )
    except ValueError as error:
        raise HTTPException(status_code=404, detail="retry snapshot unavailable") from error
    clock = getattr(app_services.pipeline, "clock", None)
    now = clock.now() if clock is not None else datetime.now(timezone.utc)
    if captured.expires_at <= now:
        raise HTTPException(status_code=409, detail="retry input has expired")
    if snapshot.captured_at + timedelta(days=30) <= now:
        raise HTTPException(status_code=409, detail="retry snapshot has expired")
    current_refs = _runtime_artifact_refs(app_services.pipeline)
    if _artifact_refs(trace) != current_refs:
        raise HTTPException(status_code=409, detail="artifact version is unresolved")
    context = _trace_access(authenticated, trace)
    try:
        result = await app_services.pipeline.run(
            context,
            captured.request,
            retry_of=trace.id,
            retry_initiator=authenticated.subject_id,
            retry_reason=payload.reason,
            delivery_disposition="review_required",
            suppress_handoff=True,
            max_retry_count=3,
        )
    except ValueError as error:
        if "retry lineage limit reached" not in str(error):
            raise
        raise HTTPException(status_code=409, detail="retry lineage limit reached") from error
    return {
        "trace_id": result.trace_id,
        "retry_of_trace_id": trace.id,
        "delivery_disposition": "review_required",
    }
