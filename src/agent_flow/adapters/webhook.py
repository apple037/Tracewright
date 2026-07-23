from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx


_RETRYABLE_HTTP = {408, 429, 500, 502, 503, 504}


@dataclass(eq=False, frozen=True)
class WebhookDeliveryError(Exception):
    error_code: str
    retryable: bool
    http_status: int | None

    def __post_init__(self) -> None:
        Exception.__init__(self, self.error_code)


@dataclass(frozen=True)
class WebhookDelivery:
    http_status: int


class HandoffWebhook:
    def __init__(
        self,
        *,
        url: str,
        secret: str,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._url = url
        self._secret = secret.encode("utf-8")
        self._client = client or httpx.AsyncClient()
        self._timeout_seconds = timeout_seconds

    @staticmethod
    def serialize(payload: dict[str, Any]) -> bytes:
        return json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    async def deliver(
        self,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        timestamp: int | None = None,
    ) -> WebhookDelivery:
        sent_at = int(time.time()) if timestamp is None else timestamp
        raw_body = self.serialize(payload)
        signed = str(sent_at).encode("ascii") + b"." + raw_body
        signature = hmac.new(self._secret, signed, hashlib.sha256).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "X-Agent-Timestamp": str(sent_at),
            "X-Agent-Signature": f"sha256={signature}",
            "Idempotency-Key": idempotency_key,
        }
        try:
            response = await self._client.post(
                self._url, content=raw_body, headers=headers, timeout=self._timeout_seconds
            )
        except httpx.TimeoutException as error:
            raise WebhookDeliveryError("WEBHOOK_TIMEOUT", True, None) from error
        except httpx.NetworkError as error:
            raise WebhookDeliveryError("WEBHOOK_CONNECTION", True, None) from error
        if 200 <= response.status_code < 300:
            return WebhookDelivery(http_status=response.status_code)
        raise WebhookDeliveryError(
            error_code=f"WEBHOOK_{response.status_code}",
            retryable=response.status_code in _RETRYABLE_HTTP,
            http_status=response.status_code,
        )

    async def close(self) -> None:
        await self._client.aclose()
