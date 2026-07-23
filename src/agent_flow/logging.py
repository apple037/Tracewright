from __future__ import annotations

from datetime import datetime, timezone
import heapq
import json
import logging
import sys
from typing import Any
from uuid import UUID

from agent_flow.api.sanitization import is_sensitive_key, sanitize_trace_value


_MAX_DEPTH = 8
_MAX_ITEMS = 50
_MAX_STRING_LENGTH = 2048


_STANDARD_FIELDS = frozenset({
    "trace_id", "span_id", "node", "component", "operation",
    "decision_summary", "reason_codes", "retry", "tool", "model",
    "error_location", "error_code",
})


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, UUID)):
        return str(value)
    return f"[{type(value).__name__}]"


class StructuredJsonFormatter(logging.Formatter):
    """Secret-safe, bounded JSON-lines formatter for operational events."""

    def __init__(
        self,
        *,
        max_depth: int = _MAX_DEPTH,
        max_items: int = _MAX_ITEMS,
        max_string_length: int = _MAX_STRING_LENGTH,
    ) -> None:
        super().__init__()
        if min(max_depth, max_items, max_string_length) < 1:
            raise ValueError("logging bounds must be positive")
        self.max_depth = max_depth
        self.max_items = max_items
        self.max_string_length = max_string_length

    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "event", record.name)
        raw_fields = getattr(record, "fields", {})
        fields = raw_fields if isinstance(raw_fields, dict) else {}
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname.lower(),
            "event": str(event)[: self.max_string_length],
        }
        standard = [
            key for key in _STANDARD_FIELDS
            if key in fields and not is_sensitive_key(key)
        ]
        extras = (
            key for key in fields
            if key not in _STANDARD_FIELDS and not is_sensitive_key(key)
        )
        selected = sorted(standard, key=str) + heapq.nsmallest(
            self.max_items, extras, key=str
        )
        for key in selected:
            payload[str(key)] = sanitize_trace_value(
                fields[key],
                max_depth=self.max_depth,
                max_items=self.max_items,
                max_string_length=self.max_string_length,
            )
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_default,
        )


def configure_json_stdout(
    logger: logging.Logger, *, level: int = logging.INFO
) -> logging.StreamHandler:
    """Attach one JSON stdout handler and return it for explicit lifecycle control."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(StructuredJsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(level)
    return handler
