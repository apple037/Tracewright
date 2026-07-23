import json
from pathlib import Path

import httpx
import pytest
import respx

from agent_flow.config import ModelConfig, Settings, load_model_config
from agent_flow.contracts import ResponseDraft
from agent_flow.model_registry import ModelInventoryProbe, ModelRegistry
from agent_flow.adapters.models import EmbeddingModel, ModelGateway
from agent_flow.pipeline.model_outputs import DialogueClassificationResult


@pytest.fixture
def bootstrap_registry():
    settings = Settings(
        database_url="postgresql://agent:agent@localhost/agent",
        local_vllm_base_url="http://localhost:8000/v1/",
        local_vllm_api_key="local-secret",
        remote_model_base_url="http://remote-models:11434/",
        remote_model_api_key="remote-secret",
    )
    return ModelRegistry(
        load_model_config(Path("config/models.bootstrap.example.yaml")), settings
    )


def stub_openai_inventory(respx_mock, model, *, max_model_len=None):
    entry = {"id": model}
    if max_model_len is not None:
        entry["max_model_len"] = max_model_len
    return respx_mock.get("/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [entry]})
    )


def test_bootstrap_routes_initial_demo_models(bootstrap_registry):
    assert bootstrap_registry.resolve("dialogue_classifier").model == "qwen3.5:9b"
    assert (
        bootstrap_registry.resolve("dialogue_classifier").adapter
        == "openai_compatible"
    )
    assert (
        bootstrap_registry.resolve("response_generator").model
        == "Qwen/Qwen3-8B-AWQ"
    )
    assert bootstrap_registry.resolve("response_generator").min_context_length == 6144
    assert bootstrap_registry.resolve("embedding").model == "qwen3-embedding-0.6b"


@pytest.mark.asyncio
@respx.mock
async def test_vllm_inventory_requires_exact_model_and_strict_schema(bootstrap_registry):
    models = respx.get("http://localhost:8000/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"id": "Qwen/Qwen3-8B-AWQ", "max_model_len": 6144}
                ]
            },
        )
    )
    chat = respx.post("http://localhost:8000/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"text":"ok","citations":[],"evidence_ids":[]}'
                        }
                    }
                ]
            },
        )
    )

    result = await ModelInventoryProbe(bootstrap_registry).probe_role(
        "response_generator"
    )

    assert models.called
    assert result.model == "Qwen/Qwen3-8B-AWQ"
    assert result.max_model_len == 6144
    assert result.available is True
    assert result.verified_capabilities == frozenset(
        {"chat", "structured_json", "reasoning_toggle"}
    )
    request = chat.calls.last.request
    assert request.url == "http://localhost:8000/v1/chat/completions"
    payload = __import__("json").loads(request.content)
    assert payload["model"] == "Qwen/Qwen3-8B-AWQ"
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert payload["response_format"]["json_schema"]["schema"] == ResponseDraft.model_json_schema()
    assert payload["chat_template_kwargs"]["enable_thinking"] is False


@pytest.mark.asyncio
@respx.mock
async def test_inventory_does_not_fuzzy_match(bootstrap_registry):
    respx.get("http://localhost:8000/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "qwen3-8b"}]})
    )
    chat = respx.post("http://localhost:8000/v1/chat/completions").mock(
        return_value=httpx.Response(500)
    )

    with pytest.raises(RuntimeError, match="exact model not found"):
        await ModelInventoryProbe(bootstrap_registry).probe_role("response_generator")

    assert not chat.called


@pytest.mark.asyncio
@respx.mock
async def test_remote_openai_inventory_verifies_role_specific_structured_json(
    bootstrap_registry,
):
    models = respx.get("http://remote-models:11434/v1/models").mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": "qwen3.5:9b", "digest": "sha256:structured"}]}
        ),
    )
    chat = respx.post("http://remote-models:11434/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": (
                        '{"intent":"answer","conversation_mode":"informational",'
                        '"urgency":"normal","language":"en","emotion":{'
                        '"category":"neutral","dialogue_stage":"not_applicable",'
                        '"override":"no_emotional_content",'
                        '"response_mode":"business_first","confidence":1,'
                        '"evidence_spans":[],"reason_codes":["NO_EMOTIONAL_CONTENT"]}}'
                    )},
                }]
            },
        )
    )

    result = await ModelInventoryProbe(bootstrap_registry).probe_role(
        "dialogue_classifier"
    )

    assert models.called and chat.called
    assert result.digest == "sha256:structured"
    assert result.verified_capabilities == frozenset(
        {"chat", "structured_json", "reasoning_toggle"}
    )
    chat_payload = __import__("json").loads(chat.calls.last.request.content)
    assert (
        chat_payload["response_format"]["json_schema"]["schema"]
        == DialogueClassificationResult.model_json_schema()
    )
    assert chat_payload["chat_template_kwargs"]["enable_thinking"] is False
    assert chat_payload["max_tokens"] == 2048


@pytest.mark.asyncio
@respx.mock
async def test_openai_embedding_inventory_and_gateway_verify_vectors(bootstrap_registry):
    respx.get("http://remote-models:11434/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={"data": [
                {"id": "qwen3-embedding-0.6b", "digest": "sha256:embedding"}
            ]},
        )
    )
    vector = [0.1] * 1024
    embed_route = respx.post("http://remote-models:11434/v1/embeddings").mock(
        return_value=httpx.Response(200, json={"data": [{"embedding": vector}]})
    )

    probe_result = await ModelInventoryProbe(bootstrap_registry).probe_role("embedding")
    vectors = await EmbeddingModel(bootstrap_registry).embed("embedding", ["hello"])

    assert probe_result.verified_capabilities == frozenset({"embedding"})
    assert vectors == [vector]
    assert __import__("json").loads(embed_route.calls.last.request.content) == {
        "model": "qwen3-embedding-0.6b",
        "input": ["hello"],
    }


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("vector", [[0.0] * 1023, [float("nan")] * 1024])
async def test_embedding_probe_rejects_invalid_shape_or_values(
    bootstrap_registry, vector
):
    stub_openai_inventory(respx, "qwen3-embedding-0.6b")
    respx.post("/v1/embeddings").mock(
        return_value=httpx.Response(
            200,
            content=json.dumps({"data": [{"embedding": vector}]}).encode(),
            headers={"content-type": "application/json"},
        )
    )

    with pytest.raises(RuntimeError, match="embedding.*1024 finite"):
        await ModelInventoryProbe(bootstrap_registry).probe_role("embedding")


@pytest.mark.asyncio
@respx.mock
async def test_structured_probe_rejects_truncation_without_response_content(
    bootstrap_registry,
):
    stub_openai_inventory(respx, "Qwen/Qwen3-8B-AWQ", max_model_len=6144)
    secret_content = "do-not-include-model-content"
    respx.post("/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{
                    "finish_reason": "length",
                    "message": {"content": json.dumps({
                        "text": secret_content,
                        "citations": [],
                        "evidence_ids": [],
                    })},
                }]
            },
        )
    )

    with pytest.raises(
        RuntimeError,
        match=(
            "capability probe failed for role response_generator "
            "at capability: output truncated"
        ),
    ) as failure:
        await ModelInventoryProbe(bootstrap_registry).probe_role("response_generator")

    assert secret_content not in str(failure.value)


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("max_model_len", [None, 4096])
async def test_local_inventory_requires_configured_minimum_context(
    bootstrap_registry, max_model_len
):
    stub_openai_inventory(
        respx, "Qwen/Qwen3-8B-AWQ", max_model_len=max_model_len
    )

    with pytest.raises(
        RuntimeError,
        match="inventory probe failed for role response_generator at inventory",
    ):
        await ModelInventoryProbe(bootstrap_registry).probe_role("response_generator")


@pytest.mark.asyncio
@respx.mock
async def test_structured_rejects_missing_capability_before_io():
    config = ModelConfig.model_validate(
        {
            "mode": "bootstrap",
            "promotion_semantic_mode": "human_only",
            "endpoints": {
                "local": {
                    "adapter": "openai_compatible",
                    "base_url_env": "LOCAL_VLLM_BASE_URL",
                    "max_concurrency": 1,
                }
            },
            "profiles": {
                "freeform": {
                    "endpoint": "local",
                    "model": "custom/model",
                    "family": "custom",
                    "capabilities": ["chat"],
                    "max_concurrency": 1,
                }
            },
            "roles": {"freeform_writer": "freeform"},
        }
    )
    registry = ModelRegistry(
        config,
        Settings(
            database_url="postgresql://agent:agent@localhost/agent",
            local_vllm_base_url="http://localhost:8000",
        ),
    )
    route = respx.post("http://localhost:8000/v1/chat/completions").mock(
        return_value=httpx.Response(200)
    )

    with pytest.raises(RuntimeError, match="structured_json"):
        await ModelGateway(registry).structured(
            "freeform_writer", {"messages": []}, ResponseDraft
        )

    assert not route.called


def test_resolution_is_flexible_and_does_not_expose_credentials(bootstrap_registry):
    generator = bootstrap_registry.resolve("response_generator")
    classifier = bootstrap_registry.resolve("dialogue_classifier")

    assert generator.profile_name == "local_generator"
    assert generator.endpoint_name == "local_vllm"
    assert generator.base_url == "http://localhost:8000/v1"
    assert classifier.profile_name == "remote_structured"
    assert classifier.base_url == "http://remote-models:11434/v1"
    assert "secret" not in repr(generator)
    assert "local-secret" not in repr(generator)
    assert "remote-secret" not in repr(classifier)


def test_unknown_and_disabled_roles_fail_explicitly(bootstrap_registry):
    with pytest.raises(RuntimeError, match="unknown role"):
        bootstrap_registry.resolve("does_not_exist")
    with pytest.raises(RuntimeError, match="role disabled"):
        bootstrap_registry.resolve("response_judge_zh_verifier")


def test_openai_endpoint_rejects_ambiguous_double_v1_suffix():
    registry = ModelRegistry(
        load_model_config(Path("config/models.bootstrap.example.yaml")),
        Settings(
            database_url="postgresql://agent:agent@localhost/agent",
            local_vllm_base_url="http://localhost:8000/v1/v1",
        ),
    )

    with pytest.raises(RuntimeError, match="multiple /v1 suffixes"):
        registry.resolve("response_generator")


@pytest.mark.asyncio
@respx.mock
async def test_reasoning_toggle_probes_without_structured_json():
    config = ModelConfig.model_validate(
        {
            "mode": "bootstrap",
            "promotion_semantic_mode": "human_only",
            "endpoints": {
                "local": {
                    "adapter": "openai_compatible",
                    "base_url_env": "LOCAL_VLLM_BASE_URL",
                    "max_concurrency": 1,
                }
            },
            "profiles": {
                "thinking_chat": {
                    "endpoint": "local",
                    "model": "custom/thinking-chat",
                    "family": "custom",
                    "capabilities": ["chat", "reasoning_toggle"],
                    "request_options": {"enable_thinking": False},
                    "max_concurrency": 1,
                }
            },
            "roles": {"thinking_writer": "thinking_chat"},
        }
    )
    registry = ModelRegistry(
        config,
        Settings(
            database_url="postgresql://agent:agent@localhost/agent",
            local_vllm_base_url="http://localhost:8000/v1",
        ),
    )
    respx.get("http://localhost:8000/v1/models").mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": "custom/thinking-chat"}]}
        )
    )
    chat = respx.post("http://localhost:8000/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
        )
    )

    result = await ModelInventoryProbe(registry).probe_role("thinking_writer")

    assert result.verified_capabilities == frozenset({"chat", "reasoning_toggle"})
    payload = __import__("json").loads(chat.calls.last.request.content)
    assert payload["chat_template_kwargs"]["enable_thinking"] is False


@pytest.mark.asyncio
@respx.mock
async def test_openai_empty_choices_is_classified_as_malformed_response(
    bootstrap_registry,
):
    respx.post("http://localhost:8000/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": []})
    )

    with pytest.raises(
        RuntimeError,
        match="model response malformed for role response_generator at openai_chat",
    ):
        await ModelGateway(bootstrap_registry).structured(
            "response_generator", {"messages": []}, ResponseDraft
        )


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    "inventory_response",
    [
        httpx.Response(503, text="temporarily unavailable"),
        httpx.Response(200, content=b"not-json"),
    ],
    ids=["transport", "parse"],
)
async def test_inventory_transport_or_parse_failure_includes_role_and_stage(
    bootstrap_registry, inventory_response
):
    respx.get("http://localhost:8000/v1/models").mock(
        return_value=inventory_response
    )

    with pytest.raises(
        RuntimeError,
        match="inventory probe failed for role response_generator at inventory",
    ) as failure:
        await ModelInventoryProbe(bootstrap_registry).probe_role("response_generator")

    assert "local-secret" not in str(failure.value)
