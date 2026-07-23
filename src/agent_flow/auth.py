import hmac

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


class DemoTokenAuthenticator:
    """Constant-time static-token authentication for the demo runtime only."""

    def __init__(
        self,
        *,
        customer_token: str,
        admin_token: str,
        tenant_id: str,
        customer_id: str,
    ) -> None:
        self._customer_token = customer_token
        self._admin_token = admin_token
        self._customer = AuthenticatedPrincipal(
            subject_id="demo-customer",
            tenant_id=tenant_id,
            customer_id=customer_id,
            scopes=frozenset({"turn:write", "trace:read", "trace:retry"}),
        )
        self._admin = AuthenticatedPrincipal(
            subject_id="demo-admin",
            tenant_id=tenant_id,
            customer_id=None,
            scopes=frozenset(
                {"customer:act_as", "trace:read", "trace:retry", "trace:admin"}
            ),
        )

    @classmethod
    def from_settings(cls, settings) -> "DemoTokenAuthenticator":
        if settings.app_runtime_mode != "demo":
            raise RuntimeError("demo authentication is disabled")
        return cls(
            customer_token=settings.demo_customer_token.get_secret_value(),
            admin_token=settings.demo_admin_token.get_secret_value(),
            tenant_id=settings.demo_tenant_id,
            customer_id=settings.demo_customer_id,
        )

    async def __call__(self, token: str) -> AuthenticatedPrincipal | None:
        if hmac.compare_digest(token, self._customer_token):
            return self._customer
        if hmac.compare_digest(token, self._admin_token):
            return self._admin
        return None
