from pydantic import Field

from agent_flow.adapters.models import ModelGateway
from agent_flow.contracts import DialogueClassification, StrictModel
from agent_flow.pipeline.model_outputs import DialogueClassificationResult
from agent_flow.pipeline.policy import invoke_structured_model


class ClassificationRequest(StrictModel):
    messages: tuple[str, ...] = Field(min_length=1, max_length=100)
    # Brief catalog of what the knowledge base owns, so the classifier can judge
    # whether the turn needs retrieval instead of blindly querying RAG.
    knowledge_catalog: tuple[str, ...] = Field(default=(), max_length=50)


async def classify_dialogue(
    models: ModelGateway,
    messages: list[str] | tuple[str, ...],
    knowledge_catalog: tuple[str, ...] = (),
) -> DialogueClassification:
    request = ClassificationRequest(
        messages=tuple(messages), knowledge_catalog=tuple(knowledge_catalog)
    )
    result = await invoke_structured_model(
        models,
        "dialogue_classifier", request, DialogueClassificationResult
    )
    return DialogueClassification.model_validate(result.model_dump())
