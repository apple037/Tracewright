from __future__ import annotations

import heapq
import re
from typing import Any


_SAFE_STRUCTURED_KEYS = frozenset({"decisionsummary", "reasoncodes"})
_SENSITIVE_ALIASES = frozenset({
    "authorization", "apikey", "password", "secret", "token",
    "accesstoken", "refreshtoken", "cookie", "setcookie", "credential",
    "privatekey", "clientsecret", "thinking", "chainofthought", "rawprompt",
    "reasoning", "nativereasoning", "hiddenreasoning", "reasoningcontent",
    "databaseurl", "connectionurl", "webhooksecret", "webhooksignature",
    "signature", "exception", "excinfo",
    "message", "messages", "customertext", "assistanttext",
    "conversationtext", "conversationhistory",
    "apikeys", "passwords", "secrets", "tokens", "cookies", "credentials",
    "privatekeys", "databaseurls", "connectionurls", "webhooksecrets",
    "webhooksignatures", "connectionstring", "connectionstrings",
})
_SENSITIVE_SEGMENTS = frozenset({
    "authorization", "password", "secret", "token", "cookie", "credential",
    "thinking", "reasoning", "signature", "exception",
})
_SENSITIVE_PAIRS = frozenset({
    ("api", "key"), ("client", "secret"), ("private", "key"),
    ("raw", "prompt"), ("chain", "thought"),
    ("database", "url"), ("connection", "url"), ("webhook", "signature"),
    ("connection", "string"),
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


def is_sensitive_key(value: object) -> bool:
    """Return whether a structured field name is prohibited from public output."""
    return _is_sensitive_key(value)


def _mapping_key_rank(value: object) -> tuple[str, int, str, str]:
    """Prefer an exact string key when multiple keys stringify identically."""
    value_type = type(value)
    return (
        str(value),
        0 if isinstance(value, str) else 1,
        value_type.__module__,
        value_type.__qualname__,
    )


def sanitize_trace_value(
    value: Any,
    *,
    max_depth: int = 12,
    max_items: int = 100,
    max_string_length: int = 20_000,
    _depth: int = 0,
) -> Any:
    """Return one bounded, recursively filtered copy without mutating the input."""
    if min(max_depth, max_items, max_string_length) < 1:
        raise ValueError("sanitization bounds must be positive")
    if isinstance(value, BaseException):
        return "[exception-redacted]"
    if isinstance(value, dict):
        if _depth >= max_depth - 1:
            return "[max-depth]"
        safe_keys = (
            key for key in value
            if not _is_sensitive_key(key)
        )
        selected = heapq.nsmallest(max_items, safe_keys, key=_mapping_key_rank)
        sanitized: dict[str, Any] = {}
        for key in selected:
            normalized = str(key)
            if normalized in sanitized:
                continue
            sanitized[normalized] = sanitize_trace_value(
                value[key],
                max_depth=max_depth,
                max_items=max_items,
                max_string_length=max_string_length,
                _depth=_depth + 1,
            )
        return sanitized
    if isinstance(value, (list, tuple)):
        if _depth >= max_depth - 1:
            return "[max-depth]"
        return [
            sanitize_trace_value(
                item,
                max_depth=max_depth,
                max_items=max_items,
                max_string_length=max_string_length,
                _depth=_depth + 1,
            )
            for item in value[:max_items]
        ]
    if isinstance(value, set):
        if _depth >= max_depth - 1:
            return "[max-depth]"
        selected = heapq.nsmallest(max_items, value, key=lambda item: str(item))
        return [
            sanitize_trace_value(
                item,
                max_depth=max_depth,
                max_items=max_items,
                max_string_length=max_string_length,
                _depth=_depth + 1,
            )
            for item in selected
        ]
    if isinstance(value, str):
        return value[:max_string_length]
    return value
