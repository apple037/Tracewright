from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from agent_flow.api.dependencies import principal, require_scope, services
from agent_flow.auth import AuthenticatedPrincipal, bind_customer_context
from agent_flow.errors import AgentError


router = APIRouter(prefix="/api/v1")


def _customer_context(authenticated: AuthenticatedPrincipal):
    try:
        return bind_customer_context(authenticated, None, None)
    except AgentError as error:
        raise HTTPException(status_code=403, detail=error.public_message) from error


@router.get("/sessions/{session_id}/messages")
async def get_session_messages(
    session_id: str,
    request: Request,
    authenticated: AuthenticatedPrincipal = Depends(principal),
    limit: int = Query(default=100, ge=1, le=500),
):
    """Replay a session's visible transcript.

    Only customer-facing text is returned — never drafts, never model
    reasoning. Scoping is by the authenticated tenant and customer, so a
    guessed session id cannot read someone else's conversation.
    """
    require_scope(authenticated, "turn:write", "trace:read", "trace:internal")
    context = _customer_context(authenticated)
    conversations = services(request).conversations
    if conversations is None:
        raise HTTPException(status_code=503, detail="conversations unavailable")
    turns = await conversations.list_turns(
        tenant_id=context.tenant_id,
        customer_id=context.customer_id,
        session_id=session_id,
        limit=limit,
    )
    messages: list[dict[str, object]] = []
    for turn in turns:
        created_at = turn["created_at"]
        timestamp = created_at.isoformat() if created_at is not None else None
        messages.append(
            {"role": "customer", "text": turn["customer_text"], "created_at": timestamp}
        )
        messages.append(
            {
                "role": "agent",
                "text": turn["assistant_text"],
                "citations": list(turn["citations"]),
                "created_at": timestamp,
            }
        )
    return {"session_id": session_id, "messages": messages}
