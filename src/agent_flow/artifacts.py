import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from agent_flow.contracts import ConversationMode, PersonaArtifact, PromptArtifact


OVERRIDES_FILENAME = "overrides.json"

# Fields the console is allowed to edit at runtime, per artifact kind. Anything
# else stays file-only: tone is safe to tune live, guardrails are not.
EDITABLE_PROMPT_FIELDS = frozenset({"system_prompt"})
EDITABLE_PERSONA_FIELDS = frozenset({"style_prompt"})


def load_overrides(config_root: Path) -> dict[str, dict[str, dict[str, str]]]:
    """Console edits, kept out of the YAML so its comments survive.

    Editing through the console never rewrites the hand-written config files —
    it layers on top of them, so "revert" is just deleting an entry.
    """
    path = config_root / OVERRIDES_FILENAME
    if not path.is_file():
        return {"prompts": {}, "personas": {}}
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"prompts": {}, "personas": {}}
    return {
        "prompts": dict(stored.get("prompts") or {}),
        "personas": dict(stored.get("personas") or {}),
    }


def save_overrides(
    config_root: Path, overrides: Mapping[str, Mapping[str, Mapping[str, str]]]
) -> None:
    path = config_root / OVERRIDES_FILENAME
    path.write_text(
        json.dumps(
            {
                "prompts": dict(overrides.get("prompts") or {}),
                "personas": dict(overrides.get("personas") or {}),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


class ArtifactRegistry:
    def __init__(self, root: Path, overrides: Mapping[str, Mapping[str, str]] | None = None):
        self.root = root.resolve()
        # Keyed by artifact_id. Applied before validation so the checksum — and
        # therefore the prompt_ref recorded on every trace — reflects what
        # actually ran, not what is on disk.
        self.overrides = dict(overrides or {})

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
        editable = (
            EDITABLE_PROMPT_FIELDS
            if artifact_type is PromptArtifact
            else EDITABLE_PERSONA_FIELDS
        )
        override = self.overrides.get(str(payload.get("artifact_id", "")), {})
        for key, value in override.items():
            if key in editable:
                payload[key] = value
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
    # Every prompt found on disk, keyed by its `node:` field. Nodes that have no
    # prompt file simply get None and fall back to schema-only instructions.
    prompts_by_node: Mapping[str, PromptArtifact] = field(default_factory=dict)
    # artifact_id -> set of fields currently coming from overrides.json rather
    # than the YAML, so the console can show "edited" and offer a revert.
    overridden: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def prompt_for(self, node: str) -> PromptArtifact | None:
        return self.prompts_by_node.get(node)

    def system_prompt_for(self, node: str) -> str | None:
        prompt = self.prompts_by_node.get(node)
        return prompt.system_prompt if prompt and prompt.system_prompt else None


def _yaml_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if path.suffix in {".yaml", ".yml"} and path.is_file()
    )


def load_runtime_artifacts(config_root: Path) -> RuntimeArtifacts:
    # Drop a YAML into config/prompts or config/personas and it is picked up —
    # no code change. Prompts are keyed by their `node:` field, personas by the
    # conversation modes they declare in `applies_to`.
    overrides = load_overrides(config_root)
    prompt_registry = ArtifactRegistry(config_root / "prompts", overrides["prompts"])
    persona_registry = ArtifactRegistry(config_root / "personas", overrides["personas"])

    prompts: dict[str, PromptArtifact] = {}
    for path in _yaml_files(config_root / "prompts"):
        prompt = prompt_registry.load_prompt(path.name)
        if prompt.node in prompts:
            raise ValueError(f"duplicate prompt for node {prompt.node}: {path.name}")
        prompts[prompt.node] = prompt

    personas = tuple(
        persona_registry.load_persona(path.name)
        for path in _yaml_files(config_root / "personas")
    )

    for required in ("strategy_selector", "response_generator"):
        if required not in prompts:
            # FileNotFoundError, not ValueError: readiness reports an absent
            # config as "missing" and a malformed one as "invalid".
            raise FileNotFoundError(f"no prompt file defines node {required}")

    overridden = {
        artifact_id: tuple(sorted(fields))
        for section in ("prompts", "personas")
        for artifact_id, fields in overrides[section].items()
        if fields
    }
    return RuntimeArtifacts(
        strategy_prompt=prompts["strategy_selector"],
        response_prompt=prompts["response_generator"],
        personas=personas,
        prompts_by_node=prompts,
        overridden=overridden,
    )
