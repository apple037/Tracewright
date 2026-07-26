"""Live prompt editing, and the traceability that has to survive it.

An edit must change the artifact checksum, because that checksum is what a trace
records — otherwise two different prompts would be indistinguishable after the
fact. Reverting must restore the original exactly.
"""

import shutil
from pathlib import Path

import pytest

from agent_flow.artifacts import load_runtime_artifacts
from agent_flow.config import Settings, load_model_config
from agent_flow.runtime_config import RuntimeConfigService


@pytest.fixture
def config_root(tmp_path):
    root = tmp_path / "config"
    shutil.copytree(Path("config"), root)
    (root / "overrides.json").unlink(missing_ok=True)
    return root


@pytest.fixture
def service(config_root):
    settings = Settings(database_url="postgresql://unused/unused")
    return RuntimeConfigService(
        config_root, load_model_config(settings.model_config_path), settings
    )


def test_editing_a_prompt_changes_the_checksum_and_reverting_restores_it(service):
    original = service.artifacts().prompts_by_node["response_generator"]

    edited = service.set_prompt("response_generator", "Only ever answer in haiku.")
    assert edited["checksum"] != original.ref.checksum
    assert edited["edited"] is True
    live = service.artifacts().prompts_by_node["response_generator"]
    assert live.system_prompt == "Only ever answer in haiku."

    reverted = service.clear_prompt("response_generator")
    assert reverted["checksum"] == original.ref.checksum
    assert reverted["edited"] is False
    assert service.artifacts().prompts_by_node["response_generator"].system_prompt == (
        original.system_prompt
    )


def test_an_override_never_rewrites_the_yaml_file(service, config_root):
    path = config_root / "prompts" / "response_generator.v1.yaml"
    before = path.read_text(encoding="utf-8")

    service.set_prompt("response_generator", "changed")

    # The comments in these files are the documentation a non-coder reads; an
    # override must not touch them.
    assert path.read_text(encoding="utf-8") == before
    assert (config_root / "overrides.json").is_file()


def test_edits_survive_a_fresh_load(service, config_root):
    service.set_persona("familiar_companion.zh-TW", "Be extremely terse.")

    reloaded = load_runtime_artifacts(config_root)
    persona = next(
        p for p in reloaded.personas if p.artifact_id == "familiar_companion.zh-TW"
    )
    assert persona.style_prompt == "Be extremely terse."
    assert reloaded.overridden["familiar_companion.zh-TW"] == ("style_prompt",)


def test_unknown_targets_are_rejected(service):
    with pytest.raises(KeyError):
        service.set_prompt("no_such_node", "x")
    with pytest.raises(KeyError):
        service.set_persona("no_such_persona", "x")


def test_prompts_are_discovered_from_disk_not_hardcoded(config_root):
    artifacts = load_runtime_artifacts(config_root)
    # Dropping a YAML in config/prompts is enough; nothing lists filenames.
    assert set(artifacts.prompts_by_node) >= {
        "dialogue_classifier",
        "strategy_selector",
        "response_generator",
        "response_judge",
    }
    assert artifacts.system_prompt_for("response_generator")
    assert artifacts.system_prompt_for("no_such_node") is None
