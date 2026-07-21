from pydantic import BaseModel, ConfigDict

from agent_flow.errors import AgentError


class AuthenticatedPrincipal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    subject_id: str
    tenant_id: str
    customer_id: str | None
    scopes: frozenset[str]


class AuthorizedCustomerContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    subject_id: str
    tenant_id: str
    customer_id: str


def bind_customer_context(
    principal: AuthenticatedPrincipal,
    requested_customer_id: str | None,
    session_customer_id: str | None,
) -> AuthorizedCustomerContext:
    if principal.customer_id is not None:
        if requested_customer_id not in (None, principal.customer_id):
            raise AgentError.auth("AUTH_CUSTOMER_MISMATCH")
        customer_id = principal.customer_id
    else:
        if "customer:act_as" not in principal.scopes or requested_customer_id is None:
            raise AgentError.auth("AUTH_ACT_AS_REQUIRED")
        customer_id = requested_customer_id

    if session_customer_id not in (None, customer_id):
        raise AgentError.auth("AUTH_SESSION_OWNERSHIP")

    return AuthorizedCustomerContext(
        subject_id=principal.subject_id,
        tenant_id=principal.tenant_id,
        customer_id=customer_id,
    )
