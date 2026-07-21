from pydantic import Field

from agent_flow.adapters.models import ModelGateway
from agent_flow.contracts import DialogueClassification, StrictModel
from agent_flow.pipeline.model_outputs import DialogueClassificationResult


class ClassificationRequest(StrictModel):
    messages: tuple[str, ...] = Field(min_length=1, max_length=100)


async def classify_dialogue(
    models: ModelGateway, messages: list[str] | tuple[str, ...]
) -> DialogueClassification:
    request = ClassificationRequest(messages=tuple(messages))
    result = await models.structured(
        "dialogue_classifier", request, DialogueClassificationResult
    )
    return DialogueClassification.model_validate(result.model_dump())
