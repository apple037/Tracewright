from __future__ import annotations

from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from agent_flow.model_registry import (
    ModelRegistry,
    ModelResponse,
    ResolvedModel,
    _authorization_header,
)


T = TypeVar("T", bound=BaseModel)


def _request_dict(request: object) -> dict[str, Any]:
    if isinstance(request, BaseModel):
        return request.model_dump(mode="json")
    if isinstance(request, dict):
        return dict(request)
    raise TypeError("model request must be a mapping or Pydantic model")


class ModelGateway:
    def __init__(self, registry: ModelRegistry, *, timeout: float = 30.0):
        self.registry = registry
        self.timeout = timeout

    async def complete(self, role: str, request: object) -> str:
        resolved = self.registry.resolve(role)
        if "chat" not in resolved.capabilities:
            raise RuntimeError(f"role {role} lacks required capability: chat")
        if resolved.adapter == "openai_compatible":
            response = await self._openai_chat(resolved, request)
            return response.text
        if resolved.adapter == "ollama_compatible":
            return await self._ollama_chat(resolved, request)
        raise RuntimeError(f"unsupported model adapter: {resolved.adapter}")

    async def structured(
        self, role: str, request: object, response_type: type[T]
    ) -> T:
        resolved = self.registry.resolve(role)
        if "structured_json" not in resolved.capabilities:
            raise RuntimeError(
                f"role {role} lacks required capability: structured_json"
            )
        if "chat" not in resolved.capabilities:
            raise RuntimeError(f"role {role} lacks required capability: chat")
        schema = response_type.model_json_schema()
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
            content = response.text
        elif resolved.adapter == "ollama_compatible":
            content = await self._ollama_chat(resolved, request, response_format=schema)
        else:
            raise RuntimeError(f"unsupported model adapter: {resolved.adapter}")
        return response_type.model_validate_json(content)

    async def _openai_chat(
        self,
        resolved: ResolvedModel,
        request: object,
        *,
        response_format: dict[str, object] | None = None,
    ) -> ModelResponse:
        payload = _request_dict(request)
        payload.update(
            {
                "model": resolved.model,
                "temperature": resolved.temperature,
                "max_tokens": resolved.max_tokens,
            }
        )
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
        async with httpx.AsyncClient(
            base_url=resolved.base_url,
            headers=_authorization_header(resolved),
            timeout=self.timeout,
        ) as client:
            response = await client.post("/api/chat", json=payload)
            response.raise_for_status()
        return str(response.json()["message"]["content"])


class EmbeddingModel:
    def __init__(self, registry: ModelRegistry, *, timeout: float = 30.0):
        self.registry = registry
        self.timeout = timeout

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
