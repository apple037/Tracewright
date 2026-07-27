from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status

from agent_flow.api.dependencies import principal, require_scope, services
from agent_flow.auth import AuthenticatedPrincipal, bind_customer_context
from agent_flow.contracts import InboundMessage
from agent_flow.errors import AgentError


router = APIRouter(prefix="/api/v1", tags=["Messages"])


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
    """Queue a customer message. Returns at once with a submission id.

    The pipeline runs in a background worker, so a reply takes tens of seconds.
    Poll `GET /submissions/{id}` until its status is `completed` or `failed`,
    then read the reply from `GET /sessions/{session_id}/messages`.

    `session_id` is the chat this message belongs to and is how history is
    grouped — a channel adapter passes its own chat id straight through.
    `idempotency_key` makes a retried send safe: the same key returns the
    original submission instead of asking the model twice.
    """
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
    """Whether a queued message is done, and the trace it produced."""
    require_scope(authenticated, "turn:write", "trace:read", "trace:internal")
    context = _customer_context(authenticated)
    inbound = services(request).inbound
    if inbound is None:
        raise HTTPException(status_code=503, detail="submissions unavailable")
    result = await inbound.get(submission_id, context)
    if result is None:
        raise HTTPException(status_code=404, detail="submission not found")
    return result
