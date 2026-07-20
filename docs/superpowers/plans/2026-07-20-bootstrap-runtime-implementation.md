# Bootstrap Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a testable FastAPI bootstrap runtime that runs the fixed customer-service turn pipeline with local vLLM `Qwen/Qwen3-8B`, remote `qwen3.5:9b`, remote `qwen3:embedding:0.6b`, mock business adapters, PostgreSQL/pgvector persistence, exact failure tracing, handoff outbox, and full-turn manual retry.

**Architecture:** A fixed `TurnPipeline` calls small typed nodes through explicit Python control flow. Model, RAG, tool, trace, and outbox boundaries are protocols with mock and PostgreSQL/HTTP implementations; bootstrap mode uses one remote Qwen semantic judge and exposes `reduced_assurance`. This plan delivers the runtime and trace APIs needed by the later Incident-first Console; the polished frontend and offline improvement/promotion lifecycle remain separate implementation plans.

**Tech Stack:** Python 3.12, `uv`, FastAPI, Pydantic v2, pydantic-settings, HTTPX, psycopg 3, Alembic, PostgreSQL 16 + pgvector, pytest, pytest-asyncio, respx, Docker Compose.

## Global Constraints

- Package and virtual-environment management uses `uv`; commit `uv.lock`.
- Application code addresses model roles, never model names.
- Host development vLLM base URL is `http://localhost:8000/v1`; Compose uses `http://host.docker.internal:8000/v1`.
- The local inventory must resolve the exact ID `Qwen/Qwen3-8B`.
- Bootstrap roles are local `Qwen/Qwen3-8B` for strategy/generation, remote `qwen3.5:9b` for classification/judging, and remote `qwen3:embedding:0.6b` for embeddings.
- Bootstrap mode is always marked `reduced_assurance`; the same Qwen profile is never invoked twice as fake independent judges.
- Business integrations are read-only and begin with typed mock adapters.
- High-risk outcomes create a signed Webhook outbox event and return a safe response.
- Every customer/session access is bound to an authenticated principal and tenant before downstream calls.
- Raw conversation text is retained unmasked for 30 days; structured traces are retained for 180 days; secrets are never logged.
- Hidden chain-of-thought is never stored; logs contain structured decision summaries and reason codes.
- Automatic retries cover only declared transient errors; manual retry creates an immutable full-turn replay trace.
- The MVP is a fixed pipeline, not a reusable orchestrator, graph runtime, or plugin system.

---

## File Structure

```text
agent-flow/
├── pyproject.toml
├── uv.lock
├── alembic.ini
├── compose.yaml
├── Dockerfile
├── .dockerignore
├── .env.example
├── config/
│   └── models.bootstrap.example.yaml
├── migrations/
│   ├── env.py
│   └── versions/0001_bootstrap_runtime.py
├── src/agent_flow/
│   ├── __init__.py
│   ├── main.py
│   ├── config.py
│   ├── auth.py
│   ├── contracts.py
│   ├── errors.py
│   ├── model_registry.py
│   ├── retry.py
│   ├── adapters/
│   │   ├── models.py
│   │   ├── evidence.py
│   │   └── webhook.py
│   ├── repositories/
│   │   ├── postgres.py
│   │   ├── traces.py
│   │   ├── conversations.py
│   │   ├── rag.py
│   │   └── outbox.py
│   ├── pipeline/
│   │   ├── classify.py
│   │   ├── risk.py
│   │   ├── evidence.py
│   │   ├── respond.py
│   │   ├── validate.py
│   │   └── turn.py
│   ├── api/
│   │   ├── dependencies.py
│   │   ├── turns.py
│   │   ├── traces.py
│   │   └── health.py
│   └── worker.py
└── tests/
    ├── conftest.py
    ├── fakes.py
    ├── unit/
    │   └── pipeline/
    │       └── conftest.py
    ├── integration/
    │   └── conftest.py
    ├── contract/
    ├── live/
    └── e2e/
```

The later Console plan owns `frontend/` and typed visual presenters. The later Improvement plan owns candidate generation, the append-only improvement ledger, evaluation suites, approval, activation, and rollback.

## Test Fixture Contracts

- `tests/integration/conftest.py` owns `postgres_pool: PostgresPool`, `trace_repository: PostgresTraceRepository`, `conversation_repository: ConversationRepository`, `rag_repository: RagRepository`, and `outbox: OutboxRepository`, all using `TEST_DATABASE_URL` and clearing only test-schema rows before/after a test.
- `tests/conftest.py` owns cross-suite fixtures `fake_models: FakeModelGateway`, `verified_draft: ResponseDraft`, and the opt-in `live_inventory_probe`. The fake response queues are deterministic and reset for every test.
- `tests/unit/pipeline/conftest.py` owns typed node inputs: `classification`, `order_plan`, `utc_now`, `fresh_collected_evidence`, `expired_collected_evidence`, `validated_evidence`, `repair_models`, and `repairable_validation`. Task 8 extends it with `trace_spy`, `trace_state`, and `pipeline_for_run_node`.
- `tests/e2e/conftest.py` owns `context: AuthorizedCustomerContext`, `clock: FrozenClock`, `mock_rag: MockRagClient`, `mock_tool: MockToolClient`, `memory_handoffs: MemoryHandoffSink`, `pipeline: TurnPipeline`, named failure-injection pipeline variants, `client: TestClient`, and token fixtures. It reuses root fixtures and uses tenant `t1`, customer `c1`, and deterministic committed evidence.
- A task that first introduces a fixture also creates or extends the owning `conftest.py` in the same commit. Test modules must not depend on an undeclared global fixture.

---

### Task 1: Project Foundation and Typed Settings

**Files:**
- Create: `pyproject.toml`
- Create: `.env.example`
- Create: `config/models.bootstrap.example.yaml`
- Create: `src/agent_flow/__init__.py`
- Create: `src/agent_flow/config.py`
- Test: `tests/unit/test_config.py`

**Interfaces:**
- Consumes: environment variables and a YAML configuration path.
- Produces: `Settings`, `EndpointConfig`, `ProfileConfig`, `ModelConfig`, `load_model_config(path: Path) -> ModelConfig`, and deterministic `model_config_checksum(config: ModelConfig) -> str`.

- [ ] **Step 1: Add the project metadata and dependencies**

```toml
[project]
name = "agent-flow"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "alembic>=1.14,<2",
  "fastapi>=0.115,<1",
  "httpx>=0.28,<1",
  "psycopg[binary,pool]>=3.2,<4",
  "pydantic>=2.10,<3",
  "pydantic-settings>=2.7,<3",
  "pyyaml>=6.0,<7",
  "uvicorn[standard]>=0.34,<1",
]

[dependency-groups]
dev = ["pytest>=8.3,<9", "pytest-asyncio>=0.25,<1", "respx>=0.22,<1"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/agent_flow"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 2: Write failing settings tests**

```python
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agent_flow.config import ModelConfig, Settings, load_model_config, model_config_checksum


def test_settings_default_to_bootstrap(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://agent:agent@localhost/agent")
    settings = Settings()
    assert settings.assurance_mode == "bootstrap"
    assert settings.local_vllm_base_url == "http://localhost:8000/v1"


def test_bootstrap_model_roles_are_exact():
    config = load_model_config(Path("config/models.bootstrap.example.yaml"))
    assert config.profiles[config.roles["response_generator"]].model == "Qwen/Qwen3-8B"
    assert "structured_json" in config.profiles[config.roles["response_generator"]].capabilities
    assert config.profiles[config.roles["dialogue_classifier"]].model == "qwen3.5:9b"
    assert config.profiles[config.roles["embedding"]].model == "qwen3:embedding:0.6b"
    assert set(config.disabled_roles) == {"response_judge_zh_verifier", "promotion_judge_secondary"}


def test_model_config_checksum_is_stable_across_hash_seeds():
    script = (
        "from pathlib import Path; "
        "from agent_flow.config import load_model_config, model_config_checksum; "
        "print(model_config_checksum(load_model_config(Path('config/models.bootstrap.example.yaml'))))"
    )
    checksums = set()
    for seed in (1, 7, 29, 113):
        env = {**os.environ, "PYTHONHASHSEED": str(seed)}
        result = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        checksums.add(result.stdout.strip())
    assert len(checksums) == 1


def test_model_config_checksum_changes_with_model_name():
    first = load_model_config(Path("config/models.bootstrap.example.yaml"))
    changed_profiles = dict(first.profiles)
    changed_profiles["local_generator"] = changed_profiles["local_generator"].model_copy(
        update={"model": "Qwen/another-model"}
    )
    second = first.model_copy(update={"profiles": changed_profiles})
    assert model_config_checksum(first) != model_config_checksum(second)


def test_response_generator_profile_requires_structured_json():
    config = load_model_config(Path("config/models.bootstrap.example.yaml"))
    data = config.model_dump(mode="python")
    data["profiles"]["local_generator"]["capabilities"].remove("structured_json")
    with pytest.raises(ValueError, match="response_generator.*structured_json"):
        ModelConfig.model_validate(data)
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_config.py -v`

Expected: FAIL because `agent_flow.config` does not exist.

- [ ] **Step 4: Implement strict settings and YAML contracts**

```python
# src/agent_flow/config.py
from pathlib import Path
from typing import Literal

import hashlib
import json
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str
    model_config_path: Path = Path("config/models.bootstrap.example.yaml")
    assurance_mode: Literal["bootstrap", "dual_judge"] = "bootstrap"
    local_vllm_base_url: str = "http://localhost:8000/v1"
    local_vllm_api_key: str = "EMPTY"
    remote_model_base_url: str = "http://127.0.0.1:11434"
    remote_model_api_key: str = ""
    webhook_url: str = "http://127.0.0.1:9999/mock-handoff"
    webhook_secret: str = "development-only"


class EndpointConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    adapter: Literal["openai_compatible", "ollama_compatible"]
    base_url_env: str
    api_key_env: str | None = None
    max_concurrency: int = Field(ge=1)


class ProfileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint: str
    model: str
    family: str
    capabilities: set[str]
    max_concurrency: int = Field(ge=1)
    temperature: float = 0.0
    max_tokens: int = Field(default=512, ge=1)
    request_options: dict[str, object] = Field(default_factory=dict)


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoints: dict[str, EndpointConfig]
    profiles: dict[str, ProfileConfig]
    roles: dict[str, str]
    mode: Literal["bootstrap", "dual_judge"]
    disabled_roles: set[str] = Field(default_factory=set)
    promotion_semantic_mode: Literal["human_only", "dual_judge_required"]

    @model_validator(mode="after")
    def validate_references(self):
        required_role_capabilities = {
            "dialogue_classifier": {"chat", "structured_json"},
            "strategy_advisor": {"chat", "structured_json"},
            "response_generator": {"chat", "structured_json"},
            "response_judge": {"chat", "structured_json"},
            "response_judge_zh_verifier": {"chat", "structured_json"},
            "promotion_judge_primary": {"chat", "structured_json"},
            "promotion_judge_secondary": {"chat", "structured_json"},
            "embedding": {"embedding"},
        }
        for profile_name, profile in self.profiles.items():
            if profile.endpoint not in self.endpoints:
                raise ValueError(f"profile {profile_name} references unknown endpoint")
            if profile.max_concurrency > self.endpoints[profile.endpoint].max_concurrency:
                raise ValueError(f"profile {profile_name} exceeds endpoint concurrency")
        for role, profile_name in self.roles.items():
            if profile_name not in self.profiles:
                raise ValueError(f"role {role} references unknown profile")
            missing = required_role_capabilities.get(role, set()) - self.profiles[profile_name].capabilities
            if missing:
                raise ValueError(f"role {role} missing capabilities: {sorted(missing)}")
        return self


def load_model_config(path: Path) -> ModelConfig:
    return ModelConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def _canonicalize(value: object) -> object:
    if isinstance(value, dict):
        return {key: _canonicalize(value[key]) for key in sorted(value)}
    if isinstance(value, set):
        canonical_items = [_canonicalize(item) for item in value]
        return sorted(
            canonical_items,
            key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )
    if isinstance(value, list):
        return [_canonicalize(item) for item in value]
    return value


def model_config_checksum(config: ModelConfig) -> str:
    canonical = _canonicalize(config.model_dump(mode="python", exclude_none=False))
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
```

Create `.env.example` exactly as:

```dotenv
DATABASE_URL=postgresql://agent:agent@localhost:5432/agent
MODEL_CONFIG_PATH=config/models.bootstrap.example.yaml
ASSURANCE_MODE=bootstrap
LOCAL_VLLM_BASE_URL=http://localhost:8000/v1
LOCAL_VLLM_API_KEY=EMPTY
REMOTE_MODEL_BASE_URL=http://127.0.0.1:11434
REMOTE_MODEL_API_KEY=
WEBHOOK_URL=http://127.0.0.1:9999/mock-handoff
WEBHOOK_SECRET=development-only
```

Create `config/models.bootstrap.example.yaml` exactly as:

```yaml
mode: bootstrap
promotion_semantic_mode: human_only
disabled_roles:
  - response_judge_zh_verifier
  - promotion_judge_secondary
endpoints:
  local_vllm:
    adapter: openai_compatible
    base_url_env: LOCAL_VLLM_BASE_URL
    api_key_env: LOCAL_VLLM_API_KEY
    max_concurrency: 1
  remote_models:
    adapter: ollama_compatible
    base_url_env: REMOTE_MODEL_BASE_URL
    api_key_env: REMOTE_MODEL_API_KEY
    max_concurrency: 6
profiles:
  local_generator:
    endpoint: local_vllm
    model: Qwen/Qwen3-8B
    family: qwen
    capabilities: [chat, structured_json, reasoning_toggle]
    request_options: {enable_thinking: false}
    temperature: 0.2
    max_tokens: 1024
    max_concurrency: 1
  remote_structured:
    endpoint: remote_models
    model: qwen3.5:9b
    family: qwen
    capabilities: [chat, structured_json, reasoning_toggle]
    request_options: {enable_thinking: false}
    temperature: 0
    max_tokens: 512
    max_concurrency: 2
  remote_embedding:
    endpoint: remote_models
    model: qwen3:embedding:0.6b
    family: qwen
    capabilities: [embedding]
    max_concurrency: 4
roles:
  dialogue_classifier: remote_structured
  strategy_advisor: local_generator
  response_generator: local_generator
  response_judge: remote_structured
  promotion_judge_primary: remote_structured
  embedding: remote_embedding
```

Concurrency is configured per endpoint and per shared profile, not per role alias. The remote profile limits sum to the endpoint limit (`2 + 4 = 6`); every role mapped to `remote_structured` shares its capacity of 2. The endpoint semaphore remains the final cap if an operator later configures profile totals above it, and readiness must report that effective bottleneck explicitly.

- [ ] **Step 5: Lock dependencies and verify**

Run: `uv lock && uv run pytest tests/unit/test_config.py -v`

Expected: all five configuration tests pass, including identical checksums across hash seeds and rejection of a generator without structured JSON.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock .env.example config/models.bootstrap.example.yaml src/agent_flow tests/unit/test_config.py
git commit -m "build: scaffold bootstrap runtime configuration"
```

---

### Task 2: Domain Contracts, Errors, and Authorization Context

**Files:**
- Create: `src/agent_flow/contracts.py`
- Create: `src/agent_flow/errors.py`
- Create: `src/agent_flow/auth.py`
- Create: `tests/fakes.py`
- Create: `tests/conftest.py`
- Test: `tests/unit/test_auth.py`
- Test: `tests/unit/test_contracts.py`

**Interfaces:**
- Consumes: bearer-token claims and API request identifiers.
- Produces: `AuthenticatedPrincipal`, `AuthorizedCustomerContext`, `TurnRequest`, `TurnResult`, `AgentError`, and `bind_customer_context(principal: AuthenticatedPrincipal, requested_customer_id: str | None, session_customer_id: str | None) -> AuthorizedCustomerContext`.

- [ ] **Step 1: Write failing IDOR and serialization tests**

```python
import pytest

from agent_flow.auth import AuthenticatedPrincipal, bind_customer_context
from agent_flow.errors import AgentError


def test_self_service_customer_is_derived_not_trusted():
    principal = AuthenticatedPrincipal(subject_id="u1", tenant_id="t1", customer_id="c1", scopes=set())
    with pytest.raises(AgentError) as caught:
        bind_customer_context(principal, requested_customer_id="c2", session_customer_id=None)
    assert caught.value.error_code == "AUTH_CUSTOMER_MISMATCH"


def test_agent_requires_act_as_scope():
    principal = AuthenticatedPrincipal(subject_id="a1", tenant_id="t1", customer_id=None, scopes={"agent"})
    with pytest.raises(AgentError):
        bind_customer_context(principal, requested_customer_id="c2", session_customer_id=None)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_auth.py tests/unit/test_contracts.py -v`

Expected: collection FAIL because domain modules do not exist.

- [ ] **Step 3: Implement immutable authorization and error contracts**

```python
# src/agent_flow/auth.py
from pydantic import BaseModel, ConfigDict

from agent_flow.errors import AgentError


class AuthenticatedPrincipal(BaseModel):
    model_config = ConfigDict(frozen=True)
    subject_id: str
    tenant_id: str
    customer_id: str | None
    scopes: set[str]


class AuthorizedCustomerContext(BaseModel):
    model_config = ConfigDict(frozen=True)
    subject_id: str
    tenant_id: str
    customer_id: str


def bind_customer_context(
    principal: AuthenticatedPrincipal,
    requested_customer_id: str | None,
    session_customer_id: str | None,
) -> AuthorizedCustomerContext:
    if principal.customer_id is not None:
        if requested_customer_id not in (None, principal.customer_id):
            raise AgentError.auth("AUTH_CUSTOMER_MISMATCH")
        customer_id = principal.customer_id
    else:
        if "customer:act_as" not in principal.scopes or requested_customer_id is None:
            raise AgentError.auth("AUTH_ACT_AS_REQUIRED")
        customer_id = requested_customer_id
    if session_customer_id not in (None, customer_id):
        raise AgentError.auth("AUTH_SESSION_OWNERSHIP")
    return AuthorizedCustomerContext(
        subject_id=principal.subject_id,
        tenant_id=principal.tenant_id,
        customer_id=customer_id,
    )
```

Implement `AgentError` as a dataclass/exception with `error_code`, `category`, `retryable`, `failure_stage`, `component`, `operation`, `field_path`, and safe public message. Implement Pydantic contracts for trace IDs, emotion assessment, strategy decision, evidence, model verdict, handoff, turn request/result, and assurance metadata using finite enums from the design spec. The bootstrap `StrategyDecision` contract contains `strategy_version`, `response_mode`, ordered `answer_order`, and bounded `reason_codes`; `ResponseDraft` contains `text`, `citations`, and `evidence_ids`.

Create the shared model fake with an explicit role-call ledger:

```python
# tests/fakes.py
from collections import deque


class FakeModelGateway:
    def __init__(self, responses: dict[str, list[object]]):
        self.responses = {role: deque(values) for role, values in responses.items()}
        self.calls: list[str] = []
        self.requests: list[object] = []

    async def structured(self, role: str, request: object, response_type: type):
        self.calls.append(role)
        self.requests.append(request)
        return response_type.model_validate(self.responses[role].popleft())

    async def complete(self, role: str, request: object) -> str:
        self.calls.append(role)
        self.requests.append(request)
        return str(self.responses[role].popleft())
```

Create cross-suite fixtures at their first use rather than in a test module:

```python
# tests/conftest.py
import pytest

from agent_flow.contracts import ResponseDraft
from tests.fakes import FakeModelGateway


@pytest.fixture
def fake_models():
    return FakeModelGateway({
        "dialogue_classifier": [{
            "intent": "order_status",
            "conversation_mode": "transactional_read",
            "urgency": "normal",
            "language": "zh-TW",
            "emotion": {
                "category": "stress_exhaustion",
                "dialogue_stage": "surface",
                "override": "none",
                "response_mode": "business_first",
                "confidence": 0.91,
                "evidence_spans": ["很累"],
                "reason_codes": ["EXPLICIT_EXHAUSTION"],
            },
        }],
        "strategy_advisor": [{
            "strategy_version": "bootstrap-v1",
            "response_mode": "business_first",
            "answer_order": ["verified_fact", "brief_acknowledgment"],
            "reason_codes": ["TRANSACTIONAL_READ", "VERIFIED_EVIDENCE_AVAILABLE"],
        }],
        "response_generator": [{
            "text": "訂單目前運送中，尚無確認送達日期。",
            "citations": ["tool:order.lookup:o1"],
            "evidence_ids": ["tool-result-1"],
        }],
        "response_judge": [{
            "passed": True,
            "failed_criteria": [],
            "confidence": 0.88,
            "reason_codes": ["GROUNDED"],
        }],
    })


@pytest.fixture
def verified_draft():
    return ResponseDraft(
        text="訂單目前運送中，尚無確認送達日期。",
        citations=["tool:order.lookup:o1"],
        evidence_ids=["tool-result-1"],
    )
```

- [ ] **Step 4: Verify contracts and authorization**

Run: `uv run pytest tests/unit/test_auth.py tests/unit/test_contracts.py -v`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/agent_flow/auth.py src/agent_flow/contracts.py src/agent_flow/errors.py tests/conftest.py tests/fakes.py tests/unit/test_auth.py tests/unit/test_contracts.py
git commit -m "feat: add typed turn and authorization contracts"
```

---

### Task 3: Model Registry, Inventory, and Capability Gate

**Files:**
- Create: `src/agent_flow/model_registry.py`
- Create: `src/agent_flow/adapters/models.py`
- Test: `tests/contract/test_model_inventory.py`

**Interfaces:**
- Consumes: `Settings`, `ModelConfig`, HTTPX client.
- Produces: `ResolvedModel`, `ModelRegistry.resolve(role: str) -> ResolvedModel`, `ModelInventoryProbe.probe_all() -> dict[str, InventoryResult]`, `ModelGateway.complete(role: str, request: object) -> str`, `ModelGateway.structured(role: str, request: object, response_type: type[T]) -> T`, and `EmbeddingModel.embed(role: str, texts: list[str]) -> list[list[float]]`.
- `ModelGateway.structured` requires the resolved profile to declare `structured_json`, sends a strict schema through the adapter, and rejects the call before I/O when the capability is absent.

- [ ] **Step 1: Write failing exact-inventory tests**

```python
from pathlib import Path

import httpx
import pytest
import respx

from agent_flow.config import Settings, load_model_config
from agent_flow.model_registry import ModelRegistry
from agent_flow.model_registry import ModelInventoryProbe


@pytest.fixture
def bootstrap_registry():
    settings = Settings(
        database_url="postgresql://agent:agent@localhost/agent",
        local_vllm_base_url="http://localhost:8000/v1",
        remote_model_base_url="http://remote-models:11434",
    )
    return ModelRegistry(load_model_config(Path("config/models.bootstrap.example.yaml")), settings)


@pytest.mark.asyncio
@respx.mock
async def test_vllm_inventory_requires_exact_model_id(bootstrap_registry):
    respx.get("http://localhost:8000/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "Qwen/Qwen3-8B"}]})
    )
    respx.post("http://localhost:8000/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={
            "choices": [{"message": {"content": (
                '{"text":"ok","citations":[],"evidence_ids":[]}'
            )}}]
        })
    )
    result = await ModelInventoryProbe(bootstrap_registry).probe_role("response_generator")
    assert result.model == "Qwen/Qwen3-8B"
    assert result.available is True
    assert "structured_json" in result.verified_capabilities


@pytest.mark.asyncio
@respx.mock
async def test_inventory_does_not_fuzzy_match(bootstrap_registry):
    respx.get("http://localhost:8000/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "qwen3-8b"}]})
    )
    with pytest.raises(RuntimeError, match="exact model not found"):
        await ModelInventoryProbe(bootstrap_registry).probe_role("response_generator")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/contract/test_model_inventory.py -v`

Expected: FAIL because the registry/probe are missing.

- [ ] **Step 3: Implement role resolution and adapter-specific inventory**

```python
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from agent_flow.config import ModelConfig, Settings, model_config_checksum


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ModelResponse(BaseModel):
    text: str
    finish_reason: str
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class InventoryResult:
    role: str
    model: str
    available: bool
    digest: str | None
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


class ModelRegistry:
    def __init__(self, config: ModelConfig, settings: Settings):
        self.config = config
        self.settings = settings
        self.checksum = model_config_checksum(config)

    def resolve(self, role: str) -> ResolvedModel:
        if role in self.config.disabled_roles:
            raise RuntimeError(f"role disabled in {self.config.mode}: {role}")
        profile_name = self.config.roles[role]
        profile = self.config.profiles[profile_name]
        endpoint = self.config.endpoints[profile.endpoint]
        return ResolvedModel(
            role=role,
            profile_name=profile_name,
            endpoint_name=profile.endpoint,
            adapter=endpoint.adapter,
            model=profile.model,
            family=profile.family,
            capabilities=frozenset(profile.capabilities),
            configuration_checksum=self.checksum,
        )
```

Implement `/v1/models` parsing for `openai_compatible` and `/api/tags` plus `/api/show` parsing for `ollama_compatible`. Normalize exactly one `/v1` suffix for OpenAI endpoints. For every declared required capability, run a bounded adapter-specific sample and record it in `verified_capabilities`; declared capabilities are not treated as verified merely because inventory resolved. The local generator probe sends the finite `ResponseDraft` schema using OpenAI-compatible `response_format={"type":"json_schema", ...}`, parses the returned content with Pydantic, and fails readiness if `structured_json` or `reasoning_toggle` is not verified. Probe remote structured JSON and embeddings separately; expose results without keys.

- [ ] **Step 4: Run contract tests, then the read-only local inventory command**

Run: `uv run pytest tests/contract/test_model_inventory.py -v`

Expected: all tests pass.

Run: `Invoke-RestMethod http://localhost:8000/v1/models | ConvertTo-Json -Depth 5`

Expected: output contains exact ID `Qwen/Qwen3-8B`.

- [ ] **Step 5: Commit**

```bash
git add src/agent_flow/model_registry.py src/agent_flow/adapters/models.py tests/contract/test_model_inventory.py
git commit -m "feat: add exact model inventory gate"
```

---

### Task 4: PostgreSQL, pgvector, and Trace Repository

**Files:**
- Create: `alembic.ini`
- Create: `migrations/env.py`
- Create: `migrations/versions/0001_bootstrap_runtime.py`
- Create: `src/agent_flow/repositories/postgres.py`
- Create: `src/agent_flow/repositories/traces.py`
- Create: `src/agent_flow/repositories/conversations.py`
- Create: `tests/integration/conftest.py`
- Test: `tests/integration/test_trace_repository.py`

**Interfaces:**
- Consumes: `DATABASE_URL`, typed trace/span/event values.
- Produces: `PostgresPool`, `TraceRepository.start_trace`, `start_span`, `append_event`, `finish_span`, `finish_trace`, `get_trace`, `events_after`, plus `ConversationRepository.get_snapshot`, `append_turn`, and `get_retry_snapshot`.

- [ ] **Step 1: Write a failing repository integration test**

```python
import os

import pytest_asyncio

from agent_flow.repositories.postgres import PostgresPool
from agent_flow.repositories.traces import PostgresTraceRepository


@pytest_asyncio.fixture
async def trace_repository():
    pool = PostgresPool(os.environ["TEST_DATABASE_URL"])
    await pool.open()
    repository = PostgresTraceRepository(pool)
    await repository.clear_test_data()
    yield repository
    await repository.clear_test_data()
    await pool.close()


@pytest.mark.asyncio
async def test_trace_events_are_monotonic_and_locate_failure(trace_repository):
    trace_id = await trace_repository.start_trace(tenant_id="t1", customer_id="c1", session_id="s1")
    span_id = await trace_repository.start_span(trace_id, "response_validator")
    event = await trace_repository.append_event(
        trace_id=trace_id,
        span_id=span_id,
        event_type="validation.failed",
        component="qwen_judge",
        status="failed",
        error_code="VAL_GROUND_004",
        payload={"field_path": "draft.delivery_date"},
    )
    await trace_repository.finish_trace(trace_id, "failed", primary_failure_event_id=event.id)
    loaded = await trace_repository.get_trace(trace_id, tenant_id="t1")
    assert loaded.primary_failure_event_id == event.id
    assert [item.sequence for item in loaded.events] == [1]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/integration/test_trace_repository.py -v`

Expected: FAIL because migrations/repository do not exist.

- [ ] **Step 3: Create the initial migration**

Create pgvector extension plus schemas/tables: `runtime.conversations`, `runtime.turns`, `runtime.jobs`, `observability.traces`, `observability.spans`, `observability.events`, `rag.documents`, `rag.chunks VECTOR(1024)`, and `notification.outbox`. Include tenant/customer columns, trace retry lineage, monotonically unique `(trace_id, sequence)`, expiry indexes, and outbox idempotency uniqueness. Do not create HNSW in this migration.

```python
def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE SCHEMA IF NOT EXISTS observability")
    op.create_table(
        "traces",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("customer_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("primary_failure_event_id", sa.BigInteger()),
        sa.Column("root_trace_id", postgresql.UUID(as_uuid=True)),
        sa.Column("retry_of_trace_id", postgresql.UUID(as_uuid=True)),
        sa.Column("retry_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        schema="observability",
    )
```

- [ ] **Step 4: Implement repository transactions and tenant-scoped reads**

Use `psycopg_pool.AsyncConnectionPool`; assign event sequence inside a transaction while locking the trace row. `get_trace` must include `WHERE id = %s AND tenant_id = %s`. Store typed event payloads as JSONB and never accept secrets in repository APIs.

`ConversationRepository` stores each request's captured context snapshot reference before model calls, appends the final customer/assistant turn only after finalization, scopes every query by tenant/customer/session, and returns the immutable snapshot used by full-turn manual retry.

- [ ] **Step 5: Run migrations and tests**

Run (PowerShell): `$env:DATABASE_URL='postgresql://agent:agent@localhost:5432/agent_test'; $env:TEST_DATABASE_URL=$env:DATABASE_URL; uv run alembic upgrade head; uv run pytest tests/integration/test_trace_repository.py -v`

Expected: migration succeeds and repository test passes.

- [ ] **Step 6: Commit**

```bash
git add alembic.ini migrations src/agent_flow/repositories tests/integration/conftest.py tests/integration/test_trace_repository.py
git commit -m "feat: add pgvector runtime and trace persistence"
```

---

### Task 5: Retry Policy and Concurrency Guards

**Files:**
- Create: `src/agent_flow/retry.py`
- Modify: `src/agent_flow/adapters/models.py`
- Test: `tests/unit/test_retry.py`
- Test: `tests/contract/test_model_concurrency.py`

**Interfaces:**
- Consumes: endpoint/profile limits and a typed async operation.
- Produces: `RetryPolicy`, `AttemptRecord`, `run_with_retry(operation: Callable[[], Awaitable[T]], policy: RetryPolicy) -> tuple[T, list[AttemptRecord]]`, and `CapacityGuard.acquire(endpoint_name: str, profile_name: str, timeout_ms: int) -> AsyncContextManager[CapacityWait]`.

- [ ] **Step 1: Write failing retry classification tests**

```python
@pytest.mark.asyncio
async def test_503_retries_but_validation_failure_does_not():
    attempts = 0

    async def transient_operation():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise AgentError.dependency("MODEL_503", retryable=True)
        return "ok"

    result, records = await run_with_retry(transient_operation, RetryPolicy(max_attempts=2, base_delay_ms=1))
    assert result == "ok"
    assert [record.outcome for record in records] == ["failed", "completed"]


@pytest.mark.asyncio
async def test_policy_error_is_not_retried():
    async def operation():
        raise AgentError.validation("UNSUPPORTED_CLAIM")

    with pytest.raises(AgentError):
        await run_with_retry(operation, RetryPolicy(max_attempts=3, base_delay_ms=1))
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/unit/test_retry.py tests/contract/test_model_concurrency.py -v`

Expected: FAIL because retry/capacity code is missing.

- [ ] **Step 3: Implement bounded retries and two-level semaphores**

```python
@dataclass(frozen=True)
class AttemptRecord:
    attempt: int
    outcome: Literal["completed", "failed"]
    duration_ms: int
    backoff_ms: int
    error_code: str | None


@dataclass(frozen=True)
class CapacityWait:
    endpoint_name: str
    profile_name: str
    wait_ms: int
    wait_limit_kind: Literal["endpoint", "profile", "none"]


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 1
    base_delay_ms: int = 100
    max_delay_ms: int = 1000


async def run_with_retry(operation, policy: RetryPolicy):
    records: list[AttemptRecord] = []
    for attempt in range(1, policy.max_attempts + 1):
        started = time.monotonic()
        try:
            value = await operation()
            records.append(AttemptRecord(attempt, "completed", elapsed_ms(started), 0, None))
            return value, records
        except AgentError as error:
            records.append(AttemptRecord(attempt, "failed", elapsed_ms(started), 0, error.error_code))
            if not error.retryable or attempt == policy.max_attempts:
                raise
            delay_ms = min(policy.max_delay_ms, policy.base_delay_ms * 2 ** (attempt - 1))
            await asyncio.sleep(delay_ms / 1000)
    raise RuntimeError("unreachable retry state")
```

`CapacityGuard` must acquire endpoint capacity then profile capacity under one deadline, release in reverse order, and report `wait_limit_kind`. Tests must prove a profile limit of 3 never bypasses an endpoint limit of 1.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/test_retry.py tests/contract/test_model_concurrency.py -v`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/agent_flow/retry.py src/agent_flow/adapters/models.py tests/unit/test_retry.py tests/contract/test_model_concurrency.py
git commit -m "feat: add bounded retry and model capacity guards"
```

---

### Task 6: Read-Only Evidence Interfaces, Mocks, and pgvector Search

**Files:**
- Create: `src/agent_flow/adapters/evidence.py`
- Create: `src/agent_flow/repositories/rag.py`
- Create: `tests/fixtures/rag.json`
- Create: `tests/fixtures/tools.json`
- Test: `tests/contract/test_evidence_adapters.py`
- Test: `tests/integration/test_rag_repository.py`

**Interfaces:**
- Consumes: `AuthorizedCustomerContext`, `RagSearchRequest`, `ToolCallRequest`, embedding vectors.
- Produces: `RagClient.search`, `ToolClient.call`, `MockRagClient`, `MockToolClient`, `RagRepository.search_cosine`.

- [ ] **Step 1: Write failing contract tests**

```python
@pytest.fixture
def authorized_context():
    return AuthorizedCustomerContext(subject_id="u1", tenant_id="t1", customer_id="c1")


@pytest.fixture
def mock_tool():
    return MockToolClient.from_fixture("tests/fixtures/tools.json")


@pytest.mark.asyncio
@pytest.mark.parametrize("factory", [MockRagClient])
async def test_rag_result_contains_provenance(factory, authorized_context):
    client = factory.from_fixture("tests/fixtures/rag.json")
    result = await client.search(authorized_context, RagSearchRequest(query="保固多久", limit=3))
    assert result.items[0].source_id
    assert result.items[0].version
    assert result.items[0].content_checksum


@pytest.mark.asyncio
async def test_tool_requires_authorized_context(mock_tool):
    with pytest.raises(TypeError):
        await mock_tool.call(ToolCallRequest(tool="order.lookup", arguments={"order_id": "o1"}))
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/contract/test_evidence_adapters.py tests/integration/test_rag_repository.py -v`

Expected: FAIL because adapters/repository are missing.

- [ ] **Step 3: Implement protocols and deterministic mocks**

```python
class RagClient(Protocol):
    async def search(
        self, context: AuthorizedCustomerContext, request: RagSearchRequest
    ) -> RagSearchResult:
        raise NotImplementedError


class ToolClient(Protocol):
    async def call(
        self, context: AuthorizedCustomerContext, request: ToolCallRequest
    ) -> ToolCallResult:
        raise NotImplementedError
```

Mocks load committed JSON fixtures, enforce tenant/customer fields, and emit freshness/checksum metadata. `RagRepository.search_cosine` uses pgvector cosine distance with a tenant filter and exact search; do not add HNSW.

Create deterministic fixtures:

```json
[{"tenant_id":"t1","source_id":"policy-1","version":"v1","content":"標準保固期為一年。","valid_until":"2099-12-31T00:00:00Z"}]
```

```json
[{"tenant_id":"t1","customer_id":"c1","tool":"order.lookup","arguments":{"order_id":"o1"},"result":{"status":"in_transit","delivery_date":null}}]
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/contract/test_evidence_adapters.py tests/integration/test_rag_repository.py -v`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/agent_flow/adapters/evidence.py src/agent_flow/repositories/rag.py tests/contract tests/integration/test_rag_repository.py tests/fixtures
git commit -m "feat: add authorized RAG and tool evidence adapters"
```

---

### Task 7: Classification, Risk, Evidence, Response, and Validation Nodes

**Files:**
- Create: `src/agent_flow/pipeline/classify.py`
- Create: `src/agent_flow/pipeline/risk.py`
- Create: `src/agent_flow/pipeline/evidence.py`
- Create: `src/agent_flow/pipeline/respond.py`
- Create: `src/agent_flow/pipeline/validate.py`
- Create: `tests/unit/pipeline/conftest.py`
- Test: `tests/unit/pipeline/test_nodes.py`

**Interfaces:**
- Consumes: authorized context, conversation snapshot, model gateway, evidence clients.
- Produces: `classify_dialogue`, `risk_precheck`, `plan_evidence`, `collect_evidence`, `validate_evidence`, `select_strategy`, `generate_response`, `repair_response`, and `validate_response`.
- `risk_precheck(classification: DialogueClassification, message: str) -> RiskDecision` is deterministic and returns bounded handoff reason codes.
- `plan_evidence(classification: DialogueClassification) -> EvidencePlan`, `collect_evidence(context: AuthorizedCustomerContext, plan: EvidencePlan, rag: RagClient, tools: ToolClient) -> CollectedEvidence`, and `validate_evidence(plan: EvidencePlan, evidence: CollectedEvidence, now: datetime) -> ValidatedEvidence` are distinct traceable nodes.
- `select_strategy(models: ModelGateway, classification: DialogueClassification, risk: RiskDecision, evidence: ValidatedEvidence) -> StrategyDecision` calls `strategy_advisor` once.
- `generate_response(models: ModelGateway, snapshot: ConversationSnapshot, strategy: StrategyDecision, evidence: ValidatedEvidence) -> ResponseDraft` calls `ModelGateway.structured("response_generator", ..., ResponseDraft)` once.
- `repair_response(models: ModelGateway, draft: ResponseDraft, validation: ValidationResult, evidence: ValidatedEvidence) -> ResponseDraft` uses the same structured response-generator role with only the failed criteria and grounded evidence, never the judge role.
- `validate_response(models: ModelGateway, draft: ResponseDraft, evidence: ValidatedEvidence, assurance_mode: str) -> ValidationResult` gives the judge the frozen draft and verified evidence.

- [ ] **Step 1: Write failing node tests using fakes**

```python
@pytest.mark.asyncio
async def test_classifier_returns_intent_and_emotion_in_one_call(fake_models):
    result = await classify_dialogue(fake_models, ["我很累，訂單也還沒到"])
    assert result.intent == "order_status"
    assert result.emotion.category == "stress_exhaustion"
    assert fake_models.calls == ["dialogue_classifier"]


@pytest.mark.asyncio
async def test_bootstrap_validator_calls_one_judge(fake_models, verified_draft, validated_evidence):
    verdict = await validate_response(
        fake_models,
        verified_draft,
        validated_evidence,
        assurance_mode="bootstrap",
    )
    assert verdict.assurance == "reduced_assurance"
    assert fake_models.calls == ["response_judge"]


def test_risk_precheck_handoffs_account_security_issue(classification):
    result = risk_precheck(classification, "我的帳號被盜用了")
    assert result.requires_handoff is True
    assert result.reason_code == "ACCOUNT_SECURITY"


def test_evidence_planner_declares_required_order_fact_and_freshness(classification):
    plan = plan_evidence(classification)
    assert plan.required_facts == ["order.current_status"]
    assert plan.tool_calls[0].operation == "order.lookup"
    assert plan.tool_calls[0].freshness_seconds == 60


def test_evidence_validator_rejects_missing_or_expired_required_evidence(
    order_plan,
    expired_collected_evidence,
    utc_now,
):
    with pytest.raises(AgentError) as caught:
        validate_evidence(order_plan, expired_collected_evidence, now=utc_now)
    assert caught.value.error_code == "EVIDENCE_INSUFFICIENT"
    assert caught.value.failure_stage == "evidence_validator"
    assert caught.value.retryable is False


def test_evidence_validator_accepts_fresh_non_conflicting_required_evidence(
    order_plan,
    fresh_collected_evidence,
    utc_now,
):
    validated = validate_evidence(order_plan, fresh_collected_evidence, now=utc_now)
    assert validated.sufficient is True
    assert validated.reason_codes == ["REQUIRED_EVIDENCE_PRESENT"]


@pytest.mark.asyncio
async def test_repair_uses_generator_with_failed_criteria(
    repair_models,
    verified_draft,
    repairable_validation,
    validated_evidence,
):
    repaired = await repair_response(
        repair_models,
        verified_draft,
        repairable_validation,
        validated_evidence,
    )
    assert repaired.text != verified_draft.text
    assert repair_models.calls == ["response_generator"]
    assert repair_models.requests[0].failed_criteria == ["UNSUPPORTED_DELIVERY_PROMISE"]
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/unit/pipeline/test_nodes.py -v`

Expected: FAIL because node functions do not exist.

- [ ] **Step 3: Implement one-purpose typed nodes**

Each model node sends a finite JSON schema, validates with Pydantic, records bounded reason codes, and never stores native reasoning output. `classify_dialogue` returns intent and emotion in one call. `risk_precheck` performs the high-risk rules before any evidence calls. `plan_evidence` declares required facts and freshness constraints; `collect_evidence` uses `asyncio.TaskGroup` and reports each source independently; `validate_evidence` rejects missing, expired, conflicting, or incomplete required facts with non-retryable `EVIDENCE_INSUFFICIENT` at `failure_stage="evidence_validator"`. `validate_response` runs deterministic checks before the semantic judge and returns failed criteria plus repairability.

```python
def validate_evidence(
    plan: EvidencePlan,
    evidence: CollectedEvidence,
    now: datetime,
) -> ValidatedEvidence:
    problems = find_evidence_problems(plan, evidence, now)
    if problems:
        raise AgentError.validation(
            "EVIDENCE_INSUFFICIENT",
            retryable=False,
            failure_stage="evidence_validator",
        )
    return ValidatedEvidence.from_collected(evidence, reason_codes=["REQUIRED_EVIDENCE_PRESENT"])


async def repair_response(
    models: ModelGateway,
    draft: ResponseDraft,
    validation: ValidationResult,
    evidence: ValidatedEvidence,
) -> ResponseDraft:
    request = build_repair_request(
        draft=draft,
        failed_criteria=validation.failed_criteria,
        evidence=evidence,
    )
    return await models.structured("response_generator", request, ResponseDraft)


async def validate_response(
    models: ModelGateway,
    draft: ResponseDraft,
    evidence: ValidatedEvidence,
    assurance_mode: str,
) -> ValidationResult:
    deterministic = run_deterministic_checks(draft)
    if deterministic.has_hard_failure:
        return ValidationResult.from_deterministic(deterministic)
    primary = await models.structured("response_judge", build_judge_request(draft, evidence), JudgeVerdict)
    if assurance_mode == "bootstrap":
        return ValidationResult.from_single_judge(primary, assurance="reduced_assurance")
    secondary = await models.structured("response_judge_zh_verifier", build_judge_request(draft, evidence), JudgeVerdict)
    return ValidationResult.from_independent_judges(primary, secondary)
```

- [ ] **Step 4: Run node tests**

Run: `uv run pytest tests/unit/pipeline/test_nodes.py -v`

Expected: all tests pass; fake call lists prove no duplicate bootstrap judge, evidence sufficiency is enforced without a model guess, and repair never asks the judge to generate customer text.

- [ ] **Step 5: Commit**

```bash
git add src/agent_flow/pipeline tests/unit/pipeline/conftest.py tests/unit/pipeline/test_nodes.py
git commit -m "feat: add typed customer turn nodes"
```

---

### Task 8: Fixed TurnPipeline and Exact Failure Tracing

**Files:**
- Create: `src/agent_flow/pipeline/turn.py`
- Modify: `src/agent_flow/repositories/traces.py`
- Modify: `tests/unit/pipeline/conftest.py`
- Test: `tests/unit/pipeline/test_turn.py`
- Create: `tests/e2e/conftest.py`
- Test: `tests/e2e/test_turn_pipeline.py`
- Test: `tests/e2e/test_failure_locations.py`

**Interfaces:**
- Consumes: node functions, repositories, authorized context, `TurnRequest`.
- Produces: `TurnPipeline(traces, conversations, handoffs, models, rag, tools, clock, assurance_mode)` and `TurnPipeline.run(context, request, retry_of=None) -> TurnResult`.
- Produces: `run_node(state: TurnState, name: str, operation: Callable[[], T | Awaitable[T]], attempt: int = 1) -> T`. The operation is a zero-argument closure with all node-specific arguments bound explicitly at the call site; `run_node` never dispatches or resolves arguments by node-name strings.
- The API performs `input_gate` before calling this service. The pipeline records `context_loader` as its first runtime node and binds the immutable conversation/artifact snapshot to the trace before any model, RAG, or tool call.

- [ ] **Step 1: Write a failing happy-path and failure-location test**

```python
@pytest.mark.asyncio
async def test_run_node_wraps_explicit_sync_and_async_closures(
    pipeline_for_run_node,
    trace_state,
    trace_spy,
):
    seen = []

    def sync_operation():
        seen.append("sync")
        return "risk-result"

    async def async_operation():
        seen.append("async")
        return "model-result"

    assert await pipeline_for_run_node.run_node(trace_state, "risk_precheck", sync_operation) == "risk-result"
    assert await pipeline_for_run_node.run_node(trace_state, "dialogue_classifier", async_operation) == "model-result"
    assert seen == ["sync", "async"]
    assert trace_spy.completed_nodes == ["risk_precheck", "dialogue_classifier"]


@pytest.fixture
def context():
    return AuthorizedCustomerContext(subject_id="u1", tenant_id="t1", customer_id="c1")


@pytest_asyncio.fixture
async def pipeline(trace_repository, conversation_repository, fake_models, mock_rag, mock_tool, memory_handoffs, clock):
    return TurnPipeline(
        traces=trace_repository,
        conversations=conversation_repository,
        handoffs=memory_handoffs,
        models=fake_models,
        rag=mock_rag,
        tools=mock_tool,
        clock=clock,
        assurance_mode="bootstrap",
    )


@pytest.mark.asyncio
async def test_pipeline_returns_reduced_assurance_reply(pipeline, context, fake_models):
    result = await pipeline.run(context, TurnRequest(session_id="s1", message="查詢訂單 o1"))
    assert result.reply
    assert result.assurance == "reduced_assurance"
    assert result.handoff_status is None
    assert fake_models.calls == [
        "dialogue_classifier",
        "strategy_advisor",
        "response_generator",
        "response_judge",
    ]


@pytest.mark.asyncio
async def test_tool_timeout_trace_names_exact_operation(pipeline_with_tool_timeout, context):
    result = await pipeline_with_tool_timeout.run(context, TurnRequest(session_id="s1", message="查詢訂單 o1"))
    trace = await pipeline_with_tool_timeout.traces.get_trace(result.trace_id, tenant_id="t1")
    assert trace.issue_summary.failed_node == "evidence_collector"
    assert trace.issue_summary.component == "order_api"
    assert trace.issue_summary.operation == "order.lookup"


@pytest.mark.asyncio
async def test_insufficient_evidence_handoffs_at_validator(pipeline_with_expired_evidence, context):
    result = await pipeline_with_expired_evidence.run(
        context,
        TurnRequest(session_id="s1", message="查詢訂單 o1"),
    )
    trace = await pipeline_with_expired_evidence.traces.get_trace(result.trace_id, tenant_id="t1")
    assert result.handoff_status == "queued"
    assert result.reply is None
    assert trace.issue_summary.error_code == "EVIDENCE_INSUFFICIENT"
    assert trace.issue_summary.failed_node == "evidence_validator"


@pytest.mark.asyncio
async def test_second_validation_failure_handoffs(pipeline_with_double_validation_failure, context):
    result = await pipeline_with_double_validation_failure.run(
        context,
        TurnRequest(session_id="s1", message="查詢訂單 o1"),
    )
    trace = await pipeline_with_double_validation_failure.traces.get_trace(result.trace_id, tenant_id="t1")
    validator_spans = [span for span in trace.spans if span.node == "response_validator"]
    assert result.handoff_status == "queued"
    assert result.reply is None
    assert trace.issue_summary.error_code == "VALIDATION_EXHAUSTED"
    assert trace.issue_summary.failed_node == "response_validator"
    assert [span.attempt for span in validator_spans] == [1, 2]
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/unit/pipeline/test_turn.py tests/e2e/test_turn_pipeline.py tests/e2e/test_failure_locations.py -v`

Expected: FAIL because `TurnPipeline` does not exist.

- [ ] **Step 3: Implement explicit pipeline control flow**

```python
import asyncio
from collections.abc import Awaitable, Callable
from inspect import isawaitable
from typing import TypeVar


T = TypeVar("T")
NodeOperation = Callable[[], T | Awaitable[T]]


class TurnPipeline:
    async def run_node(
        self,
        state: TurnState,
        name: str,
        operation: NodeOperation[T],
        attempt: int = 1,
    ) -> T:
        span_id = await self.traces.start_span(state.trace_id, node=name, attempt=attempt)
        await self.traces.append_node_started(state.trace_id, span_id, node=name, attempt=attempt)
        try:
            pending_or_value = operation()
            value = await pending_or_value if isawaitable(pending_or_value) else pending_or_value
        except asyncio.CancelledError as error:
            await self.traces.finish_node_cancelled(state.trace_id, span_id, error)
            raise
        except Exception as error:
            await self.traces.finish_node_failed(state.trace_id, span_id, error)
            raise
        await self.traces.finish_node_completed(state.trace_id, span_id)
        return value

    async def run(self, context: AuthorizedCustomerContext, request: TurnRequest, retry_of: UUID | None = None) -> TurnResult:
        trace_id = await self.traces.start_trace_from_request(context, request, retry_of=retry_of)
        state = TurnState(trace_id=trace_id, context=context, request=request)
        try:
            state.snapshot = await self.run_node(
                state,
                "context_loader",
                lambda: (
                    self.conversations.get_retry_snapshot(
                        retry_of,
                        tenant_id=context.tenant_id,
                        customer_id=context.customer_id,
                    )
                    if retry_of is not None
                    else self.conversations.get_snapshot(
                        tenant_id=context.tenant_id,
                        customer_id=context.customer_id,
                        session_id=request.session_id,
                    )
                ),
            )
            state.classification = await self.run_node(
                state,
                "dialogue_classifier",
                lambda: classify_dialogue(self.models, state.snapshot.messages),
            )
            state.risk = await self.run_node(
                state,
                "risk_precheck",
                lambda: risk_precheck(state.classification, state.request.message),
            )
            if state.risk.requires_handoff:
                return await self.finish_handoff(state, reason_code=state.risk.reason_code)
            state.evidence_plan = await self.run_node(
                state,
                "evidence_planner",
                lambda: plan_evidence(state.classification),
            )
            state.collected_evidence = await self.run_node(
                state,
                "evidence_collector",
                lambda: collect_evidence(state.context, state.evidence_plan, self.rag, self.tools),
            )
            state.evidence = await self.run_node(
                state,
                "evidence_validator",
                lambda: validate_evidence(state.evidence_plan, state.collected_evidence, self.clock.now()),
            )
            state.strategy = await self.run_node(
                state,
                "strategy_selector",
                lambda: select_strategy(self.models, state.classification, state.risk, state.evidence),
            )
            state.draft = await self.run_node(
                state,
                "response_generator",
                lambda: generate_response(self.models, state.snapshot, state.strategy, state.evidence),
            )
            state.validation = await self.run_node(
                state,
                "response_validator",
                lambda: validate_response(self.models, state.draft, state.evidence, self.assurance_mode),
            )
            if not state.validation.passed:
                if state.validation.repairable:
                    state.draft = await self.run_node(
                        state,
                        "response_repair",
                        lambda: repair_response(self.models, state.draft, state.validation, state.evidence),
                    )
                    state.validation = await self.run_node(
                        state,
                        "response_validator",
                        lambda: validate_response(self.models, state.draft, state.evidence, self.assurance_mode),
                        attempt=2,
                    )
                if not state.validation.passed:
                    return await self.finish_handoff(
                        state,
                        reason_code="VALIDATION_EXHAUSTED",
                        failed_node="response_validator",
                    )
            return await self.finalize(state)
        except AgentError as error:
            return await self.fail_or_handoff(state, error)
```

`run_node` must always start/finish a span, append typed events for attempts and decisions, and set `primary_failure_event_id` only for the causal error. Downstream cancellations are separate events. `finish_handoff` records `VALIDATION_EXHAUSTED` against the second `response_validator` span when repair was attempted; it must never finalize or persist a still-failing draft as an assistant reply.

- [ ] **Step 4: Run E2E and failure-injection tests**

Run: `uv run pytest tests/unit/pipeline/test_turn.py tests/e2e/test_turn_pipeline.py tests/e2e/test_failure_locations.py -v`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/agent_flow/pipeline/turn.py src/agent_flow/repositories/traces.py tests/unit/pipeline tests/e2e
git commit -m "feat: add fixed traced turn pipeline"
```

---

### Task 9: FastAPI Turns, Trace APIs, Health, and Manual Retry

**Files:**
- Create: `src/agent_flow/main.py`
- Create: `src/agent_flow/api/dependencies.py`
- Create: `src/agent_flow/api/turns.py`
- Create: `src/agent_flow/api/traces.py`
- Create: `src/agent_flow/api/health.py`
- Test: `tests/e2e/test_api.py`
- Test: `tests/e2e/test_manual_retry.py`

**Interfaces:**
- Consumes: `TurnPipeline`, auth claims, trace/conversation repositories.
- Produces: `POST /api/v1/turns`, `GET /api/v1/traces/{id}`, incremental events, `POST /retry`, liveness/readiness.

- [ ] **Step 1: Write failing API authorization and retry tests**

```python
def test_cross_customer_request_stops_before_pipeline(client, pipeline_spy, token_for_c1):
    response = client.post(
        "/api/v1/turns",
        headers={"Authorization": f"Bearer {token_for_c1}"},
        json={"customer_id": "c2", "session_id": "s1", "message": "hello"},
    )
    assert response.status_code in (403, 404)
    assert pipeline_spy.calls == []


def test_manual_retry_creates_linked_review_only_trace(client, admin_token, failed_trace):
    response = client.post(
        f"/api/v1/traces/{failed_trace}/retry",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"reason": "model endpoint recovered"},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["retry_of_trace_id"] == str(failed_trace)
    assert body["delivery_disposition"] == "review_required"
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/e2e/test_api.py tests/e2e/test_manual_retry.py -v`

Expected: FAIL because the FastAPI application is missing.

- [ ] **Step 3: Implement dependency wiring and tenant-safe routes**

```python
@router.post("/api/v1/traces/{trace_id}/retry", status_code=202)
async def retry_trace(
    trace_id: UUID,
    command: ManualRetryCommand,
    principal: Annotated[AuthenticatedPrincipal, Depends(require_admin)],
    service: Annotated[ManualRetryService, Depends(get_retry_service)],
) -> ManualRetryAccepted:
    return await service.retry_entire_turn(trace_id, principal, command.reason)
```

The service rejects active traces, cross-tenant access, expired snapshots, unresolved artifact versions, blank reasons, and more than three retries per root trace. It reuses captured conversation/artifact versions, refreshes read-only tools, suppresses duplicate handoff idempotency keys, and never automatically sends the replay reply.

- [ ] **Step 4: Run API tests**

Run: `uv run pytest tests/e2e/test_api.py tests/e2e/test_manual_retry.py -v`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/agent_flow/main.py src/agent_flow/api tests/e2e/test_api.py tests/e2e/test_manual_retry.py
git commit -m "feat: expose authorized turn trace and retry APIs"
```

---

### Task 10: Handoff Outbox and Worker

**Files:**
- Create: `src/agent_flow/adapters/webhook.py`
- Create: `src/agent_flow/repositories/outbox.py`
- Create: `src/agent_flow/worker.py`
- Test: `tests/integration/test_handoff_outbox.py`

**Interfaces:**
- Consumes: `HandoffEvent`, PostgreSQL outbox rows, Webhook URL/secret.
- Produces: idempotent `OutboxRepository.enqueue/claim/complete/fail`, HMAC Webhook delivery, worker loop.

- [ ] **Step 1: Write failing idempotency and signature tests**

```python
@pytest.mark.asyncio
async def test_duplicate_handoff_is_not_enqueued_twice(outbox, handoff_event):
    first = await outbox.enqueue(handoff_event)
    second = await outbox.enqueue(handoff_event)
    assert first.id == second.id


def test_signature_uses_timestamp_and_raw_body():
    signature = sign_webhook(b"{\"trace_id\":\"t1\"}", "1700000000", b"secret")
    assert signature.startswith("sha256=")
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/integration/test_handoff_outbox.py -v`

Expected: FAIL because outbox/worker code is missing.

- [ ] **Step 3: Implement SKIP LOCKED claims and bounded delivery**

```sql
SELECT id, payload, attempts
FROM notification.outbox
WHERE status IN ('queued', 'failed') AND next_attempt_at <= now()
ORDER BY created_at
FOR UPDATE SKIP LOCKED
LIMIT %s;
```

Sign `timestamp + "." + raw_body` with HMAC-SHA256. Send `X-Agent-Timestamp`, `X-Agent-Signature`, and `Idempotency-Key`. Retry timeout/408/429/5xx with bounded backoff; do not retry other 4xx. Record exact HTTP/error metadata without secrets.

- [ ] **Step 4: Run integration tests**

Run: `uv run pytest tests/integration/test_handoff_outbox.py -v`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/agent_flow/adapters/webhook.py src/agent_flow/repositories/outbox.py src/agent_flow/worker.py tests/integration/test_handoff_outbox.py
git commit -m "feat: deliver idempotent handoff outbox events"
```

---

### Task 11: Retention, Structured Logs, and Bootstrap Acceptance Suite

**Files:**
- Modify: `src/agent_flow/worker.py`
- Create: `src/agent_flow/logging.py`
- Create: `tests/e2e/test_bootstrap_acceptance.py`
- Create: `tests/integration/test_retention.py`
- Create: `tests/unit/test_secret_filter.py`
- Create: `tests/live/test_local_inventory.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Consumes: expired rows and typed trace context.
- Produces: JSON stdout logs, 30/180-day cleanup jobs, bootstrap acceptance evidence.

- [ ] **Step 1: Write failing retention and secret tests**

```python
def test_secret_filter_removes_headers_and_connections():
    payload = {
        "Authorization": "Bearer secret",
        "Cookie": "sid=secret",
        "database_url": "postgresql://secret",
        "trace_id": "t1",
    }
    assert filter_secrets(payload) == {"trace_id": "t1"}


@pytest.mark.asyncio
async def test_retention_deletes_turns_before_structured_trace(retention_fixture):
    await retention_fixture.run(now=retention_fixture.day_31)
    assert await retention_fixture.raw_turn_exists() is False
    assert await retention_fixture.trace_exists() is True
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/unit/test_secret_filter.py tests/integration/test_retention.py -v`

Expected: FAIL because logging/retention code is missing.

- [ ] **Step 3: Implement JSON stdout and cleanup jobs**

`filter_secrets` removes case-insensitive Authorization, Cookie, password, API-key, token, and connection-string fields recursively. Raw text remains only in `runtime.turns`; stdout/events contain bounded summaries and references. Retention jobs hard-delete expired turns at 30 days and structured trace/event/span data at 180 days in bounded batches.

- [ ] **Step 4: Run full offline bootstrap acceptance**

Run: `uv run pytest -v`

Expected: all tests pass; ordinary suite makes no live model/RAG/tool/Webhook calls.

- [ ] **Step 5: Run opt-in local inventory and capability smoke**

```python
# append to tests/conftest.py
import pytest

from agent_flow.config import Settings, load_model_config
from agent_flow.model_registry import ModelInventoryProbe, ModelRegistry


def pytest_addoption(parser):
    parser.addoption("--run-live-local-model", action="store_true", default=False)


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-live-local-model"):
        return
    marker = pytest.mark.skip(reason="requires --run-live-local-model")
    for item in items:
        if "live_local_model" in item.keywords:
            item.add_marker(marker)


@pytest.fixture
def live_inventory_probe():
    settings = Settings(
        database_url="postgresql://unused/unused",
        local_vllm_base_url="http://localhost:8000/v1",
    )
    registry = ModelRegistry(load_model_config(settings.model_config_path), settings)
    return ModelInventoryProbe(registry)
```

```python
# tests/live/test_local_inventory.py
@pytest.mark.live_local_model
@pytest.mark.asyncio
async def test_live_local_generator_inventory_and_schema(live_inventory_probe):
    result = await live_inventory_probe.probe_role("response_generator")
    assert result.available is True
    assert result.model == "Qwen/Qwen3-8B"
    assert {"chat", "structured_json", "reasoning_toggle"} <= result.verified_capabilities
```

Run: `uv run pytest tests/live/test_local_inventory.py -v --run-live-local-model`

Expected: exact model inventory passes for `Qwen/Qwen3-8B`, and one bounded `ResponseDraft` schema generation with thinking disabled verifies the live capabilities required by generation and repair.

- [ ] **Step 6: Commit**

```bash
git add src/agent_flow/logging.py src/agent_flow/worker.py tests
git commit -m "test: add retention logging and bootstrap acceptance"
```

---

### Task 12: Container Delivery and Runtime README

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`
- Create: `compose.yaml`
- Create: `README.md`
- Create: `src/agent_flow/seed_demo.py`
- Modify: `.env.example`
- Test: `tests/e2e/test_compose_config.py`

**Interfaces:**
- Consumes: locked application, PostgreSQL image, host vLLM, remote model endpoint.
- Produces: `postgres`, `migrate`, `app`, `worker`, and `demo-seed` Compose services plus complete bootstrap operating instructions.

- [ ] **Step 1: Write a failing Compose policy test**

```python
def test_compose_uses_host_gateway_not_container_localhost(compose_config):
    app = compose_config["services"]["app"]
    assert app["environment"]["LOCAL_VLLM_BASE_URL"] == "http://host.docker.internal:8000/v1"
    assert "host.docker.internal:host-gateway" in app["extra_hosts"]
    assert compose_config["services"]["postgres"]["image"].startswith("pgvector/pgvector:")
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/e2e/test_compose_config.py -v`

Expected: FAIL because container files do not exist.

- [ ] **Step 3: Create pinned, non-root container delivery**

```dockerfile
# Dockerfile
FROM ghcr.io/astral-sh/uv:0.11.29-python3.12-bookworm-slim
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
RUN groupadd --system agent && useradd --system --gid agent --home /app agent
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev && chown -R agent:agent /app
USER agent
EXPOSE 8080
CMD ["uv", "run", "uvicorn", "agent_flow.main:app", "--host", "0.0.0.0", "--port", "8080"]
```

```yaml
volumes:
  postgres-data:

networks:
  agent-net:
    driver: bridge

services:
  postgres:
    image: pgvector/pgvector:0.8.5-pg16-bookworm
    environment:
      POSTGRES_DB: agent
      POSTGRES_USER: agent
      POSTGRES_PASSWORD: agent
    volumes:
      - postgres-data:/var/lib/postgresql/data
    networks: [agent-net]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U agent -d agent"]
      interval: 5s
      timeout: 3s
      retries: 20
  migrate:
    build: .
    command: ["uv", "run", "alembic", "upgrade", "head"]
    env_file: [.env]
    environment:
      DATABASE_URL: postgresql://agent:agent@postgres:5432/agent
    networks: [agent-net]
    depends_on:
      postgres:
        condition: service_healthy
  app:
    build: .
    command: ["uv", "run", "uvicorn", "agent_flow.main:app", "--host", "0.0.0.0", "--port", "8080"]
    env_file: [.env]
    environment:
      DATABASE_URL: postgresql://agent:agent@postgres:5432/agent
      LOCAL_VLLM_BASE_URL: http://host.docker.internal:8000/v1
    extra_hosts:
      - host.docker.internal:host-gateway
    ports:
      - "8080:8080"
    networks: [agent-net]
    depends_on:
      migrate:
        condition: service_completed_successfully
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/v1/health/live')"]
      interval: 10s
      timeout: 3s
      retries: 10
  worker:
    build: .
    command: ["uv", "run", "python", "-m", "agent_flow.worker"]
    env_file: [.env]
    environment:
      DATABASE_URL: postgresql://agent:agent@postgres:5432/agent
      LOCAL_VLLM_BASE_URL: http://host.docker.internal:8000/v1
    extra_hosts:
      - host.docker.internal:host-gateway
    networks: [agent-net]
    depends_on:
      migrate:
        condition: service_completed_successfully
  demo-seed:
    build: .
    profiles: [demo]
    command: ["uv", "run", "python", "-m", "agent_flow.seed_demo"]
    env_file: [.env]
    environment:
      DATABASE_URL: postgresql://agent:agent@postgres:5432/agent
    networks: [agent-net]
    depends_on:
      migrate:
        condition: service_completed_successfully
```

Create `.dockerignore` with `.git`, `.env`, `.venv`, `.pytest_cache`, `.superpowers`, `.agents`, `__pycache__`, and `*.pyc`. Add `src/agent_flow/seed_demo.py` to load only committed mock RAG/tool fixtures. Do not add Redis, RabbitMQ, Kafka, Celery, OTLP, or a frontend framework.

- [ ] **Step 4: Write the bootstrap README**

Use these README headings and runnable command blocks:

```markdown
# Agent Flow Bootstrap Runtime
## Scope and Reduced-Assurance Warning
## Prerequisites
## Host Development
### `Copy-Item .env.example .env`
### `uv sync --frozen`
### `Invoke-RestMethod http://localhost:8000/v1/models`
### `uv run alembic upgrade head`
### `uv run uvicorn agent_flow.main:app --reload`
### `uv run python -m agent_flow.worker`
## Docker Compose
### Why Compose uses host.docker.internal
### `docker compose up --build`
### `docker compose --profile demo run --rm demo-seed`
## Model Registry and Inventory Gate
## Turn, Trace, Event, Health, and Manual Retry APIs
## Authorization and Tenant Binding
## Retry and Handoff Outbox
## Retention and Structured Logs
## Test Commands
## Troubleshooting
## Deferred: Incident-first Console, Dual Judge, Improvement Lifecycle
```

Under the Model Registry heading document that `response_generator` requires `chat`, `structured_json`, and `reasoning_toggle`; show the opt-in live probe command and explain that a model name match without a passing `ResponseDraft` schema probe is not ready. Under the API heading include executable PowerShell `Invoke-RestMethod` examples for a successful turn, trace retrieval, incremental events, and admin manual retry. Under troubleshooting include exact checks for vLLM `/v1/models`, structured-output `response_format` rejection or invalid JSON, Compose host routing, remote Ollama tags/show, pgvector extension, migration state, semaphore saturation, and failed outbox rows.

- [ ] **Step 5: Validate tests and Compose rendering**

Run: `uv run pytest tests/e2e/test_compose_config.py -v && docker compose config --quiet`

Expected: test passes and Compose exits 0.

- [ ] **Step 6: Commit**

```bash
git add Dockerfile .dockerignore compose.yaml README.md .env.example tests/e2e/test_compose_config.py
git commit -m "docs: containerize and document bootstrap runtime"
```

---

## Final Verification

- [ ] Run `uv run pytest -v` and confirm zero failures/skips except explicitly marked live-remote tests.
- [ ] Run `uv run alembic upgrade head` against a clean pgvector database.
- [ ] Run `docker compose config --quiet`.
- [ ] Run the local inventory probe and confirm exact `Qwen/Qwen3-8B`.
- [ ] With remote `.env` populated, run opt-in inventory/capability probes for `qwen3.5:9b` and `qwen3:embedding:0.6b`.
- [ ] Send one mock successful turn, one tool-timeout turn, one high-risk handoff, and one manual retry; confirm each Trace API response identifies the exact stage/operation and immutable retry lineage.
- [ ] Confirm the bootstrap response and trace expose `reduced_assurance` and only one semantic judge call.
- [ ] Confirm no raw chain-of-thought, Authorization, Cookie, API key, or database URL appears in stdout or trace events.

## Follow-on Plans

1. **Incident-first Demo Console:** horizontal clickable flow, default failed-node expansion, typed node details, incremental polling, filters, attempt timeline, and manual-retry UI.
2. **Evaluation and Improvement:** 60-item emotion labeling, safety/grounding golden sets, append-only improvement ledger, bootstrap human-only semantic approval, future Gemma/Qwen dual judging, atomic activation, and rollback.
