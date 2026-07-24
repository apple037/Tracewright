import asyncio
import hashlib
import json
from datetime import datetime

from agent_flow.adapters.evidence import RagClient, ToolClient
from agent_flow.auth import AuthorizedCustomerContext
from agent_flow.contracts import (
    CollectedEvidence,
    DialogueClassification,
    EvidenceItem,
    EvidencePlan,
    EvidenceToolCall,
    RagSearchRequest,
    ToolCallRequest,
    ValidatedEvidence,
)
from agent_flow.errors import AgentError
from agent_flow.pipeline.policy import EVIDENCE_COLLECTOR_ALLOWED_ACTIONS


class EvidenceSourceCancelled(asyncio.CancelledError):
    def __init__(self, component: str, operation: str):
        super().__init__(f"{component}:{operation} cancelled")
        self.component = component
        self.operation = operation


class EvidenceCollectionCancelled(asyncio.CancelledError):
    def __init__(self, outcomes: tuple[BaseException, ...]):
        super().__init__("evidence collection cancelled")
        self.outcomes = outcomes


# Casual/unclear turns never retrieve, whatever topic the model also emitted.
_CASUAL_INTENTS = frozenset(
    {"greeting", "farewell", "goodbye", "smalltalk", "chitchat", "thanks", "hello"}
)
_CASUAL_MODES = frozenset({"casual", "unknown"})


def plan_evidence(classification: DialogueClassification) -> EvidencePlan:
    if classification.intent == "order_status":
        return EvidencePlan(
            required_facts=("order.current_status",),
            tool_calls=(
                EvidenceToolCall(
                    operation="order.lookup",
                    arguments={"order_id": "current"},
                    freshness_seconds=60,
                ),
            ),
        )
    if classification.intent in {"refund_status", "refund_request"}:
        return EvidencePlan(
            required_facts=("refund.current_status",),
        )
    # Small models sometimes set a knowledge_topic even for a greeting, so the
    # topic alone is not trusted. A casual/greeting intent, or a casual/unclear
    # conversation mode, means "just chat" — no RAG runs, so the generator has
    # no stray doc to parrot. Otherwise, retrieve the named topic if there is one.
    # ponytail: queries by source_id (the mock ignores query text); a real
    # semantic RAG would query the message — thread it here if that changes.
    if classification.intent in _CASUAL_INTENTS:
        return EvidencePlan()
    if classification.conversation_mode.value in _CASUAL_MODES:
        return EvidencePlan()
    if classification.knowledge_topic:
        return EvidencePlan(rag_queries=(classification.knowledge_topic,))
    return EvidencePlan()


async def collect_evidence(
    context: AuthorizedCustomerContext,
    plan: EvidencePlan,
    rag: RagClient,
    tools: ToolClient,
) -> CollectedEvidence:
    _validate_action_plan(plan)
    task_sources: dict[asyncio.Task, tuple[str, str]] = {}
    rag_tasks = []
    for query in plan.rag_queries:
        task = asyncio.create_task(_search_rag(rag, context, query))
        rag_tasks.append(task)
        task_sources[task] = ("rag", query)
    tool_tasks = []
    for call in plan.tool_calls:
        task = asyncio.create_task(_call_tool(tools, context, call))
        tool_tasks.append(task)
        task_sources[task] = ("tool", call.operation)
    tasks = [*rag_tasks, *tool_tasks]
    if tasks:
        try:
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_EXCEPTION
            )
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            outcomes: list[BaseException] = []
            for task, result in zip(tasks, results, strict=True):
                if isinstance(result, asyncio.CancelledError):
                    outcomes.append(EvidenceSourceCancelled(*task_sources[task]))
                elif isinstance(result, BaseException):
                    outcomes.append(result)
            raise EvidenceCollectionCancelled(tuple(outcomes[:20]))
        failures = [
            error for task in done
            if (error := task.exception()) is not None
        ]
        if failures:
            for task in pending:
                task.cancel()
            pending_list = list(pending)
            pending_results = await asyncio.gather(
                *pending_list, return_exceptions=True
            )
            cancelled = []
            for task, result in zip(pending_list, pending_results, strict=True):
                if isinstance(result, asyncio.CancelledError):
                    cancelled.append(EvidenceSourceCancelled(*task_sources[task]))
                elif isinstance(result, BaseException):
                    cancelled.append(result)
            raise BaseExceptionGroup(
                "evidence source outcomes", [*failures, *cancelled]
            )

    items: list[EvidenceItem] = []
    for task in rag_tasks:
        items.extend(task.result().items)
    for task in tool_tasks:
        items.append(task.result().evidence)
    return CollectedEvidence(items=tuple(items))


async def _search_rag(
    rag: RagClient, context: AuthorizedCustomerContext, query: str
):
    try:
        return await rag.search(context, RagSearchRequest(query=query))
    except asyncio.CancelledError as exc:
        raise EvidenceSourceCancelled("rag", query) from exc
    except Exception as exc:
        raise AgentError.dependency(
            "EVIDENCE_SOURCE_FAILED",
            retryable=False,
            failure_stage="evidence_collector",
            component="rag",
            operation=query,
        ) from exc


async def _call_tool(
    tools: ToolClient,
    context: AuthorizedCustomerContext,
    call: EvidenceToolCall,
):
    try:
        return await tools.call(
            context,
            ToolCallRequest(tool=call.operation, arguments=call.arguments),
        )
    except asyncio.CancelledError as exc:
        raise EvidenceSourceCancelled("tool", call.operation) from exc
    except Exception as exc:
        raise AgentError.dependency(
            "EVIDENCE_SOURCE_FAILED",
            retryable=False,
            failure_stage="evidence_collector",
            component="tool",
            operation=call.operation,
        ) from exc


def _action_error(error_code: str) -> AgentError:
    return AgentError.validation(
        error_code,
        retryable=False,
        failure_stage="evidence_collector",
    )


def _validate_action_plan(plan: EvidencePlan) -> None:
    signatures: set[str] = set()
    for call in plan.tool_calls:
        if call.operation not in EVIDENCE_COLLECTOR_ALLOWED_ACTIONS:
            raise _action_error("ACTION_NOT_ALLOWED")
        if set(call.arguments) != {"order_id"}:
            raise _action_error("ACTION_ARGUMENT_INVALID")
        order_id = call.arguments["order_id"]
        if not isinstance(order_id, str) or not order_id.strip():
            raise _action_error("ACTION_ARGUMENT_INVALID")
        signature = json.dumps(
            (call.operation, call.arguments),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if signature in signatures:
            raise _action_error("DUPLICATE_ACTION")
        signatures.add(signature)


def _is_fresh(item: EvidenceItem, freshness_seconds: int, now: datetime) -> bool:
    if item.retrieved_at.tzinfo is None or now.tzinfo is None:
        return False
    if item.retrieved_at > now:
        return False
    if item.valid_until is not None and item.valid_until <= now:
        return False
    return (now - item.retrieved_at).total_seconds() <= freshness_seconds


def _order_status(
    item: EvidenceItem, call: EvidenceToolCall, now: datetime
) -> str | None:
    if item.source_id != "tool:order.lookup":
        return None
    if item.metadata.get("tool") != call.operation:
        return None
    if item.metadata.get("arguments") != call.arguments:
        return None
    if hashlib.sha256(item.content.encode("utf-8")).hexdigest() != item.content_checksum:
        return None
    try:
        payload = json.loads(item.content)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    status = payload.get("status")
    if not isinstance(status, str) or not status.strip():
        return None
    if not _is_fresh(item, call.freshness_seconds, now):
        return None
    return status.strip()


def _raise_insufficient():
    raise AgentError.validation(
        "EVIDENCE_INSUFFICIENT",
        retryable=False,
        failure_stage="evidence_validator",
    )


def validate_evidence(
    plan: EvidencePlan, evidence: CollectedEvidence, now: datetime
) -> ValidatedEvidence:
    validated_items: list[EvidenceItem] = []
    for fact in plan.required_facts:
        if fact != "order.current_status":
            _raise_insufficient()
        call = next(
            (value for value in plan.tool_calls if value.operation == "order.lookup"),
            None,
        )
        if call is None:
            _raise_insufficient()
        matches = [
            (item, status)
            for item in evidence.items
            if (status := _order_status(item, call, now)) is not None
        ]
        if not matches or len({status for _, status in matches}) > 1:
            _raise_insufficient()
        validated_items.extend(item for item, _ in matches)
    reason = (
        "REQUIRED_EVIDENCE_PRESENT" if plan.required_facts else "NO_EVIDENCE_REQUIRED"
    )
    # Only evidence the plan actually asked for reaches the generator: facts
    # tied to a required_fact, plus hits from an intentional knowledge lookup
    # (rag_queries). When the plan retrieved nothing (e.g. a greeting), any
    # stray collected item is dropped so it can never be parroted.
    items = list(validated_items)
    if plan.rag_queries:
        items.extend(evidence.items)
    return ValidatedEvidence(
        items=tuple(items),
        sufficient=True,
        reason_codes=(reason,),
    )
