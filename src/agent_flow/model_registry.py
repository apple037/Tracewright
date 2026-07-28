from __future__ import annotations

import math
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import httpx
from pydantic import BaseModel

from agent_flow.config import ModelConfig, Settings, model_config_checksum
from agent_flow.contracts import ResponseDraft


if TYPE_CHECKING:
    from agent_flow.adapters.models import CapacityGuard


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ModelResponse(BaseModel):
    text: str
    finish_reason: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning: str | None = None


@dataclass(frozen=True)
class InventoryResult:
    role: str
    model: str
    available: bool
    digest: str | None
    max_model_len: int | None
    capabilities: frozenset[str]
    verified_capabilities: frozenset[str]
    capability_failures: tuple[str, ...]


@dataclass(frozen=True)
class ResolvedModel:
    role: str
    profile_name: str
    endpoint_name: str
    adapter: str
    model: str
    family: str
    capabilities: frozenset[str]
    configuration_checksum: str
    base_url: str
    temperature: float
    max_tokens: int
    min_context_length: int | None
    request_options: dict[str, object]
    structured_output: str
    _api_key: str = field(repr=False, compare=False)


def _normalize_openai_base_url(value: str) -> str:
    base = value.rstrip("/")
    if base.endswith("/v1/v1"):
        raise RuntimeError("OpenAI-compatible endpoint has multiple /v1 suffixes")
    if base.endswith("/v1"):
        base = base[:-3].rstrip("/")
    return f"{base}/v1"


class ModelRegistry:
    def __init__(self, config: ModelConfig, settings: Settings):
        self.config = config
        self.settings = settings
        self.checksum = model_config_checksum(config)
        self._capacity_guard: CapacityGuard | None = None

    @property
    def capacity_guard(self) -> CapacityGuard:
        if self._capacity_guard is None:
            from agent_flow.adapters.models import CapacityGuard

            self._capacity_guard = CapacityGuard(self.config)
        return self._capacity_guard

    def resolve(self, role: str) -> ResolvedModel:
        if role in self.config.disabled_roles:
            raise RuntimeError(f"role disabled in {self.config.mode}: {role}")
        if role not in self.config.roles:
            raise RuntimeError(f"unknown role: {role}")
        profile_name = self.config.roles[role]
        profile = self.config.profiles[profile_name]
        endpoint = self.config.endpoints[profile.endpoint]
        try:
            base_url = str(getattr(self.settings, endpoint.base_url_env.lower()))
        except AttributeError as exc:
            raise RuntimeError(
                f"setting not defined for endpoint {profile.endpoint}: {endpoint.base_url_env}"
            ) from exc
        if endpoint.adapter == "openai_compatible":
            base_url = _normalize_openai_base_url(base_url)
        else:
            base_url = base_url.rstrip("/")
        api_key = ""
        if endpoint.api_key_env:
            api_key = str(getattr(self.settings, endpoint.api_key_env.lower(), ""))
        return ResolvedModel(
            role=role,
            profile_name=profile_name,
            endpoint_name=profile.endpoint,
            adapter=endpoint.adapter,
            model=profile.model,
            family=profile.family,
            capabilities=frozenset(profile.capabilities),
            configuration_checksum=self.checksum,
            base_url=base_url,
            temperature=profile.temperature,
            max_tokens=profile.max_tokens,
            min_context_length=profile.min_context_length,
            request_options=dict(profile.request_options),
            structured_output=profile.structured_output,
            _api_key=api_key,
        )


class ModelInventoryProbe:
    def __init__(self, registry: ModelRegistry, *, timeout: float = 10.0):
        self.registry = registry
        self.timeout = timeout

    async def probe_all(self) -> dict[str, InventoryResult]:
        results: dict[str, InventoryResult] = {}
        for role in self.registry.config.roles:
            if role not in self.registry.config.disabled_roles:
                results[role] = await self.probe_role(role)
        return results

    async def probe_role(self, role: str) -> InventoryResult:
        resolved = self.registry.resolve(role)
        try:
            if resolved.adapter == "openai_compatible":
                digest, max_model_len = await self._require_openai_model(resolved)
            elif resolved.adapter == "ollama_compatible":
                digest, max_model_len = await self._require_ollama_model(resolved)
            else:  # ModelConfig prevents this, but keep the boundary explicit.
                raise RuntimeError(f"unsupported model adapter: {resolved.adapter}")
            if resolved.min_context_length is not None and (
                max_model_len is None
                or max_model_len < resolved.min_context_length
            ):
                raise RuntimeError(
                    "model context length requires at least "
                    f"{resolved.min_context_length}"
                )
        except RuntimeError as exc:
            if str(exc).startswith("exact model not found:"):
                raise
            raise RuntimeError(
                f"inventory probe failed for role {role} at inventory: {exc}"
            ) from exc
        except (
            httpx.HTTPError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            IndexError,
        ) as exc:
            raise RuntimeError(
                f"inventory probe failed for role {role} at inventory: {exc}"
            ) from exc

        verified: set[str] = set()
        try:
            if "embedding" in resolved.capabilities:
                from agent_flow.adapters.models import EmbeddingModel

                vectors = await EmbeddingModel(
                    self.registry, timeout=self.timeout
                ).embed(role, ["capability probe"])
                if not vectors:
                    raise RuntimeError(
                        "embedding capability requires exactly 1024 finite values"
                    )
                _validate_embedding_vector(vectors[0])
                verified.add("embedding")
            elif "structured_json" in resolved.capabilities:
                from agent_flow.adapters.models import ModelGateway

                response_type = ROLE_PROBE_SCHEMAS.get(role)
                if response_type is None:
                    raise RuntimeError(
                        f"no structured capability schema configured for role {role}"
                    )
                _, response = await ModelGateway(
                    self.registry, timeout=self.timeout
                ).structured_response(
                    role,
                    {"messages": [{"role": "user", "content": "Return a minimal valid response."}]},
                    response_type,
                )
                if response.finish_reason == "length":
                    raise RuntimeError("output truncated")
                verified.update({"chat", "structured_json"})
            elif "chat" in resolved.capabilities:
                from agent_flow.adapters.models import ModelGateway

                await ModelGateway(self.registry, timeout=self.timeout).complete(
                    role,
                    {"messages": [{"role": "user", "content": "Reply with ok."}]},
                )
                verified.add("chat")
            if (
                "reasoning_toggle" in resolved.capabilities
                and "chat" in verified
            ):
                if resolved.request_options.get("enable_thinking") is not False:
                    raise RuntimeError(
                        "reasoning_toggle requires enable_thinking=false for the probe"
                    )
                verified.add("reasoning_toggle")
        except (
            httpx.HTTPError,
            RuntimeError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            IndexError,
        ) as exc:
            raise RuntimeError(
                f"capability probe failed for role {role} at capability: {exc}"
            ) from exc

        missing = resolved.capabilities - verified
        if missing:
            raise RuntimeError(
                f"capability probe failed for role {role}: unverified {sorted(missing)}"
            )
        return InventoryResult(
            role=role,
            model=resolved.model,
            available=True,
            digest=digest,
            max_model_len=max_model_len,
            capabilities=resolved.capabilities,
            verified_capabilities=frozenset(verified),
            capability_failures=(),
        )

    async def _require_openai_model(
        self, resolved: ResolvedModel
    ) -> tuple[str | None, int | None]:
        headers = _authorization_header(resolved)
        async with httpx.AsyncClient(
            base_url=resolved.base_url, headers=headers, timeout=self.timeout
        ) as client:
            response = await client.get("/models")
            response.raise_for_status()
        entries = response.json().get("data", [])
        match = next((entry for entry in entries if entry.get("id") == resolved.model), None)
        if match is None:
            raise RuntimeError(f"exact model not found: {resolved.model}")
        return match.get("digest"), _openai_context_length(match)

    async def _require_ollama_model(
        self, resolved: ResolvedModel
    ) -> tuple[str | None, int | None]:
        headers = _authorization_header(resolved)
        async with httpx.AsyncClient(
            base_url=resolved.base_url, headers=headers, timeout=self.timeout
        ) as client:
            response = await client.get("/api/tags")
            response.raise_for_status()
            entries = response.json().get("models", [])
            match = next(
                (
                    entry
                    for entry in entries
                    if entry.get("name", entry.get("model")) == resolved.model
                ),
                None,
            )
            if match is None:
                raise RuntimeError(f"exact model not found: {resolved.model}")
            show = await client.post("/api/show", json={"model": resolved.model})
            show.raise_for_status()
            show_body = show.json()
        model_info = show_body.get("model_info", {})
        context_value = next(
            (
                value
                for key, value in model_info.items()
                if key == "context_length" or key.endswith(".context_length")
            ),
            None,
        )
        return match.get("digest"), _positive_int_or_none(context_value)


def _authorization_header(resolved: ResolvedModel) -> dict[str, str]:
    if not resolved._api_key:
        return {}
    return {"Authorization": f"Bearer {resolved._api_key}"}


def _positive_int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


def _openai_context_length(entry: dict[str, object]) -> int | None:
    """Context window from a /v1/models entry, whichever way it is spelled.

    vLLM reports `max_model_len` at the top level. llama.cpp reports the context
    it was actually started with as `meta.n_ctx`, and nothing else — so reading
    only vLLM's spelling left `min_context_length` unverifiable there, which is
    worse than it sounds: the check silently passes instead of failing.
    """
    meta = entry.get("meta")
    candidates = [entry.get("max_model_len"), entry.get("context_length")]
    if isinstance(meta, dict):
        # n_ctx is what the server will serve; n_ctx_train is what the weights
        # were trained for and is not a promise about this process.
        candidates.append(meta.get("n_ctx"))
    return next(
        (length for length in map(_positive_int_or_none, candidates) if length),
        None,
    )


class _RoleProbeSchemas(Mapping[str, type[BaseModel]]):
    _schema_names = {
        "dialogue_classifier": "DialogueClassificationResult",
        "strategy_advisor": "StrategyProposalResult",
        "response_generator": "ResponseDraft",
        "response_judge": "JudgeVerdictResult",
        "response_judge_zh_verifier": "JudgeVerdictResult",
        "promotion_judge_primary": "JudgeVerdictResult",
        "promotion_judge_secondary": "JudgeVerdictResult",
    }

    def __getitem__(self, role: str) -> type[BaseModel]:
        if role == "response_generator":
            return ResponseDraft
        from agent_flow.pipeline import model_outputs

        return getattr(model_outputs, self._schema_names[role])

    def __iter__(self) -> Iterator[str]:
        return iter(self._schema_names)

    def __len__(self) -> int:
        return len(self._schema_names)


ROLE_PROBE_SCHEMAS: Mapping[str, type[BaseModel]] = _RoleProbeSchemas()


def _validate_embedding_vector(vector: list[float]) -> None:
    if len(vector) != 1024 or not all(math.isfinite(value) for value in vector):
        raise RuntimeError(
            "embedding capability requires exactly 1024 finite values"
        )
