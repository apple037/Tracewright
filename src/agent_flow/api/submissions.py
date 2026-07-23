from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status

from agent_flow.api.dependencies import principal, require_scope, services
from agent_flow.auth import AuthenticatedPrincipal, bind_customer_context
from agent_flow.contracts import InboundMessage
from agent_flow.errors import AgentError


router = APIRouter(prefix="/api/v1")


def _customer_context(authenticated: AuthenticatedPrincipal):
    try:
        return bind_customer_context(authenticated, None, None)
    except AgentError as error:
        raise HTTPException(status_code=403, detail=error.public_message) from error


@router.post("/submissions", status_code=status.HTTP_202_ACCEPTED)
async def create_submission(
    message: InboundMessage,
    request: Request,
    authenticated: AuthenticatedPrincipal = Depends(principal),
):
    require_scope(authenticated, "turn:write")
    context = _customer_context(authenticated)
    inbound = services(request).inbound
    if inbound is None:
        raise HTTPException(status_code=503, detail="submissions unavailable")
    try:
        return await inbound.submit(context, message)
    except ValueError as error:
        raise HTTPException(status_code=409, detail="submission conflict") from error


@router.get("/submissions/{submission_id}")
async def get_submission(
    submission_id: UUID,
    request: Request,
    authenticated: AuthenticatedPrincipal = Depends(principal),
):
    require_scope(authenticated, "turn:write", "trace:read", "trace:internal")
    context = _customer_context(authenticated)
    inbound = services(request).inbound
    if inbound is None:
        raise HTTPException(status_code=503, detail="submissions unavailable")
    result = await inbound.get(submission_id, context)
    if result is None:
        raise HTTPException(status_code=404, detail="submission not found")
    return result
