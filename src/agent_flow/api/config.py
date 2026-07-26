"""Admin view of the knobs that get turned most: prompts, persona, models.

Read shows exactly what is in effect right now — including whether a value came
from the YAML on disk or from a console edit. Write layers an override on top of
the file; the file itself, and its comments, are never rewritten.

Every edit changes the artifact's checksum, and that checksum is recorded on the
span of every node that used it. So a trace always identifies the exact prompt
text that produced it, even after the prompt has been edited since.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from agent_flow.api.dependencies import principal, require_scope, services
from agent_flow.auth import AuthenticatedPrincipal
from agent_flow.contracts import ConversationMode, ResponseMode


router = APIRouter(prefix="/api/v1")

MAX_PROMPT_CHARS = 20000


class PromptEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    system_prompt: str = Field(max_length=MAX_PROMPT_CHARS)


class PersonaEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    style_prompt: str = Field(max_length=MAX_PROMPT_CHARS)


def _admin(request: Request, authenticated: AuthenticatedPrincipal):
    require_scope(authenticated, "trace:admin")
    return services(request)


def _config_service(request: Request, authenticated: AuthenticatedPrincipal):
    app_services = _admin(request, authenticated)
    service = getattr(app_services, "runtime_config", None)
    if service is None:
        raise HTTPException(status_code=503, detail="configuration unavailable")
    return service


@router.get("/config")
async def read_config(
    request: Request,
    authenticated: AuthenticatedPrincipal = Depends(principal),
):
    service = _config_service(request, authenticated)
    artifacts = service.artifacts()
    overridden = dict(artifacts.overridden)

    prompts = [
        {
            "node": node,
            "artifact_id": prompt.artifact_id,
            "version": prompt.version,
            "checksum": prompt.ref.checksum,
            "system_prompt": prompt.system_prompt,
            "system_rules": list(prompt.system_rules),
            "output_contract": prompt.output_contract,
            "edited": "system_prompt" in overridden.get(prompt.artifact_id, ()),
        }
        for node, prompt in sorted(artifacts.prompts_by_node.items())
    ]
    personas = [
        {
            "artifact_id": persona.artifact_id,
            "version": persona.version,
            "checksum": persona.ref.checksum,
            "locale": persona.locale,
            "applies_to": [mode.value for mode in persona.applies_to],
            "style_prompt": persona.style_prompt,
            "edited": "style_prompt" in overridden.get(persona.artifact_id, ()),
        }
        for persona in artifacts.personas
    ]
    return {
        "prompts": prompts,
        "personas": personas,
        "models": service.model_summary(),
        "choices": {
            "response_modes": [mode.value for mode in ResponseMode],
            "conversation_modes": [mode.value for mode in ConversationMode],
        },
        "settings": service.settings_summary(),
    }


@router.put("/config/prompts/{node}")
async def update_prompt(
    node: str,
    edit: PromptEdit,
    request: Request,
    authenticated: AuthenticatedPrincipal = Depends(principal),
):
    service = _config_service(request, authenticated)
    try:
        return service.set_prompt(node, edit.system_prompt)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown prompt node") from None


@router.delete("/config/prompts/{node}")
async def revert_prompt(
    node: str,
    request: Request,
    authenticated: AuthenticatedPrincipal = Depends(principal),
):
    service = _config_service(request, authenticated)
    try:
        return service.clear_prompt(node)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown prompt node") from None


@router.put("/config/personas/{artifact_id}")
async def update_persona(
    artifact_id: str,
    edit: PersonaEdit,
    request: Request,
    authenticated: AuthenticatedPrincipal = Depends(principal),
):
    service = _config_service(request, authenticated)
    try:
        return service.set_persona(artifact_id, edit.style_prompt)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown persona") from None


@router.delete("/config/personas/{artifact_id}")
async def revert_persona(
    artifact_id: str,
    request: Request,
    authenticated: AuthenticatedPrincipal = Depends(principal),
):
    service = _config_service(request, authenticated)
    try:
        return service.clear_persona(artifact_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown persona") from None
