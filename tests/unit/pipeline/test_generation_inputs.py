"""What the drafting model is given.

The generator used to receive only `conversation_snapshot` — which holds turns
already persisted — plus strategy enums. On the first turn of a session it was
therefore asked to write a reply having never seen the customer's message.
"""

from datetime import datetime, timezone

import pytest

from agent_flow.contracts import ConversationSnapshot
from agent_flow.pipeline.respond import generate_response, repair_response
from agent_flow.pipeline.turn import _history_lines


def _empty_snapshot():
    return ConversationSnapshot(
        session_id="s1", messages=(), captured_at=datetime.now(timezone.utc)
    )


def _snapshot():
    return ConversationSnapshot(
        session_id="s1",
        messages=(
            {"role": "customer", "text": "訂單呢？"},
            {"role": "assistant", "text": "運送中。"},
        ),
        captured_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_generator_sees_the_current_message_on_the_first_turn(
    fake_models, transactional_strategy, validated_evidence, response_prompt
):
    await generate_response(
        fake_models, _empty_snapshot(), transactional_strategy, validated_evidence,
        response_prompt, None, customer_message="那要多久？",
    )
    request = fake_models.requests[0]
    assert request.customer_message == "那要多久？"
    assert request.conversation_snapshot.messages == ()


@pytest.mark.asyncio
async def test_repair_keeps_the_message_and_the_history(
    fake_models, verified_draft, repairable_validation, transactional_strategy,
    validated_evidence, response_prompt
):
    await repair_response(
        fake_models, verified_draft, repairable_validation, transactional_strategy,
        validated_evidence, response_prompt, None,
        customer_message="那要多久？", snapshot=_snapshot(),
    )
    request = fake_models.requests[0]
    assert request.customer_message == "那要多久？"
    assert len(request.conversation_snapshot.messages) == 2


@pytest.mark.asyncio
async def test_the_node_instructions_reach_the_model(
    fake_models, transactional_strategy, validated_evidence, response_prompt
):
    await generate_response(
        fake_models, _empty_snapshot(), transactional_strategy, validated_evidence,
        response_prompt, None, customer_message="hi",
    )
    assert fake_models.system_prompts[0] == response_prompt.system_prompt.strip()


def test_history_is_tagged_with_who_said_what():
    # The classifier takes a flat list of strings, so the speaker has to be in
    # the text — otherwise it can only guess by position.
    assert _history_lines(_snapshot()) == ("customer: 訂單呢？", "assistant: 運送中。")
