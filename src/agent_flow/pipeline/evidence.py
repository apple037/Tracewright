import asyncio
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
    if classification.conversation_mode.value == "informational":
        return EvidencePlan(rag_queries=(classification.intent,))
    return EvidencePlan()


async def collect_evidence(
    context: AuthorizedCustomerContext,
    plan: EvidencePlan,
    rag: RagClient,
    tools: ToolClient,
) -> CollectedEvidence:
    _validate_action_plan(plan)
    rag_tasks: list[asyncio.Task] = []
    tool_tasks: list[asyncio.Task] = []
    async with asyncio.TaskGroup() as group:
        for query in plan.rag_queries:
            rag_tasks.append(
                group.create_task(_search_rag(rag, context, query))
            )
        for call in plan.tool_calls:
            tool_tasks.append(
                group.create_task(
                    _call_tool(tools, context, call)
                )
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


def _facts(item: EvidenceItem) -> frozenset[str]:
    declared = item.metadata.get("facts")
    if isinstance(declared, (list, tuple, set, frozenset)):
        return frozenset(str(value) for value in declared)
    fact = item.metadata.get("fact")
    if isinstance(fact, str):
        return frozenset((fact,))
    tool = item.metadata.get("tool")
    fallback = {
        "order.lookup": "order.current_status",
        "refund.lookup": "refund.current_status",
    }.get(tool)
    return frozenset((fallback,)) if fallback else frozenset()


def _freshness_for(fact: str, plan: EvidencePlan) -> int | None:
    operation = {
        "order.current_status": "order.lookup",
        "refund.current_status": "refund.lookup",
    }.get(fact)
    return next(
        (
            call.freshness_seconds
            for call in plan.tool_calls
            if call.operation == operation
        ),
        None,
    )


def _is_fresh(item: EvidenceItem, fact: str, plan: EvidencePlan, now: datetime) -> bool:
    if item.retrieved_at.tzinfo is None or now.tzinfo is None:
        return False
    if item.retrieved_at > now:
        return False
    if item.valid_until is not None and item.valid_until <= now:
        return False
    freshness = _freshness_for(fact, plan)
    return freshness is None or (now - item.retrieved_at).total_seconds() <= freshness


def validate_evidence(
    plan: EvidencePlan, evidence: CollectedEvidence, now: datetime
) -> ValidatedEvidence:
    insufficient = False
    for fact in plan.required_facts:
        matches = [item for item in evidence.items if fact in _facts(item)]
        fresh = [
            item
            for item in matches
            if item.content.strip() and _is_fresh(item, fact, plan, now)
        ]
        if not fresh or len({item.content for item in fresh}) > 1:
            insufficient = True
            break
    if insufficient:
        raise AgentError.validation(
            "EVIDENCE_INSUFFICIENT",
            retryable=False,
            failure_stage="evidence_validator",
        )
    reason = (
        "REQUIRED_EVIDENCE_PRESENT" if plan.required_facts else "NO_EVIDENCE_REQUIRED"
    )
    return ValidatedEvidence(
        items=evidence.items, sufficient=True, reason_codes=(reason,)
    )
