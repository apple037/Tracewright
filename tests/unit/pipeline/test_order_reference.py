"""Two bugs that only showed up against a real model and real fixtures.

Both made the demo's own documented examples fail, and neither was caught by
the existing tests because the fakes were permissive.
"""

import pytest

from agent_flow.contracts import ConversationMode
from agent_flow.pipeline.evidence import extract_order_reference, plan_evidence
from agent_flow.pipeline.respond import _applicable_persona


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("where is my order order-1?", "order-1"),
        ("查詢訂單 o1", "o1"),
        ("Order #A77 please", "A77"),
        ("订单 x9，謝謝", "x9"),
        ("where is my stuff", None),
        ("", None),
    ],
)
def test_order_reference_is_read_off_the_message(message, expected):
    assert extract_order_reference(message) == expected


def test_the_planner_asks_the_tool_for_the_order_the_customer_named(classification):
    plan = plan_evidence(classification, "where is my order order-1?")
    # It used to send the literal string "current", which matches no order, so
    # every order lookup failed the turn.
    assert plan.tool_calls[0].arguments["order_id"] == "order-1"


def test_an_unnamed_order_still_produces_a_lookup(classification):
    plan = plan_evidence(classification, "where is my stuff")
    assert plan.tool_calls[0].arguments["order_id"] == "current"


def test_a_persona_is_not_applied_across_languages(classification, companion_persona):
    casual = classification.model_copy(
        update={"conversation_mode": ConversationMode.CASUAL, "intent": "greeting"}
    )
    # The shipped persona is zh-TW. Applied to an English customer it made the
    # assistant answer "good morning" in Chinese.
    assert _applicable_persona(
        casual.model_copy(update={"language": "en-US"}), companion_persona
    ) is None
    assert _applicable_persona(
        casual.model_copy(update={"language": "zh-TW"}), companion_persona
    ) is companion_persona
    # A regional variant still counts as a match.
    assert _applicable_persona(
        casual.model_copy(update={"language": "zh-CN"}), companion_persona
    ) is companion_persona


def test_a_follow_up_reuses_the_order_named_earlier(classification):
    # "is it still on the way?" names no order. Without history it fell back to
    # the literal "current" and the lookup failed.
    plan = plan_evidence(
        classification,
        "is it still on the way?",
        ["hello", "where is my order order-7?"],
    )
    assert plan.tool_calls[0].arguments["order_id"] == "order-7"


def test_the_current_message_wins_over_an_older_order(classification):
    plan = plan_evidence(
        classification, "what about order-9?", ["where is my order order-7?"]
    )
    assert plan.tool_calls[0].arguments["order_id"] == "order-9"
