from __future__ import annotations

from typing import Any


_SENSITIVE_KEYS = frozenset({
    "authorization", "apikey", "password", "secret", "token",
    "accesstoken", "refreshtoken", "cookie", "setcookie", "credential",
    "privatekey", "clientsecret", "thinking", "chainofthought", "rawprompt",
    "reasoning", "nativereasoning", "hiddenreasoning", "reasoningcontent",
})


def _normalized_key(value: object) -> str:
    return "".join(character for character in str(value).lower() if character.isalnum())


def sanitize_trace_value(value: Any) -> Any:
    """Return a recursively filtered copy without mutating stored trace payloads."""
    if isinstance(value, dict):
        return {
            key: sanitize_trace_value(item)
            for key, item in value.items()
            if _normalized_key(key) not in _SENSITIVE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_trace_value(item) for item in value]
    return value
