"""Reading a session's transcript back.

Without this the console cannot restore the chat after a reload — the server
remembers the conversation but nothing can ask for it. The scoping matters: a
guessed session id must never return another customer's messages.
"""

from datetime import datetime, timezone

import pytest
from starlette.testclient import TestClient

from agent_flow.auth import AuthenticatedPrincipal
from agent_flow.main import create_app
from agent_flow.repositories.conversations import _as_turns


CUSTOMER = AuthenticatedPrincipal(
    subject_id="demo", tenant_id="t1", customer_id="c1",
    scopes=frozenset({"turn:write", "trace:read"}),
)


class FakeConversations:
    def __init__(self, turns=()):
        self.turns = turns
        self.calls = []

    async def list_turns(self, *, tenant_id, customer_id, session_id, limit):
        self.calls.append((tenant_id, customer_id, session_id, limit))
        return self.turns


def _client(conversations):
    async def authenticate(token):
        return CUSTOMER if token == "good-token" else None

    return TestClient(
        create_app(conversations=conversations, authenticate=authenticate)
    )


def _turn(customer, assistant):
    return {
        "customer_text": customer,
        "assistant_text": assistant,
        "citations": ("policy:refund",),
        "created_at": datetime(2026, 7, 26, 9, 30, tzinfo=timezone.utc),
    }


def test_transcript_is_returned_oldest_first_with_roles():
    conversations = FakeConversations((_turn("where is my refund", "still processing"),))
    response = _client(conversations).get(
        "/api/v1/sessions/console-1/messages",
        headers={"Authorization": "Bearer good-token"},
    )

    assert response.status_code == 200
    messages = response.json()["messages"]
    assert [m["role"] for m in messages] == ["customer", "agent"]
    assert messages[0]["text"] == "where is my refund"
    assert messages[1]["citations"] == ["policy:refund"]
    assert messages[0]["created_at"].startswith("2026-07-26T09:30")


def test_the_query_is_scoped_to_the_authenticated_customer():
    conversations = FakeConversations()
    _client(conversations).get(
        "/api/v1/sessions/someone-elses-session/messages",
        headers={"Authorization": "Bearer good-token"},
    )
    # The session id is caller-supplied, so tenant and customer must come from
    # the token, never from the request.
    assert conversations.calls == [("t1", "c1", "someone-elses-session", 100)]


def test_an_unauthenticated_caller_gets_nothing():
    conversations = FakeConversations((_turn("hi", "hello"),))
    response = _client(conversations).get("/api/v1/sessions/console-1/messages")
    assert response.status_code == 401
    assert conversations.calls == []


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (
            [{"role": "customer", "text": "a"}, {"role": "assistant", "text": "b"}],
            [("customer", "a"), ("assistant", "b")],
        ),
        # Snapshots written before role tagging are bare strings; roles are
        # recovered by position rather than migrating the table.
        (["a", "b", "c"], [("customer", "a"), ("assistant", "b"), ("customer", "c")]),
        (None, []),
    ],
)
def test_legacy_snapshots_still_parse(stored, expected):
    assert [(t["role"], t["text"]) for t in _as_turns(stored)] == expected
