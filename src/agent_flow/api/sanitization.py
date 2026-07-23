from __future__ import annotations

import re
from typing import Any


_SAFE_STRUCTURED_KEYS = frozenset({"decisionsummary", "reasoncodes"})
_SENSITIVE_ALIASES = frozenset({
    "authorization", "apikey", "password", "secret", "token",
    "accesstoken", "refreshtoken", "cookie", "setcookie", "credential",
    "privatekey", "clientsecret", "thinking", "chainofthought", "rawprompt",
    "reasoning", "nativereasoning", "hiddenreasoning", "reasoningcontent",
})
_SENSITIVE_SEGMENTS = frozenset({
    "authorization", "password", "secret", "token", "cookie", "credential",
    "thinking", "reasoning",
})
_SENSITIVE_PAIRS = frozenset({
    ("api", "key"), ("client", "secret"), ("private", "key"),
    ("raw", "prompt"), ("chain", "thought"),
})


def _normalized_key(value: object) -> str:
    return "".join(character for character in str(value).lower() if character.isalnum())


def _key_segments(value: object) -> tuple[str, ...]:
    key = str(value)
    key = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", key)
    key = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    return tuple(part.lower() for part in re.findall(r"[A-Za-z0-9]+", key))


def _is_sensitive_key(value: object) -> bool:
    normalized = _normalized_key(value)
    if normalized in _SAFE_STRUCTURED_KEYS:
        return False
    if normalized in _SENSITIVE_ALIASES:
        return True
    segments = _key_segments(value)
    if not _SENSITIVE_SEGMENTS.isdisjoint(segments):
        return True
    return any(pair in _SENSITIVE_PAIRS for pair in zip(segments, segments[1:]))


def sanitize_trace_value(value: Any) -> Any:
    """Return a recursively filtered copy without mutating stored trace payloads."""
    if isinstance(value, dict):
        return {
            key: sanitize_trace_value(item)
            for key, item in value.items()
            if not _is_sensitive_key(key)
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_trace_value(item) for item in value]
    return value
