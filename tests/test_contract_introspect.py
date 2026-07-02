"""Tests for contract_introspect — cross-service edge inference (spike)."""
from __future__ import annotations

from pathlib import Path

from graphify.contract_introspect import (
    Endpoint,
    cross_service_graph,
    normalize_path,
)
from graphify.validate import validate_extraction

FIXTURE = Path(__file__).parent / "fixtures" / "xservice"


# --- path normalization (the validated core) ---

def test_normalize_strips_scheme_host_and_collapses_params():
    assert normalize_path("http://user-service/users/${id}") == "/users/{}"
    assert normalize_path("/users/{user_id}") == "/users/{}"          # FastAPI/Spring
    assert normalize_path("/users/:id") == "/users/{}"                # Nest/Express
    assert normalize_path("/users/<id>") == "/users/{}"               # Flask
    assert normalize_path("https://api.stripe.com/v1/charges") == "/v1/charges"


def test_normalize_drops_query_and_trailing_slash():
    assert normalize_path("/users/?page=2") == "/users"
    assert normalize_path("`/orders/${oid}/items`") == "/orders/{}/items"


# --- fixture integration: cross-language cross-service resolution ---

def test_fixture_cross_service_edges():
    g = cross_service_graph(FIXTURE)
    assert validate_extraction({"nodes": g["nodes"], "edges": g["edges"]}) == []
    assert g["stats"] == {
        "endpoints": 4, "calls": 3,
        "matched_unique": 2, "matched_ambiguous": 0, "external": 1,
    }
    nodes = {n["id"]: n for n in g["nodes"]}
    resolved = {
        (nodes[e["source"]]["metadata"]["service"], nodes[e["target"]]["metadata"]["service"],
         e["metadata"]["path"])
        for e in g["edges"] if e["relation"] == "calls_service"
    }
    # NestJS consumer -> Python (FastAPI) and Java (Spring) producers
    assert ("order_service", "user_service", "/users/{}") in resolved
    assert ("order_service", "billing_service", "/invoices/{}") in resolved


def test_fixture_all_cross_service_edges_are_inferred_not_extracted():
    g = cross_service_graph(FIXTURE)
    xs = [e for e in g["edges"] if e["relation"] == "calls_service"]
    assert xs and all(e["confidence"] == "INFERRED" for e in xs)


def test_fixture_external_call_not_matched():
    # the Stripe call must be counted external, never edged.
    g = cross_service_graph(FIXTURE)
    assert g["stats"]["external"] == 1
    for e in g["edges"]:
        assert "stripe" not in e.get("metadata", {}).get("raw_url", "")


def test_producer_extraction_covers_all_three_frameworks():
    g = cross_service_graph(FIXTURE)
    routes = {(n["metadata"]["service"], n["label"]) for n in g["nodes"] if n["kind"] == "route"}
    assert ("user_service", "GET /users/{}") in routes      # FastAPI
    assert ("order_service", "GET /orders/{}") in routes    # NestJS (@Controller prefix composed)
    assert ("billing_service", "GET /invoices/{}") in routes  # Spring (@RequestMapping prefix composed)


# --- collision -> AMBIGUOUS (kept, never dropped) ---

def test_path_collision_marks_ambiguous(tmp_path, monkeypatch):
    import graphify.contract_introspect as ci

    # Two services expose the SAME (GET, /users/{}); a third consumes it.
    eps = [
        Endpoint("GET", "/users/{}", "/users/{id}", "svc_a", "get_a", "a.py", 1),
        Endpoint("GET", "/users/{}", "/users/:id", "svc_b", "get_b", "b.ts", 1),
    ]
    calls = [ci.Call("GET", "/users/{}", "http://x/users/1", "consume", "svc_c", "c.ts", 5)]
    monkeypatch.setattr(ci, "scan_service",
        lambda d, name: (eps, calls) if name == "svc_a" else ([], []))
    # single dir so scan runs once with the injected data
    (tmp_path / "svc_a").mkdir()
    g = ci.cross_service_graph(tmp_path)

    assert g["stats"]["matched_ambiguous"] == 1
    xs = [e for e in g["edges"] if e["relation"] == "calls_service"]
    assert len(xs) == 2  # fanned out to BOTH candidates, not dropped
    assert all(e["confidence"] == "AMBIGUOUS" for e in xs)
    assert {e["metadata"]["to_service"] for e in xs} == {"svc_a", "svc_b"}


def test_method_mismatch_is_not_a_match(tmp_path, monkeypatch):
    import graphify.contract_introspect as ci

    eps = [Endpoint("POST", "/users/{}", "/users/{id}", "svc_a", "create", "a.py", 1)]
    calls = [ci.Call("GET", "/users/{}", "http://x/users/1", "consume", "svc_c", "c.ts", 5)]
    monkeypatch.setattr(ci, "scan_service", lambda d, name: (eps, calls))
    (tmp_path / "svc_a").mkdir()
    g = ci.cross_service_graph(tmp_path)
    assert g["stats"]["external"] == 1
    assert [e for e in g["edges"] if e["relation"] == "calls_service"] == []
