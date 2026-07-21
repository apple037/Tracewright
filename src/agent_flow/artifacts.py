import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from agent_flow.contracts import ConversationMode, PersonaArtifact, PromptArtifact


class ArtifactRegistry:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def _load(
        self,
        filename: str,
        artifact_type: type[PersonaArtifact] | type[PromptArtifact],
    ) -> PersonaArtifact | PromptArtifact:
        relative = Path(filename)
        if relative.name != filename or relative.suffix not in {".yaml", ".yml"}:
            raise ValueError("invalid artifact path")
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("invalid artifact path")

        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        artifact = artifact_type.model_validate(payload)
        canonical = json.dumps(
            artifact.model_dump(
                mode="json",
                exclude={"checksum", "ref"},
            ),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        checksum = hashlib.sha256(canonical).hexdigest()
        return artifact.model_copy(update={"checksum": checksum})

    def load_persona(self, filename: str) -> PersonaArtifact:
        artifact = self._load(filename, PersonaArtifact)
        if not isinstance(artifact, PersonaArtifact):
            raise TypeError("expected persona artifact")
        return artifact

    def load_prompt(self, filename: str) -> PromptArtifact:
        artifact = self._load(filename, PromptArtifact)
        if not isinstance(artifact, PromptArtifact):
            raise TypeError("expected prompt artifact")
        return artifact


def resolve_persona(
    conversation_mode: ConversationMode,
    personas: Sequence[PersonaArtifact],
) -> PersonaArtifact | None:
    matches = [persona for persona in personas if conversation_mode in persona.applies_to]
    if len(matches) > 1:
        raise ValueError(f"multiple personas apply to {conversation_mode.value}")
    return matches[0] if matches else None


@dataclass(frozen=True)
class RuntimeArtifacts:
    strategy_prompt: PromptArtifact
    response_prompt: PromptArtifact
    personas: tuple[PersonaArtifact, ...]


def load_runtime_artifacts(config_root: Path) -> RuntimeArtifacts:
    prompt_registry = ArtifactRegistry(config_root / "prompts")
    persona_registry = ArtifactRegistry(config_root / "personas")
    return RuntimeArtifacts(
        strategy_prompt=prompt_registry.load_prompt("strategy_selector.v1.yaml"),
        response_prompt=prompt_registry.load_prompt("response_generator.v1.yaml"),
        personas=(
            persona_registry.load_persona("familiar_companion.zh-TW.v1.yaml"),
        ),
    )
