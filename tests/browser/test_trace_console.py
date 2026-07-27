import json

from playwright.sync_api import expect


def _fulfill(route, payload, status=200):
    route.fulfill(
        status=status, content_type="application/json", body=json.dumps(payload)
    )


def _get_route(page, pattern, payload, status=200):
    page.route(pattern, lambda route: _fulfill(route, payload, status))


def authenticate(page):
    page.get_by_label("Demo token").fill("demo-token")
    page.get_by_role("button", name="Enter").click()


TOOL_TIMEOUT_DETAIL = {
    "id": "trace-tt",
    "trace_id": "trace-tt",
    "status": "failed",
    "spans": [
        {"id": "s1", "node": "context_loader", "name": "context_loader",
         "status": "completed", "attempt": 1, "error_code": None},
        {"id": "s2", "node": "evidence_collector", "name": "evidence_collector",
         "status": "failed", "attempt": 1, "error_code": "TOOL_TIMEOUT"},
    ],
    "events": [],
    "issue_summary": {
        "error_code": "TOOL_TIMEOUT", "failed_node": "evidence_collector",
        "component": "order_api", "operation": "order.lookup",
    },
}


SESSIONS = {
    "sessions": [
        {"session_id": "line-U123", "turn_count": 2,
         "last_activity": "2026-07-23T00:00:00Z", "last_message": "where is my order"},
        {"session_id": "line-U999", "turn_count": 1,
         "last_activity": "2026-07-22T00:00:00Z", "last_message": "hello"},
    ]
}


def install_api_fixture(page, *, scenario, sessions=None):
    # The console lists chats on every refresh; without a route the real server
    # answers 401 and the page logs an error the teardown treats as a failure.
    _get_route(page, "**/api/v1/sessions", sessions or {"sessions": []})
    _get_route(page, "**/api/v1/sessions/*/messages", {"session_id": "s", "messages": []})
    if scenario == "tool-timeout":
        _get_route(page, "**/api/v1/traces", {
            "items": [{
                "trace_id": "trace-tt", "status": "failed", "channel": "console",
                "external_message_id": "tool timeout trace",
                "terminal_outcome": "handoff", "retry_of_trace_id": None,
                "delivery_disposition": "suppressed",
                "created_at": "2026-07-23T00:00:00Z",
            }],
            "next_cursor": None,
        })
        _get_route(page, "**/api/v1/traces/trace-tt", TOOL_TIMEOUT_DETAIL)
        _get_route(
            page, "**/api/v1/traces/*/events*",
            {"trace_id": "trace-tt", "events": []},
        )
    elif scenario == "empty":
        _get_route(page, "**/api/v1/traces", {"items": [], "next_cursor": None})


def test_failed_node_opens_and_shows_exact_location(page, console_url):
    install_api_fixture(page, scenario="tool-timeout")
    page.goto(console_url)
    authenticate(page)
    page.get_by_role("button", name="tool timeout trace").click()
    failed = page.locator('[data-node="evidence_collector"][data-status="failed"]')
    expect(failed).to_have_attribute("aria-expanded", "true")
    expect(failed.locator("[data-error-code]")).to_have_text("TOOL_TIMEOUT")
    expect(failed.locator("[data-component]")).to_have_text("order_api")
    expect(failed.locator("[data-operation]")).to_have_text("order.lookup")


def test_empty_trace_list_renders_placeholder(page, console_url):
    install_api_fixture(page, scenario="empty")
    page.goto(console_url)
    authenticate(page)
    expect(page.get_by_text("No traces yet.")).to_be_visible()


def test_completed_node_toggles_with_keyboard(page, console_url):
    install_api_fixture(page, scenario="tool-timeout")
    page.goto(console_url)
    authenticate(page)
    completed = page.locator('[data-node="context_loader"][data-status="completed"]')
    expect(completed).to_have_attribute("aria-expanded", "false")
    completed.focus()
    page.keyboard.press("Enter")
    expect(completed).to_have_attribute("aria-expanded", "true")


def _submission_status_sequence(page, submission_id, trace_id, statuses, terminal):
    calls = {"n": 0}

    def handler(route):
        index = min(calls["n"], len(statuses) - 1)
        calls["n"] += 1
        status = statuses[index]
        body = {
            "submission_id": submission_id, "trace_id": trace_id, "status": status,
            "citations": [], "handoff": None, "text": None,
        }
        if status == "completed":
            body.update(terminal)
        _fulfill(route, body)

    page.route(f"**/api/v1/submissions/{submission_id}", handler)


def install_submission_sequence(page, *, statuses, handoff=None, text="訂單正在配送中",
                                forbidden_partial_text=None):
    page.route(
        "**/api/v1/submissions",
        lambda route: _fulfill(
            route,
            {"submission_id": "sub-1", "trace_id": "trace-001", "status": "queued"},
            status=202,
        ) if route.request.method == "POST" else route.continue_(),
    )
    terminal = {"text": None if handoff else text, "handoff": handoff}
    _submission_status_sequence(page, "sub-1", "trace-001", statuses, terminal)


def submit_chat(page, text):
    page.get_by_label("Type a customer message…").fill(text)
    page.get_by_role("button", name="Send message").click()


def test_chat_shows_customer_bubble_and_safe_terminal_reply(page, chat_url):
    install_submission_sequence(page, statuses=["queued", "running", "completed"])
    page.goto(chat_url)
    authenticate(page)
    submit_chat(page, "我的訂單在哪裡？")
    expect(page.locator(".chat-customer .chat-bubble")).to_have_text("我的訂單在哪裡？")
    expect(page.get_by_text("Running")).to_be_visible()
    expect(page.locator(".chat-agent .chat-bubble")).to_have_text("訂單正在配送中")


def test_demo_page_chat_selects_trace_and_shows_reply(page, console_url):
    install_api_fixture(page, scenario="none")
    _get_route(page, "**/api/v1/traces", {
        "items": [{
            "trace_id": "trace-001", "status": "succeeded", "channel": "console",
            "external_message_id": None, "terminal_outcome": "reply",
            "retry_of_trace_id": None, "delivery_disposition": "deliver",
            "created_at": "2026-07-23T00:00:00Z",
        }],
        "next_cursor": None,
    })
    _get_route(page, "**/api/v1/traces/trace-001", {
        "id": "trace-001", "trace_id": "trace-001", "status": "succeeded",
        "spans": [{"id": "s1", "node": "response_generator", "name": "response_generator",
                   "status": "completed", "attempt": 1, "error_code": None}],
        "events": [], "issue_summary": None,
    })
    _get_route(page, "**/api/v1/traces/*/events*", {"trace_id": "trace-001", "events": []})
    install_submission_sequence(page, statuses=["queued", "running", "completed"])
    page.goto(console_url)
    authenticate(page)
    submit_chat(page, "我的訂單在哪裡？")
    expect(page.locator(".chat-agent .chat-bubble")).to_have_text("訂單正在配送中")
    expect(page.locator("[data-selected-trace]")).to_have_text("trace-001")


def test_chat_renders_handoff_safe_message_without_partial_draft(page, chat_url):
    install_submission_sequence(
        page,
        statuses=["queued", "running", "completed"],
        handoff={"required": True, "reason_code": "HIGH_RISK", "safe_message": "已轉交人工協助"},
        forbidden_partial_text="unvalidated draft",
    )
    page.goto(chat_url)
    authenticate(page)
    submit_chat(page, "我需要人工協助")
    expect(page.get_by_text("已轉交人工協助")).to_be_visible()
    expect(page.get_by_text("unvalidated draft")).to_have_count(0)


def _retry_detail(trace_id, status="succeeded"):
    return {
        "id": trace_id, "trace_id": trace_id, "status": status,
        "spans": [{"id": "s1", "node": "response_generator", "name": "response_generator",
                   "status": "completed", "attempt": 1, "error_code": None}],
        "events": [], "issue_summary": None,
    }


def install_retry_fixture(page, *, source_trace, retry_trace):
    install_api_fixture(page, scenario="none")
    retried = {"done": False}

    def list_handler(route):
        items = [{
            "trace_id": source_trace, "status": "succeeded", "channel": "console",
            "external_message_id": None, "terminal_outcome": "reply",
            "retry_of_trace_id": None, "delivery_disposition": "deliver",
            "created_at": "2026-07-23T00:00:00Z",
        }]
        if retried["done"]:
            items.insert(0, {
                "trace_id": retry_trace, "status": "queued", "channel": "console",
                "external_message_id": None, "terminal_outcome": None,
                "retry_of_trace_id": source_trace, "delivery_disposition": "review_required",
                "created_at": "2026-07-23T00:01:00Z",
            })
        _fulfill(route, {"items": items, "next_cursor": None})

    page.route("**/api/v1/traces", list_handler)
    _get_route(page, f"**/api/v1/traces/{source_trace}", _retry_detail(source_trace))
    _get_route(page, f"**/api/v1/traces/{retry_trace}", _retry_detail(retry_trace, "queued"))
    _get_route(page, "**/api/v1/traces/*/events*", {"trace_id": source_trace, "events": []})

    def retry_handler(route):
        retried["done"] = True
        _fulfill(route, {
            "trace_id": retry_trace, "retry_of_trace_id": source_trace,
            "delivery_disposition": "review_required",
        }, status=202)

    page.route(f"**/api/v1/traces/{source_trace}/retry", retry_handler)


def test_manual_retry_preserves_source_and_selects_new_attempt(page, console_url):
    install_retry_fixture(page, source_trace="trace-001", retry_trace="trace-002")
    page.goto(console_url)
    authenticate(page)
    page.get_by_role("button", name="Retry trace").click()
    page.get_by_label("Retry reason").fill("operator requested retry")
    page.get_by_role("button", name="Confirm retry").click()
    expect(page.locator('[data-trace-id="trace-001"]')).to_be_visible()
    expect(page.locator('[data-trace-id="trace-002"]')).to_be_visible()
    expect(page.locator("[data-selected-trace]")).to_have_text("trace-002")


def test_chats_are_listed_and_selecting_one_loads_its_transcript(page, console_url):
    # Messages are grouped by chat id — the id a channel like LINE supplies.
    install_api_fixture(page, scenario="empty", sessions=SESSIONS)
    page.route(
        "**/api/v1/sessions/line-U999/messages",
        lambda route: _fulfill(route, {
            "session_id": "line-U999",
            "messages": [
                {"role": "customer", "text": "hello", "created_at": "2026-07-22T00:00:00Z"},
                {"role": "agent", "text": "hi there", "citations": [],
                 "created_at": "2026-07-22T00:00:01Z"},
            ],
        }),
    )
    page.goto(console_url)
    authenticate(page)

    expect(page.locator('[data-session-id="line-U123"]')).to_be_visible()
    page.locator('[data-session-id="line-U999"]').click()

    expect(page.locator(".chat-customer .chat-bubble")).to_have_text("hello")
    expect(page.locator(".chat-agent .chat-bubble")).to_have_text("hi there")
    expect(page.locator('[data-session-id="line-U999"]')).to_have_attribute(
        "aria-pressed", "true"
    )


def test_refresh_clears_token_and_returns_to_token_dialog(page, console_url):
    install_api_fixture(page, scenario="empty")
    page.goto(console_url)
    authenticate(page)
    page.reload()
    expect(page.get_by_role("dialog", name="Demo authentication")).to_be_visible()
