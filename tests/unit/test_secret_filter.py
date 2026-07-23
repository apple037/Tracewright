from __future__ import annotations

import json
import logging
import sys

from agent_flow.api.sanitization import sanitize_trace_value
from agent_flow.logging import StructuredJsonFormatter, configure_json_stdout


def test_secret_filter_is_recursive_copy_on_filter_and_preserves_safe_decisions():
    source = {
        "Authorization": "Bearer secret",
        "nested": {
            "apiKey": "secret",
            "API_keys": ["secret"],
            "tokens": ["secret"],
            "credentials": {"user": "secret"},
            "privateKeys": ["secret"],
            "webhook_signature": "secret",
            "webhookSecrets": ["secret"],
            "database-url": "postgresql://secret",
            "connectionURLs": ["postgresql://secret"],
            "decision_summary": "Use verified order state.",
            "reasonCodes": ["ORDER_VERIFIED"],
        },
    }

    filtered = sanitize_trace_value(source)

    assert filtered == {
        "nested": {
            "decision_summary": "Use verified order state.",
            "reasonCodes": ["ORDER_VERIFIED"],
        }
    }
    assert source["Authorization"] == "Bearer secret"
    assert source["nested"]["apiKey"] == "secret"


def test_structured_log_is_bounded_deterministic_and_does_not_mutate_input():
    source = {
        "trace_id": "trace-1",
        "span_id": "span-1",
        "node": "response_judge",
        "component": "model",
        "operation": "validate",
        "decision_summary": "grounded",
        "reason_codes": ["GROUNDED"],
        "retry": {"attempt": 2},
        "tool": {"name": "order.lookup"},
        "model": {"role": "response_judge"},
        "error_location": {"node": "response_judge", "operation": "parse"},
        "password": "do-not-log",
        "long": "x" * 100,
        "many": list(range(10)),
        "deep": {"a": {"b": {"c": "hidden"}}},
    }
    before = json.loads(json.dumps(source))
    record = logging.LogRecord(
        name="agent_flow",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="turn decision",
        args=(),
        exc_info=None,
    )
    record.event = "turn.validated"
    record.fields = source
    formatter = StructuredJsonFormatter(
        max_depth=3, max_items=3, max_string_length=16
    )

    first = formatter.format(record)
    second = formatter.format(record)
    payload = json.loads(first)

    assert first == second
    assert payload["level"] == "warning"
    assert payload["event"] == "turn.validated"
    assert payload["trace_id"] == "trace-1"
    assert payload["decision_summary"] == "grounded"
    assert payload["reason_codes"] == ["GROUNDED"]
    assert "password" not in first
    assert "do-not-log" not in first
    assert len(payload["long"]) <= 16
    assert len(payload["many"]) <= 3
    assert payload["deep"]["a"]["b"] == "[max-depth]"
    assert source == before


def test_structured_log_never_serializes_exception_or_hidden_reasoning():
    record = logging.LogRecord(
        name="agent_flow",
        level=logging.ERROR,
        pathname=__file__,
        lineno=10,
        msg="failed",
        args=(),
        exc_info=None,
    )
    record.event = "turn.failed"
    record.fields = {
        "exception": RuntimeError("password=secret"),
        "nativeReasoning": "hidden chain of thought",
        "thinking_log": "hidden",
        "error_code": "MODEL_RESPONSE_INVALID",
        "error_location": {"node": "response_generator", "operation": "parse"},
    }

    output = StructuredJsonFormatter().format(record)
    payload = json.loads(output)

    assert "secret" not in output
    assert "hidden chain of thought" not in output
    assert "thinking_log" not in output
    assert payload["error_code"] == "MODEL_RESPONSE_INVALID"
    assert payload["error_location"] == {
        "node": "response_generator",
        "operation": "parse",
    }


def test_structured_log_bounds_before_traversing_adversarial_input():
    deep: dict[str, object] = {"leaf": "safe"}
    for index in range(5000):
        deep = {f"level_{index}": deep}
    source = {
        "deep": deep,
        "wide": {f"item_{index:04}": index for index in range(1000)},
    }
    record = logging.LogRecord(
        name="agent_flow",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="bounded",
        args=(),
        exc_info=None,
    )
    record.event = "bounded"
    record.fields = source

    payload = json.loads(
        StructuredJsonFormatter(max_depth=4, max_items=5).format(record)
    )

    assert len(payload["wide"]) == 5
    assert "[max-depth]" in json.dumps(payload["deep"])
    assert "leaf" not in json.dumps(payload["deep"])
    assert len(deep) == 1


def test_structured_log_drops_raw_conversation_fields():
    record = logging.LogRecord(
        name="agent_flow",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="raw input must not be emitted",
        args=(),
        exc_info=None,
    )
    record.event = "turn.completed"
    record.fields = {
        "message": "customer raw message",
        "messages": [{"content": "conversation history"}],
        "customer_text": "customer raw text",
        "assistantText": "assistant raw text",
        "decision_summary": "verified response",
        "reason_codes": ["VERIFIED"],
    }

    output = StructuredJsonFormatter().format(record)
    payload = json.loads(output)

    assert "customer raw" not in output
    assert "conversation history" not in output
    assert "assistant raw" not in output
    assert payload["decision_summary"] == "verified response"
    assert payload["reason_codes"] == ["VERIFIED"]


def test_json_logging_handler_writes_to_stdout():
    logger = logging.Logger("agent_flow.test")

    handler = configure_json_stdout(logger)

    assert handler.stream is sys.stdout
