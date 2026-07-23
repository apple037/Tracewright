from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field

from agent_flow.api.dependencies import principal, require_scope, services
from agent_flow.auth import AuthenticatedPrincipal, bind_customer_context
from agent_flow.contracts import AssuranceMetadata, InboundMessage, TurnResult
from agent_flow.errors import AgentError


router = APIRouter(prefix="/api/v1")


class SubmitTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_id: str | None = Field(default=None, min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)
    message: str = Field(min_length=1, max_length=20_000)
    case_id: str | None = None


def _assurance(pipeline) -> AssuranceMetadata:
    if pipeline is not None and hasattr(pipeline, "_assurance"):
        return pipeline._assurance()
    return AssuranceMetadata(mode="reduced_assurance", judges=("response_judge",))


@router.post("/turns")
async def submit_turn(
    payload: SubmitTurn,
    request: Request,
    response: Response,
    authenticated: AuthenticatedPrincipal = Depends(principal),
):
    require_scope(authenticated, "turn:write")
    try:
        context = bind_customer_context(authenticated, payload.customer_id, None)
    except AgentError as error:
        raise HTTPException(status_code=403, detail=error.public_message) from error
    app_services = services(request)
    if app_services.inbound is None:
        raise HTTPException(status_code=503, detail="submissions unavailable")
    generated = uuid4().hex
    message = InboundMessage(
        channel="console",
        external_message_id=generated,
        session_id=payload.session_id,
        text=payload.message,
        case_id=payload.case_id,
        idempotency_key=generated,
    )
    receipt = await app_services.inbound.submit(context, message)
    result = await app_services.inbound.wait(
        receipt.submission_id, context,
        timeout=app_services.legacy_turn_timeout_seconds,
    )
    if result is not None and result.status == "completed":
        return TurnResult(
            trace_id=result.trace_id, text=result.text,
            citations=result.citations, handoff=result.handoff,
            assurance=_assurance(app_services.pipeline),
        )
    response.status_code = 202
    response.headers["Location"] = f"/api/v1/submissions/{receipt.submission_id}"
    return jsonable_encoder(receipt)
