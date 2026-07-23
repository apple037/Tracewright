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


def install_api_fixture(page, *, scenario):
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
