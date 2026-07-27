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
ADMIN = AuthenticatedPrincipal(
    subject_id="demo-admin", tenant_id="t1", customer_id="c1",
    scopes=frozenset({"turn:write", "trace:read", "trace:admin"}),
)


class FakeConversations:
    def __init__(self, turns=(), sessions=(), history_turns=8):
        self.turns = turns
        self.sessions = sessions
        self.history_turns = history_turns
        self.calls = []
        self.session_calls = []
        self.cleared = []
        self.rebuilt = []

    async def list_turns(self, *, tenant_id, customer_id, session_id, limit):
        self.calls.append((tenant_id, customer_id, session_id, limit))
        return self.turns

    async def list_sessions(self, *, tenant_id, customer_id, limit):
        self.session_calls.append((tenant_id, customer_id, limit))
        return self.sessions

    async def clear_session(self, *, tenant_id, customer_id, session_id):
        self.cleared.append((tenant_id, customer_id, session_id))
        return len(self.turns)

    async def rebuild_session(self, *, tenant_id, customer_id, session_id):
        self.rebuilt.append((tenant_id, customer_id, session_id))
        return {"restored": len(self.turns), "rebuilt": 1}


def _client(conversations):
    async def authenticate(token):
        return {"good-token": CUSTOMER, "admin-token": ADMIN}.get(token)

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


def test_sessions_are_listed_newest_first_with_a_preview():
    conversations = FakeConversations(
        sessions=(
            {
                "session_id": "line-U123",
                "turn_count": 3,
                "last_activity": datetime(2026, 7, 26, 9, 30, tzinfo=timezone.utc),
                "last_message": "where is my refund",
            },
        )
    )
    response = _client(conversations).get(
        "/api/v1/sessions", headers={"Authorization": "Bearer good-token"}
    )

    assert response.status_code == 200
    session = response.json()["sessions"][0]
    assert session["session_id"] == "line-U123"
    assert session["turn_count"] == 3
    assert session["last_message"] == "where is my refund"
    assert session["last_activity"].startswith("2026-07-26T09:30")
    # Scope comes from the token, never the request.
    assert conversations.session_calls == [("t1", "c1", 50)]


def test_an_unauthenticated_caller_gets_nothing():
    conversations = FakeConversations((_turn("hi", "hello"),))
    response = _client(conversations).get("/api/v1/sessions/console-1/messages")
    assert response.status_code == 401
    assert conversations.calls == []


def test_memory_reports_the_window_the_pipeline_will_load_not_the_transcript():
    conversations = FakeConversations(
        tuple(_turn(f"ask {i}", f"answer {i}") for i in range(5)), history_turns=2
    )
    response = _client(conversations).get(
        "/api/v1/sessions/console-1/memory",
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 200
    body = response.json()
    # Five exchanges are stored, two are reachable. That gap is the whole point
    # of the endpoint: it is the answer to "why did it forget that".
    assert body["exchanges"] == {"stored": 5, "in_window": 2}
    assert body["history_turns"] == 2
    assert [(m["role"], m["text"]) for m in body["messages"]] == [
        ("customer", "ask 3"), ("assistant", "answer 3"),
        ("customer", "ask 4"), ("assistant", "answer 4"),
    ]


def test_resetting_memory_reports_what_it_forgot_and_stays_in_scope():
    conversations = FakeConversations((_turn("hi", "hello"),))
    response = _client(conversations).delete(
        "/api/v1/sessions/console-1/memory",
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 200
    assert response.json() == {"session_id": "console-1", "exchanges_forgotten": 1}
    assert conversations.cleared == [("t1", "c1", "console-1")]


def test_rebuilding_reports_what_was_unhidden_and_what_was_re_derived():
    # Two different recoveries, so they are counted separately: one undoes a
    # reset, the other recovers exchanges that were never stored at all.
    conversations = FakeConversations((_turn("hi", "hello"),))
    response = _client(conversations).post(
        "/api/v1/sessions/console-1/memory/rebuild",
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "session_id": "console-1", "restored": 1, "rebuilt": 1,
    }
    assert conversations.rebuilt == [("t1", "c1", "console-1")]


@pytest.mark.parametrize(
    ("method", "path"),
    [("get", "memory"), ("delete", "memory"), ("post", "memory/rebuild")],
)
def test_memory_is_closed_to_a_customer_token(method, path):
    # Reading it replays the customer's own words; the other two rewrite what
    # the assistant will be told next turn.
    conversations = FakeConversations((_turn("hi", "hello"),))
    response = getattr(_client(conversations), method)(
        f"/api/v1/sessions/console-1/{path}",
        headers={"Authorization": "Bearer good-token"},
    )

    assert response.status_code == 403
    assert conversations.calls == []
    assert conversations.cleared == []
    assert conversations.rebuilt == []


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
