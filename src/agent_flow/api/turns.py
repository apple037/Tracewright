from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from agent_flow.api.dependencies import principal, require_scope, services
from agent_flow.auth import AuthenticatedPrincipal, bind_customer_context
from agent_flow.contracts import TurnRequest
from agent_flow.errors import AgentError


router = APIRouter(prefix="/api/v1")


class SubmitTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_id: str | None = Field(default=None, min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)
    message: str = Field(min_length=1, max_length=20_000)
    case_id: str | None = None


@router.post("/turns")
async def submit_turn(
    payload: SubmitTurn,
    request: Request,
    authenticated: AuthenticatedPrincipal = Depends(principal),
):
    require_scope(authenticated, "turn:write")
    try:
        context = bind_customer_context(authenticated, payload.customer_id, None)
    except AgentError as error:
        raise HTTPException(status_code=403, detail=error.public_message) from error
    pipeline = services(request).pipeline
    if pipeline is None:
        raise HTTPException(status_code=503, detail="pipeline unavailable")
    return await pipeline.run(
        context,
        TurnRequest(
            session_id=payload.session_id, message=payload.message, case_id=payload.case_id
        ),
    )
