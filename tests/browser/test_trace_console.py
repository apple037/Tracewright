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
    # Logging in restores the chat, loads the traces and then puts the cursor in
    # the composer — several awaits after the click returns. A test that starts
    # driving the page before that lands has its focus stolen mid-test, which is
    # what made the keyboard tests flaky.
    expect(page.locator("#chat-input")).to_be_focused()


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


CONFIG = {
    "prompts": [], "personas": [],
    "models": {"roles": {}, "profiles": [], "disabled_roles": [],
               "config_path": "config/models.local.yaml"},
    "choices": {"response_modes": [], "conversation_modes": []},
    "settings": {"history_turns": 8},
}

KNOWLEDGE = {
    "sources": [{
        "source": "groupbuy", "type": "fixture", "enabled": True, "editable": True,
        "path": "config/demo/groupbuy.json",
        "documents": [
            {"source_id": "groupbuy:coffee-2026-08", "version": "v1",
             "content": "八月咖啡團購清單"},
        ],
    }],
}


MEMORY = {
    "session_id": "console-1",
    "history_turns": 2,
    "exchanges": {"stored": 5, "in_window": 2},
    "messages": [
        {"role": "customer", "text": "where is my order order-1?"},
        {"role": "assistant", "text": "It is in transit."},
        {"role": "customer", "text": "is it still on the way?"},
        {"role": "assistant", "text": "Yes, order-1 is still in transit."},
    ],
}


def _open_tune(page, console_url, on_put=None, on_memory=None):
    install_api_fixture(page, scenario="empty")
    _get_route(page, "**/api/v1/config", CONFIG)
    _get_route(page, "**/api/v1/config/knowledge", KNOWLEDGE)
    page.route("**/api/v1/sessions/*/memory", on_memory or (
        lambda route: _fulfill(route, MEMORY)
    ))
    if on_put is not None:
        page.route("**/api/v1/config/knowledge/*/*", on_put)
    # Memory belongs to a chat, and the panel reads whichever one is open. A
    # fresh browser has none, so seed the id the console persists.
    page.add_init_script(
        "window.localStorage.setItem('tracewright.session', 'console-1')"
    )
    page.goto(console_url)
    authenticate(page)
    page.get_by_role("button", name="Tune").click()


def test_tune_lists_the_documents_the_assistant_may_cite(page, console_url):
    _open_tune(page, console_url)

    expect(page.get_by_text("groupbuy:coffee-2026-08")).to_be_visible()
    expect(page.get_by_label("groupbuy:coffee-2026-08")).to_have_value("八月咖啡團購清單")
    # The panel must say what editing this actually means before anyone edits it.
    expect(page.get_by_text("stated to customers as fact", exact=False)).to_be_visible()


def test_adding_a_document_sends_its_id_and_content(page, console_url):
    sent = {}

    def capture(route):
        sent["url"] = route.request.url
        sent["body"] = route.request.post_data
        _fulfill(route, {"source": "groupbuy", "source_id": "groupbuy:tea",
                         "replaced": False})

    _open_tune(page, console_url, on_put=capture)

    page.get_by_label("Document id, e.g. groupbuy:tea-2026-09").fill("groupbuy:tea")
    page.get_by_label("Add a document").fill("九月茶葉團購：烏龍 NT$500。")
    page.get_by_role("button", name="Add", exact=True).click()

    expect(page.get_by_text("groupbuy:coffee-2026-08")).to_be_visible()
    assert sent["url"].endswith("/api/v1/config/knowledge/groupbuy/groupbuy%3Atea")
    assert json.loads(sent["body"])["content"] == "九月茶葉團購：烏龍 NT$500。"


def test_tune_shows_the_memory_window_and_what_falls_outside_it(page, console_url):
    _open_tune(page, console_url)

    # The two counts side by side are the point: five stored, two reachable.
    expect(
        page.get_by_text("5 exchanges stored, 2 of them in the window", exact=False)
    ).to_be_visible()
    expect(page.get_by_text("is it still on the way?")).to_be_visible()


def test_forgetting_a_chat_asks_first_and_then_deletes(page, console_url):
    calls = []

    def memory(route):
        calls.append(route.request.method)
        if route.request.method == "DELETE":
            _fulfill(route, {"session_id": "console-1", "exchanges_forgotten": 5})
        else:
            _fulfill(route, MEMORY if len(calls) == 1 else {
                "session_id": "console-1", "history_turns": 2,
                "exchanges": {"stored": 0, "in_window": 0}, "messages": [],
            })

    _open_tune(page, console_url, on_memory=memory)
    # Irreversible, so a dismissed confirm must not delete anything.
    page.once("dialog", lambda dialog: dialog.dismiss())
    page.get_by_role("button", name="Forget this chat").click()
    expect(page.get_by_text("is it still on the way?")).to_be_visible()
    assert "DELETE" not in calls

    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Forget this chat").click()
    expect(page.get_by_text("Nothing remembered in this chat yet.")).to_be_visible()
    assert "DELETE" in calls


RUNNING_DETAIL = {
    "id": "trace-live",
    "trace_id": "trace-live",
    "status": "running",
    "spans": [
        {"id": "s1", "node": "context_loader", "name": "context_loader",
         "status": "completed", "attempt": 1, "error_code": None},
    ],
    "events": [],
    "issue_summary": None,
}


def test_focus_survives_the_rerender_that_runs_while_a_turn_is_live(page, console_url):
    # The workspace rebuilds whenever new events arrive — once a second while a
    # turn is still running. Losing the focused element there meant an
    # operator's Enter landed on nothing.
    calls = {"n": 0}

    def events(route):
        calls["n"] += 1
        # Nothing on the first poll, then an event, which is what triggers the
        # re-render this test is about.
        payload = {"trace_id": "trace-live", "events": []} if calls["n"] < 2 else {
            "trace_id": "trace-live",
            "events": [{
                "id": "e1", "trace_id": "trace-live", "span_id": "s1",
                "sequence": 1, "event_type": "node_completed",
                "created_at": "2026-07-23T00:00:01Z", "payload": {},
            }],
        }
        _fulfill(route, payload)

    _get_route(page, "**/api/v1/sessions", {"sessions": []})
    _get_route(page, "**/api/v1/sessions/*/messages", {"session_id": "s", "messages": []})
    _get_route(page, "**/api/v1/traces", {
        "items": [{
            "trace_id": "trace-live", "status": "running", "channel": "console",
            "external_message_id": "live trace", "terminal_outcome": None,
            "retry_of_trace_id": None, "delivery_disposition": None,
            "created_at": "2026-07-23T00:00:00Z",
        }],
        "next_cursor": None,
    })
    _get_route(page, "**/api/v1/traces/trace-live", RUNNING_DETAIL)
    # A running trace polls every second, so this test never waits on the five
    # second terminal cadence — that budget was the flaky part, not the console.
    page.route("**/api/v1/traces/*/events*", events)

    page.goto(console_url)
    authenticate(page)
    completed = page.locator('[data-node="context_loader"][data-status="completed"]')
    completed.focus()

    for _ in range(40):
        if calls["n"] >= 2:
            break
        page.wait_for_timeout(250)
    assert calls["n"] >= 2, "the console never polled for events a second time"
    page.wait_for_timeout(250)

    expect(completed).to_be_focused()
    page.keyboard.press("Enter")
    expect(completed).to_have_attribute("aria-expanded", "true")
