"""The model must actually receive its instructions and its output schema.

Before this, every call was a single JSON-blob user message with no system
prompt at all, and the schema travelled only in `response_format` — which
Ollama's OpenAI-compatible endpoint accepts and silently ignores.
"""

import json

import pytest

from agent_flow.adapters.models import _chat_messages, _openai_response_format


def _payload():
    return {"customer_message": "where is my refund", "validated_evidence": {"items": []}}


def test_instructions_and_schema_reach_the_model_as_a_system_message():
    schema = {"type": "object", "properties": {"text": {"type": "string"}}}
    messages = _chat_messages(
        _payload(), system_prompt="Answer only from evidence.", schema=schema
    )

    assert [m["role"] for m in messages] == ["system", "user"]
    system = messages[0]["content"]
    assert "Answer only from evidence." in system
    # The schema is repeated in the prompt because grammar enforcement cannot be
    # relied on across backends.
    assert json.dumps(schema, ensure_ascii=False, sort_keys=True) in system
    assert "where is my refund" in messages[1]["content"]


def test_payload_only_call_still_sends_one_user_message():
    messages = _chat_messages(_payload())
    assert [m["role"] for m in messages] == ["user"]


def test_probe_style_transcripts_are_passed_through_untouched():
    transcript = {"messages": [{"role": "user", "content": "ping"}]}
    assert _chat_messages(transcript) == transcript["messages"]


@pytest.mark.parametrize(
    ("mode", "expected_type"),
    [("json_schema", "json_schema"), ("json_object", "json_object"), ("none", None)],
)
def test_structured_output_mode_selects_the_response_format(mode, expected_type):
    result = _openai_response_format(mode, "Draft", {"type": "object"})
    if expected_type is None:
        assert result is None
    else:
        assert result["type"] == expected_type
