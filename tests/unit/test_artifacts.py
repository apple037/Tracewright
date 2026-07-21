import os
from pathlib import Path
import subprocess
import sys

import pytest

from agent_flow.artifacts import ArtifactRegistry, resolve_persona
from agent_flow.contracts import ArtifactRef, ConversationMode


def test_persona_artifact_is_versioned_and_checksum_is_stable():
    registry = ArtifactRegistry(Path("config/personas"))
    first = registry.load_persona("familiar_companion.zh-TW.v1.yaml")
    second = registry.load_persona("familiar_companion.zh-TW.v1.yaml")

    assert first.ref == ArtifactRef(
        artifact_id="familiar_companion.zh-TW",
        version="1.0.0",
        checksum=first.ref.checksum,
    )
    assert first.ref.checksum == second.ref.checksum
    assert len(first.ref.checksum) == 64


def test_prompt_artifacts_are_versioned_and_node_scoped():
    registry = ArtifactRegistry(Path("config/prompts"))
    strategy = registry.load_prompt("strategy_selector.v1.yaml")
    response = registry.load_prompt("response_generator.v1.yaml")
    assert strategy.artifact_id == strategy.node == "strategy_selector"
    assert response.artifact_id == response.node == "response_generator"
    assert strategy.version == response.version == "1.0.0"
    assert strategy.ref.checksum != response.ref.checksum


def test_persona_applies_only_to_companion_modes():
    persona = ArtifactRegistry(Path("config/personas")).load_persona(
        "familiar_companion.zh-TW.v1.yaml"
    )

    assert resolve_persona(ConversationMode.EMOTIONAL_SUPPORT, [persona]) == persona
    assert resolve_persona(ConversationMode.CASUAL, [persona]) == persona
    assert resolve_persona(ConversationMode.TRANSACTIONAL_READ, [persona]) is None
    assert resolve_persona(ConversationMode.INFORMATIONAL, [persona]) is None
    assert resolve_persona(ConversationMode.COMPLAINT, [persona]) is None
    assert resolve_persona(ConversationMode.BOUNDARY, [persona]) is None
    assert resolve_persona(ConversationMode.UNKNOWN, [persona]) is None


def test_artifact_registry_rejects_path_traversal():
    registry = ArtifactRegistry(Path("config/personas"))

    with pytest.raises(ValueError, match="artifact path"):
        registry.load_persona("../models.bootstrap.example.yaml")


def test_artifact_contract_rejects_unknown_fields(tmp_path):
    source = Path("config/personas/familiar_companion.zh-TW.v1.yaml").read_text(
        encoding="utf-8"
    )
    artifact = tmp_path / "persona.yaml"
    artifact.write_text(source + "\nunexpected_field: true\n", encoding="utf-8")

    with pytest.raises(ValueError):
        ArtifactRegistry(tmp_path).load_persona(artifact.name)


def test_artifact_contract_rejects_non_semantic_version(tmp_path):
    source = Path("config/personas/familiar_companion.zh-TW.v1.yaml").read_text(
        encoding="utf-8"
    )
    artifact = tmp_path / "persona.yaml"
    artifact.write_text(
        source.replace("version: 1.0.0", "version: latest"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        ArtifactRegistry(tmp_path).load_persona(artifact.name)


def test_artifact_checksum_is_cross_process_stable():
    program = (
        "from pathlib import Path; "
        "from agent_flow.artifacts import ArtifactRegistry; "
        "print(ArtifactRegistry(Path('config/personas'))."
        "load_persona('familiar_companion.zh-TW.v1.yaml').ref.checksum)"
    )
    checksums = []
    for seed in ("1", "98765"):
        env = os.environ.copy()
        env["PYTHONHASHSEED"] = seed
        checksums.append(
            subprocess.check_output(
                [sys.executable, "-c", program],
                env=env,
                text=True,
            ).strip()
        )

    assert checksums[0] == checksums[1]
