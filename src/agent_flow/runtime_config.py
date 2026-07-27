"""Live view of the runtime's editable configuration.

Holds the currently-loaded artifacts and reloads them after an edit, so a
changed prompt takes effect on the next turn without restarting the process.
"""

from __future__ import annotations

from pathlib import Path

from agent_flow.artifacts import (
    OVERRIDES_FILENAME,
    RuntimeArtifacts,
    load_overrides,
    load_runtime_artifacts,
    save_overrides,
)
from agent_flow.config import ModelConfig, Settings


class RuntimeConfigService:
    def __init__(
        self, config_root: Path, model_config: ModelConfig, settings: Settings
    ) -> None:
        self._root = config_root
        self._model_config = model_config
        self._settings = settings
        self._artifacts = load_runtime_artifacts(config_root)
        self._overrides_stamp = self._stamp()

    def _stamp(self) -> tuple[int, int] | None:
        # Size as well as mtime: two edits within the same clock tick are
        # otherwise indistinguishable.
        try:
            stat = (self._root / OVERRIDES_FILENAME).stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def artifacts(self) -> RuntimeArtifacts:
        # The worker runs the pipeline in its own process and never writes, so
        # without this it served the prompts it booted with and every console
        # edit was silently ignored — while the app reported the edit applied.
        stamp = self._stamp()
        if stamp != self._overrides_stamp:
            self._overrides_stamp = stamp
            self._reload()
        return self._artifacts

    def _reload(self) -> RuntimeArtifacts:
        # Reload before publishing: a malformed override must not take down the
        # pipeline, so validation failure leaves the previous artifacts in place.
        self._artifacts = load_runtime_artifacts(self._root)
        return self._artifacts

    def _artifact_id_for_node(self, node: str) -> str:
        prompt = self._artifacts.prompts_by_node.get(node)
        if prompt is None:
            raise KeyError(node)
        return prompt.artifact_id

    def _persona_or_raise(self, artifact_id: str) -> None:
        if not any(p.artifact_id == artifact_id for p in self._artifacts.personas):
            raise KeyError(artifact_id)

    def _write(self, section: str, artifact_id: str, values: dict[str, str] | None):
        overrides = load_overrides(self._root)
        if values is None:
            overrides[section].pop(artifact_id, None)
        else:
            overrides[section][artifact_id] = values
        save_overrides(self._root, overrides)
        self._reload()
        self._overrides_stamp = self._stamp()

    def set_prompt(self, node: str, system_prompt: str) -> dict[str, object]:
        artifact_id = self._artifact_id_for_node(node)
        self._write("prompts", artifact_id, {"system_prompt": system_prompt})
        prompt = self._artifacts.prompts_by_node[node]
        return {
            "node": node,
            "checksum": prompt.ref.checksum,
            "system_prompt": prompt.system_prompt,
            "edited": True,
        }

    def clear_prompt(self, node: str) -> dict[str, object]:
        artifact_id = self._artifact_id_for_node(node)
        self._write("prompts", artifact_id, None)
        prompt = self._artifacts.prompts_by_node[node]
        return {
            "node": node,
            "checksum": prompt.ref.checksum,
            "system_prompt": prompt.system_prompt,
            "edited": False,
        }

    def set_persona(self, artifact_id: str, style_prompt: str) -> dict[str, object]:
        self._persona_or_raise(artifact_id)
        self._write("personas", artifact_id, {"style_prompt": style_prompt})
        return self._persona_summary(artifact_id, edited=True)

    def clear_persona(self, artifact_id: str) -> dict[str, object]:
        self._persona_or_raise(artifact_id)
        self._write("personas", artifact_id, None)
        return self._persona_summary(artifact_id, edited=False)

    def _persona_summary(self, artifact_id: str, *, edited: bool) -> dict[str, object]:
        persona = next(
            p for p in self._artifacts.personas if p.artifact_id == artifact_id
        )
        return {
            "artifact_id": artifact_id,
            "checksum": persona.ref.checksum,
            "style_prompt": persona.style_prompt,
            "edited": edited,
        }

    def model_summary(self) -> dict[str, object]:
        config = self._model_config
        roles = {}
        for role, profile_name in sorted(config.roles.items()):
            profile = config.profiles[profile_name]
            roles[role] = {
                "profile": profile_name,
                "model": profile.model,
                "endpoint": profile.endpoint,
                "temperature": profile.temperature,
                "max_tokens": profile.max_tokens,
                "structured_output": profile.structured_output,
                "disabled": role in config.disabled_roles,
            }
        return {
            "roles": roles,
            "profiles": sorted(config.profiles),
            "disabled_roles": sorted(config.disabled_roles),
            "config_path": str(self._settings.model_config_path),
        }

    def settings_summary(self) -> dict[str, object]:
        return {
            "assurance_mode": self._settings.assurance_mode,
            "history_turns": self._settings.history_turns,
            "runtime_mode": self._settings.app_runtime_mode,
        }
