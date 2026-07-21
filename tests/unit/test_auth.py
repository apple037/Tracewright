import pytest

from agent_flow.auth import AuthenticatedPrincipal, bind_customer_context
from agent_flow.errors import AgentError


def test_self_service_customer_is_derived_not_trusted():
    principal = AuthenticatedPrincipal(
        subject_id="u1",
        tenant_id="t1",
        customer_id="c1",
        scopes=set(),
    )

    with pytest.raises(AgentError) as caught:
        bind_customer_context(
            principal,
            requested_customer_id="c2",
            session_customer_id=None,
        )

    assert caught.value.error_code == "AUTH_CUSTOMER_MISMATCH"


def test_self_service_customer_is_bound_from_principal():
    principal = AuthenticatedPrincipal(
        subject_id="u1",
        tenant_id="t1",
        customer_id="c1",
        scopes=set(),
    )

    context = bind_customer_context(
        principal,
        requested_customer_id=None,
        session_customer_id="c1",
    )

    assert context.customer_id == "c1"
    assert context.tenant_id == "t1"


def test_agent_requires_act_as_scope():
    principal = AuthenticatedPrincipal(
        subject_id="a1",
        tenant_id="t1",
        customer_id=None,
        scopes={"agent"},
    )

    with pytest.raises(AgentError) as caught:
        bind_customer_context(
            principal,
            requested_customer_id="c2",
            session_customer_id=None,
        )

    assert caught.value.error_code == "AUTH_ACT_AS_REQUIRED"


def test_agent_with_act_as_scope_is_bound_to_requested_customer():
    principal = AuthenticatedPrincipal(
        subject_id="a1",
        tenant_id="t1",
        customer_id=None,
        scopes={"agent", "customer:act_as"},
    )

    context = bind_customer_context(
        principal,
        requested_customer_id="c2",
        session_customer_id="c2",
    )

    assert context.customer_id == "c2"


def test_session_customer_must_match_authorized_customer():
    principal = AuthenticatedPrincipal(
        subject_id="u1",
        tenant_id="t1",
        customer_id="c1",
        scopes=set(),
    )

    with pytest.raises(AgentError) as caught:
        bind_customer_context(
            principal,
            requested_customer_id=None,
            session_customer_id="c2",
        )

    assert caught.value.error_code == "AUTH_SESSION_OWNERSHIP"
