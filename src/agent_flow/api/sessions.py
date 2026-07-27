from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from agent_flow.api.dependencies import principal, require_scope, services
from agent_flow.auth import AuthenticatedPrincipal, bind_customer_context
from agent_flow.errors import AgentError


router = APIRouter(prefix="/api/v1", tags=["Conversations"])


def _customer_context(authenticated: AuthenticatedPrincipal):
    try:
        return bind_customer_context(authenticated, None, None)
    except AgentError as error:
        raise HTTPException(status_code=403, detail=error.public_message) from error


@router.get("/sessions")
async def list_sessions(
    request: Request,
    authenticated: AuthenticatedPrincipal = Depends(principal),
    limit: int = Query(default=50, ge=1, le=200),
):
    """The chats this token may see, most recently active first.

    One session id is one chat — the same grouping a LINE webhook gives with
    its own chat id. Only the last customer message is previewed; assistant
    drafts and reasoning never appear here.
    """
    require_scope(authenticated, "turn:write", "trace:read", "trace:internal")
    context = _customer_context(authenticated)
    conversations = services(request).conversations
    if conversations is None:
        raise HTTPException(status_code=503, detail="conversations unavailable")
    sessions = await conversations.list_sessions(
        tenant_id=context.tenant_id,
        customer_id=context.customer_id,
        limit=limit,
    )
    return {
        "sessions": [
            {
                "session_id": session["session_id"],
                "turn_count": session["turn_count"],
                "last_activity": (
                    session["last_activity"].isoformat()
                    if session["last_activity"] is not None
                    else None
                ),
                "last_message": session["last_message"],
            }
            for session in sessions
        ]
    }


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


def _memory_repository(request: Request, authenticated: AuthenticatedPrincipal):
    # Admin only. Reading this shows one customer's words back verbatim, and
    # deleting it is not undoable.
    require_scope(authenticated, "trace:admin")
    conversations = services(request).conversations
    if conversations is None:
        raise HTTPException(status_code=503, detail="conversations unavailable")
    return conversations


@router.get("/sessions/{session_id}/memory")
async def get_session_memory(
    session_id: str,
    request: Request,
    authenticated: AuthenticatedPrincipal = Depends(principal),
):
    """What the assistant will remember of this session on its next message.

    Not the same thing as the transcript. `/messages` is everything that was
    said; this is the windowed slice the pipeline actually loads — the newest
    `HISTORY_TURNS` exchanges, oldest first, tagged with who said what. When a
    follow-up like "is it still on the way?" resolves against the wrong thing,
    or against nothing, this is the endpoint that says why.

    `stored` above `in_window` means older exchanges exist and are out of reach.
    """
    conversations = _memory_repository(request, authenticated)
    context = _customer_context(authenticated)
    turns = await conversations.list_turns(
        tenant_id=context.tenant_id,
        customer_id=context.customer_id,
        session_id=session_id,
        limit=500,
    )
    history_turns = getattr(conversations, "history_turns", 8)
    window = turns[len(turns) - history_turns:] if history_turns else ()
    return {
        "session_id": session_id,
        "history_turns": history_turns,
        "exchanges": {"stored": len(turns), "in_window": len(window)},
        "messages": [
            message
            for turn in window
            for message in (
                {"role": "customer", "text": turn["customer_text"]},
                {"role": "assistant", "text": turn["assistant_text"]},
            )
        ],
    }


@router.delete("/sessions/{session_id}/memory")
async def reset_session_memory(
    session_id: str,
    request: Request,
    authenticated: AuthenticatedPrincipal = Depends(principal),
):
    """Forget this session's exchanges. The next message starts from nothing.

    **Not undoable, and it also clears the transcript** the console replays —
    the two read the same rows. Traces are untouched: what was said and why it
    was answered that way stays on the record.

    Use it when stale context is poisoning a demo, or to prove a follow-up
    question really is resolving against memory rather than guessing.
    """
    conversations = _memory_repository(request, authenticated)
    context = _customer_context(authenticated)
    forgotten = await conversations.clear_session(
        tenant_id=context.tenant_id,
        customer_id=context.customer_id,
        session_id=session_id,
    )
    return {"session_id": session_id, "exchanges_forgotten": forgotten}
