"""Tests for contract_introspect — cross-service edge inference (spike)."""
from __future__ import annotations

from pathlib import Path

from graphify.contract_introspect import (
    Endpoint,
    cross_service_graph,
    normalize_path,
    reconcile_contract,
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


# --- reconciliation with the tree-sitter AST graph ---


def test_reconcile_folds_contract_handlers_onto_ast_nodes():
    """A contract function node that matches exactly one AST node by
    (path-suffix, name) is folded onto it: the AST id is canonical, service is
    stamped, the contract fn node is dropped, and its edges are repointed."""
    contract = cross_service_graph(FIXTURE)
    # Minimal AST-side stand-ins for the three real handlers (labels mirror how
    # tree-sitter emits them: trailing "()", optional leading "." for methods).
    base = {"nodes": [
        {"id": "ast_get_user", "label": "get_user()",
         "source_file": str(FIXTURE / "user_service" / "main.py"), "source_location": "L8"},
        {"id": "ast_create_user", "label": "create_user()",
         "source_file": str(FIXTURE / "user_service" / "main.py"), "source_location": "L13"},
        {"id": "ast_get_order", "label": ".getOrder()",
         "source_file": str(FIXTURE / "order_service" / "orders.controller.ts"),
         "source_location": "L9"},
        {"id": "ast_get_invoice", "label": ".getInvoice()",
         "source_file": str(FIXTURE / "billing_service" / "InvoiceController.java"),
         "source_location": "L10"},
    ], "edges": []}

    out = reconcile_contract(base, contract)
    rec = out["reconciliation"]

    # every contract handler folded onto its AST twin
    assert rec["matched"] == rec["contract_fn_nodes"] > 0
    assert rec["ambiguous_kept_new"] == 0

    # AST nodes are now service-stamped
    svc = {n["id"]: (n.get("metadata") or {}).get("service") for n in base["nodes"]}
    assert svc["ast_get_user"] == "user_service"
    assert svc["ast_get_order"] == "order_service"

    # calls_service edges are repointed onto AST ids (no svc_* function endpoints)
    cs = [e for e in out["edges"] if e["relation"] == "calls_service"]
    assert cs, "expected reconciled cross-service edges"
    assert all(
        not e["source"].startswith("svc_") and not e["target"].startswith("svc_")
        for e in cs
    ), "reconciled calls_service endpoints must be AST node ids"
    # the NestJS consumer reaches the Python handler across the boundary
    assert ("ast_get_order", "ast_get_user") in {(e["source"], e["target"]) for e in cs}

    # endpoint (route) nodes have no AST twin → kept as new svc_* nodes
    assert any(n.get("kind") == "route" for n in out["nodes"])


def test_reconcile_keeps_ambiguous_and_unmatched_as_new():
    """Contract fn nodes that match 0 or >1 base nodes are never name-guessed —
    they are kept as new nodes (recall preserved), mirroring reconcile_scip."""
    contract = {
        "nodes": [
            {"id": "svc_a_fn_save", "label": "save()", "kind": "function",
             "source_file": "a/svc.py", "metadata": {"service": "a"}},
            {"id": "svc_b_fn_ghost", "label": "ghost()", "kind": "function",
             "source_file": "b/svc.py", "metadata": {"service": "b"}},
        ],
        "edges": [],
    }
    # two same-named/​same-file AST candidates → ambiguous; ghost matches none.
    base = {"nodes": [
        {"id": "ast_save_1", "label": "save()", "source_file": "a/svc.py"},
        {"id": "ast_save_2", "label": "save()", "source_file": "a/svc.py"},
    ], "edges": []}

    out = reconcile_contract(base, contract)
    rec = out["reconciliation"]
    assert rec["matched"] == 0
    assert rec["ambiguous_kept_new"] == 1
    assert rec["kept_new"] == 1
    kept_ids = {n["id"] for n in out["nodes"]}
    assert {"svc_a_fn_save", "svc_b_fn_ghost"} <= kept_ids
