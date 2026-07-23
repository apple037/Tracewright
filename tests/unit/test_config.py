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
    assert config.profiles[config.roles["response_generator"]].model == "Qwen/Qwen3-8B-AWQ"
    assert "structured_json" in config.profiles[config.roles["response_generator"]].capabilities
    assert config.profiles[config.roles["dialogue_classifier"]].model == "qwen3.5:9b"
    assert config.profiles[config.roles["embedding"]].model == "qwen3-embedding-0.6b"
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


def test_profile_concurrency_is_an_independent_ceiling_from_endpoint():
    config = ModelConfig.model_validate(
        {
            "endpoints": {
                "shared": {
                    "adapter": "openai_compatible",
                    "base_url_env": "LOCAL_VLLM_BASE_URL",
                    "max_concurrency": 1,
                }
            },
            "profiles": {
                "wide": {
                    "endpoint": "shared",
                    "model": "test-model",
                    "family": "test",
                    "capabilities": ["chat"],
                    "max_concurrency": 3,
                }
            },
            "roles": {},
            "mode": "bootstrap",
            "disabled_roles": [],
            "promotion_semantic_mode": "human_only",
        }
    )

    assert config.endpoints["shared"].max_concurrency == 1
    assert config.profiles["wide"].max_concurrency == 3


def _dual_judge_config():
    config = load_model_config(Path("config/models.bootstrap.example.yaml"))
    data = config.model_dump(mode="python")
    data["mode"] = "dual_judge"
    data["disabled_roles"].discard("response_judge_zh_verifier")
    data["profiles"]["zh_verifier"] = {
        **data["profiles"]["remote_structured"],
        "model": "independent-verifier",
        "family": "independent-family",
    }
    data["roles"]["response_judge_zh_verifier"] = "zh_verifier"
    return data


def test_dual_judge_config_rejects_same_profile_alias():
    data = _dual_judge_config()
    data["roles"]["response_judge_zh_verifier"] = data["roles"]["response_judge"]
    with pytest.raises(ValueError, match="different profiles"):
        ModelConfig.model_validate(data)


def test_dual_judge_config_rejects_same_declared_family():
    data = _dual_judge_config()
    data["profiles"]["zh_verifier"]["family"] = data["profiles"]["remote_structured"]["family"]
    with pytest.raises(ValueError, match="different families"):
        ModelConfig.model_validate(data)


def test_dual_judge_config_accepts_distinct_profiles_and_families():
    config = ModelConfig.model_validate(_dual_judge_config())
    assert config.mode == "dual_judge"


def test_dual_judge_config_requires_both_judges_enabled():
    data = _dual_judge_config()
    data["disabled_roles"].add("response_judge_zh_verifier")
    with pytest.raises(ValueError, match="enabled"):
        ModelConfig.model_validate(data)
