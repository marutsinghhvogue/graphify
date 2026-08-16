"""Tests for change_plan — the PRD→impact chain (Stages 1→2→3 composed).

Runs the service-first pipeline over the xservice fixture graph and asserts the
plan (a) picks the responsible service, (b) discovers seeds scoped to it (or falls
back to its handlers when the prose matches no symbol), and (c) the blast radius
crosses the service boundary to name the downstream-impacted service.
"""
from __future__ import annotations

from pathlib import Path

import networkx as nx
import pytest

from graphify.change_plan import (
    ChangePlan,
    format_change_plan,
    graph_from_extraction,
    plan_change,
)
from graphify.contract_introspect import cross_service_graph
from graphify.service_profiles import load_service_docs

FIXTURE = Path(__file__).parent / "fixtures" / "xservice"


@pytest.fixture(scope="module")
def G() -> nx.DiGraph:
    return graph_from_extraction(cross_service_graph(FIXTURE))


@pytest.fixture(scope="module")
def docs() -> dict[str, str]:
    return load_service_docs(FIXTURE)


# --- graph builder ------------------------------------------------------------

def test_graph_from_extraction_preserves_nodes_and_edges(G):
    assert G.is_directed()
    assert "svc_user_service_fn_get_user" in G
    # the cross-service call edge survives (order -> user)
    assert G.has_edge("svc_order_service_fn_getorder", "svc_user_service_fn_get_user")
    rel = G.edges["svc_order_service_fn_getorder", "svc_user_service_fn_get_user"]["relation"]
    assert rel == "calls_service"


# --- end-to-end chain ---------------------------------------------------------

def test_user_prd_reaches_downstream_order_service(G, docs):
    plan = plan_change(G, "let a customer update their user profile",
                       root=FIXTURE, service_docs=docs, top_services=2, top_seeds=4, depth=3)
    # Stage 1: user_service is responsible
    assert plan.services and plan.services[0].service == "user_service"
    assert plan.seed_source == "retrieved"
    # Stage 2: seeds are scoped and drawn from user_service
    assert any(s.name in ("get_user()", "create_user()") for s in plan.seeds)
    # Stage 3: blast radius crosses into order_service (getOrder calls get_user)
    assert plan.impacted_services.get("order_service", 0) >= 1
    assert plan.cross_boundary >= 1
    assert any(r.service == "order_service" and r.cross_boundary for r in plan.affected)


def test_billing_prd_falls_back_to_service_handlers(G, docs):
    # "sales tax / charged / bill" shares no token with getInvoice → Stage 2 has no
    # lexical seed, so it must fall back to the service's handler as the change point.
    plan = plan_change(G, "add sales tax to the amount a customer is charged on their bill",
                       root=FIXTURE, service_docs=docs, top_services=1, top_seeds=4, depth=3)
    assert plan.services[0].service == "billing_service"
    assert plan.seed_source == "service-entrypoints"
    assert any(s.name == "getInvoice()" for s in plan.seeds)
    # and the fallback still yields the downstream cross-service impact
    assert plan.impacted_services.get("order_service", 0) >= 1


def test_no_service_signal_scopes_to_whole_graph(G, docs):
    plan = plan_change(G, "provision kubernetes helm chart yaml", root=FIXTURE,
                       service_docs=docs, top_services=3, depth=2)
    assert plan.services == []
    assert plan.scoped_to_all is True


def test_depth_bounds_reachability(G, docs):
    # order_service is one calls_service hop from a user seed; depth=1 still reaches
    # it, but a seed with no incoming edges yields nothing.
    plan = plan_change(G, "get a user by id", root=FIXTURE, service_docs=docs,
                       top_services=1, top_seeds=3, depth=1)
    for r in plan.affected:
        assert r.depth <= 1


# --- formatter (pure over the dataclass) --------------------------------------

def test_format_change_plan_has_all_three_stages(G, docs):
    plan = plan_change(G, "let a customer update their user profile",
                       root=FIXTURE, service_docs=docs, top_services=2, depth=3)
    out = format_change_plan(plan)
    assert "Responsible services (Stage 1):" in out
    assert "Seed symbols (Stage 2" in out
    assert "Blast radius (Stage 3" in out
    assert "downstream services: order_service" in out


def test_format_change_plan_applies_sanitize():
    plan = ChangePlan(prd="x")
    plan.scoped_to_all = True
    out = format_change_plan(plan, sanitize=str.upper)
    assert 'Change plan for "X"' in out
