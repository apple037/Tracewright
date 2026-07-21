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
        roles = sorted(self.roles.items(), key=lambda item: item[0] != "response_generator")
        for role, profile_name in roles:
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
