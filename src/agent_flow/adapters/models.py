from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from agent_flow.config import ModelConfig
from agent_flow.model_registry import (
    ModelRegistry,
    ModelResponse,
    ResolvedModel,
    _authorization_header,
)
from agent_flow.retry import CapacityWait

if TYPE_CHECKING:
    from agent_flow.observability import OperationTelemetry


T = TypeVar("T", bound=BaseModel)


class CapacityGuard:
    def __init__(self, config: ModelConfig):
        self._endpoints = {
            name: asyncio.Semaphore(endpoint.max_concurrency)
            for name, endpoint in config.endpoints.items()
        }
        self._profiles = {
            name: asyncio.Semaphore(profile.max_concurrency)
            for name, profile in config.profiles.items()
        }
        self._profile_endpoints = {
            name: profile.endpoint for name, profile in config.profiles.items()
        }

    @asynccontextmanager
    async def acquire(
        self, endpoint_name: str, profile_name: str, timeout_ms: int
    ) -> AsyncIterator[CapacityWait]:
        endpoint = self._endpoints[endpoint_name]
        profile = self._profiles[profile_name]
        if self._profile_endpoints[profile_name] != endpoint_name:
            raise ValueError(
                f"profile {profile_name} is not configured for endpoint {endpoint_name}"
            )

        started = time.monotonic()
        endpoint_blocked = endpoint.locked()
        endpoint_acquired = False
        profile_acquired = False
        profile_blocked = False
        try:
            async with asyncio.timeout(timeout_ms / 1000):
                await endpoint.acquire()
                endpoint_acquired = True
                profile_blocked = profile.locked()
                await profile.acquire()
                profile_acquired = True

            if endpoint_blocked:
                limit_kind = "endpoint"
            elif profile_blocked:
                limit_kind = "profile"
            else:
                limit_kind = "none"
            yield CapacityWait(
                endpoint_name=endpoint_name,
                profile_name=profile_name,
                wait_ms=int((time.monotonic() - started) * 1000),
                wait_limit_kind=limit_kind,
            )
        finally:
            if profile_acquired:
                profile.release()
            if endpoint_acquired:
                endpoint.release()


def _request_dict(request: object) -> dict[str, Any]:
    if isinstance(request, BaseModel):
        return request.model_dump(mode="json")
    if isinstance(request, dict):
        return dict(request)
    raise TypeError("model request must be a mapping or Pydantic model")


def _chat_messages(request_data: dict[str, Any]) -> list[dict[str, Any]]:
    messages = request_data.get("messages")
    if (
        isinstance(messages, list)
        and messages
        and all(
            isinstance(item, dict) and "role" in item and "content" in item
            for item in messages
        )
    ):
        return messages
    # Pipeline requests are domain payloads, not chat transcripts; serialize
    # them into one user message so real OpenAI-compatible endpoints accept
    # them. The response schema is enforced by response_format.
    body = json.dumps(request_data, ensure_ascii=False, default=str)
    return [
        {
            "role": "user",
            "content": (
                "Handle this request payload and reply with JSON matching "
                "the required response schema:\n" + body
            ),
        }
    ]


class ModelGateway:
    def __init__(
        self,
        registry: ModelRegistry,
        *,
        timeout: float = 30.0,
        acquire_timeout_ms: int = 5000,
        telemetry: "OperationTelemetry | None" = None,
    ):
        self.registry = registry
        self.timeout = timeout
        self.acquire_timeout_ms = acquire_timeout_ms
        self.telemetry = telemetry

    async def _emit_model(
        self,
        resolved: ResolvedModel,
        response: ModelResponse | None,
        started: float,
        status: str,
    ) -> None:
        if self.telemetry is None:
            return
        await self.telemetry.record_model(
            role=resolved.role,
            profile=resolved.profile_name,
            model=resolved.model,
            duration_ms=int((time.monotonic() - started) * 1000),
            input_tokens=response.input_tokens if response else None,
            output_tokens=response.output_tokens if response else None,
            finish_reason=response.finish_reason if response else None,
            status=status,
        )

    async def complete(self, role: str, request: object) -> str:
        resolved = self.registry.resolve(role)
        if "chat" not in resolved.capabilities:
            raise RuntimeError(f"role {role} lacks required capability: chat")
        started = time.monotonic()
        try:
            if resolved.adapter == "openai_compatible":
                response = await self._openai_chat(resolved, request)
            elif resolved.adapter == "ollama_compatible":
                response = ModelResponse(
                    text=await self._ollama_chat(resolved, request),
                    finish_reason="stop",
                )
            else:
                raise RuntimeError(f"unsupported model adapter: {resolved.adapter}")
        except Exception:
            await self._emit_model(resolved, None, started, "failed")
            raise
        await self._emit_model(resolved, response, started, "completed")
        return response.text

    async def structured(
        self, role: str, request: object, response_type: type[T]
    ) -> T:
        parsed, _ = await self.structured_response(role, request, response_type)
        return parsed

    async def structured_response(
        self, role: str, request: object, response_type: type[T]
    ) -> tuple[T, ModelResponse]:
        resolved = self.registry.resolve(role)
        required = {"chat", "structured_json"}
        if not required <= resolved.capabilities:
            raise RuntimeError(
                f"role {role} lacks required capabilities: chat, structured_json"
            )
        schema = response_type.model_json_schema()
        started = time.monotonic()
        try:
            if resolved.adapter == "openai_compatible":
                response = await self._openai_chat(
                    resolved,
                    request,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": response_type.__name__,
                            "strict": True,
                            "schema": schema,
                        },
                    },
                )
            elif resolved.adapter == "ollama_compatible":
                content = await self._ollama_chat(
                    resolved, request, response_format=schema
                )
                response = ModelResponse(text=content, finish_reason="stop")
            else:
                raise RuntimeError(f"unsupported model adapter: {resolved.adapter}")
        except Exception:
            await self._emit_model(resolved, None, started, "failed")
            raise
        await self._emit_model(resolved, response, started, "completed")
        try:
            parsed = response_type.model_validate_json(response.text)
        except ValidationError:
            raise RuntimeError(
                f"structured response invalid for role {role}"
            ) from None
        return parsed, response

    async def _openai_chat(
        self,
        resolved: ResolvedModel,
        request: object,
        *,
        response_format: dict[str, object] | None = None,
    ) -> ModelResponse:
        payload: dict[str, Any] = {
            "messages": _chat_messages(_request_dict(request)),
            "model": resolved.model,
            "temperature": resolved.temperature,
            "max_tokens": resolved.max_tokens,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        if "enable_thinking" in resolved.request_options:
            payload["chat_template_kwargs"] = {
                "enable_thinking": resolved.request_options["enable_thinking"]
            }
        passthrough = {
            key: value
            for key, value in resolved.request_options.items()
            if key != "enable_thinking"
        }
        payload.update(passthrough)
        async with self.registry.capacity_guard.acquire(
            resolved.endpoint_name,
            resolved.profile_name,
            self.acquire_timeout_ms,
        ):
            async with httpx.AsyncClient(
                base_url=resolved.base_url,
                headers=_authorization_header(resolved),
                timeout=self.timeout,
            ) as client:
                response = await client.post("/chat/completions", json=payload)
                response.raise_for_status()
        body = response.json()
        try:
            choices = body["choices"]
            if not isinstance(choices, list) or not choices:
                raise RuntimeError("choices must be a non-empty list")
            choice = choices[0]
            content = choice["message"]["content"]
            if not isinstance(content, str):
                raise RuntimeError("message content must be a string")
            usage = body.get("usage", {})
            if not isinstance(usage, dict):
                raise RuntimeError("usage must be an object")
        except RuntimeError as exc:
            raise RuntimeError(
                f"model response malformed for role {resolved.role} at openai_chat: {exc}"
            ) from exc
        except (KeyError, TypeError, IndexError) as exc:
            raise RuntimeError(
                f"model response malformed for role {resolved.role} at openai_chat: {exc}"
            ) from exc
        return ModelResponse(
            text=content,
            finish_reason=choice.get("finish_reason") or "unknown",
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
        )

    async def _ollama_chat(
        self,
        resolved: ResolvedModel,
        request: object,
        *,
        response_format: dict[str, object] | None = None,
    ) -> str:
        payload = _request_dict(request)
        payload["model"] = resolved.model
        payload["stream"] = False
        payload["options"] = {
            "temperature": resolved.temperature,
            "num_predict": resolved.max_tokens,
        }
        if response_format is not None:
            payload["format"] = response_format
        if "enable_thinking" in resolved.request_options:
            payload["think"] = resolved.request_options["enable_thinking"]
        for key, value in resolved.request_options.items():
            if key != "enable_thinking":
                payload[key] = value
        async with self.registry.capacity_guard.acquire(
            resolved.endpoint_name,
            resolved.profile_name,
            self.acquire_timeout_ms,
        ):
            async with httpx.AsyncClient(
                base_url=resolved.base_url,
                headers=_authorization_header(resolved),
                timeout=self.timeout,
            ) as client:
                response = await client.post("/api/chat", json=payload)
                response.raise_for_status()
        return str(response.json()["message"]["content"])


class EmbeddingModel:
    def __init__(
        self,
        registry: ModelRegistry,
        *,
        timeout: float = 30.0,
        acquire_timeout_ms: int = 5000,
    ):
        self.registry = registry
        self.timeout = timeout
        self.acquire_timeout_ms = acquire_timeout_ms

    async def embed(self, role: str, texts: list[str]) -> list[list[float]]:
        resolved = self.registry.resolve(role)
        if "embedding" not in resolved.capabilities:
            raise RuntimeError(f"role {role} lacks required capability: embedding")
        if not texts:
            return []
        if resolved.adapter == "ollama_compatible":
            path = "/api/embed"
            payload = {"model": resolved.model, "input": texts}
            result_key = "embeddings"
        elif resolved.adapter == "openai_compatible":
            path = "/embeddings"
            payload = {"model": resolved.model, "input": texts}
            result_key = "data"
        else:
            raise RuntimeError(f"unsupported model adapter: {resolved.adapter}")
        async with self.registry.capacity_guard.acquire(
            resolved.endpoint_name,
            resolved.profile_name,
            self.acquire_timeout_ms,
        ):
            async with httpx.AsyncClient(
                base_url=resolved.base_url,
                headers=_authorization_header(resolved),
                timeout=self.timeout,
            ) as client:
                response = await client.post(path, json=payload)
                response.raise_for_status()
        body = response.json()
        if result_key == "data":
            vectors = [entry["embedding"] for entry in body[result_key]]
        else:
            vectors = body[result_key]
        if len(vectors) != len(texts):
            raise RuntimeError("embedding response count does not match input count")
        return [[float(value) for value in vector] for vector in vectors]
