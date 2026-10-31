"""Tests for codegraph_ingest — enhancing a CodeGraph index with our cross-service
layer. Uses a synthetic CodeGraph-shaped SQLite so CI needs no CodeGraph install.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from graphify.change_plan import graph_from_extraction, plan_change
from graphify.codegraph_ingest import (
    cross_service_extraction_from_codegraph,
    discover_repo_dbs,
    ingest_db,
    ingest_estate,
)


def _make_cg_db(path: Path, nodes: list[dict], edges: list[dict]) -> Path:
    """Write a minimal CodeGraph-shaped SQLite (only the columns the adapter reads)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE nodes (id TEXT, kind TEXT, name TEXT, qualified_name TEXT, "
        "file_path TEXT, start_line INT, end_line INT, signature TEXT, docstring TEXT, language TEXT)"
    )
    conn.execute("CREATE TABLE edges (source TEXT, target TEXT, kind TEXT, metadata TEXT, line INT)")
    for n in nodes:
        conn.execute(
            "INSERT INTO nodes (id, kind, name, qualified_name, file_path, start_line, language) "
            "VALUES (:id, :kind, :name, :qualified_name, :file_path, :start_line, :language)",
            {"qualified_name": n.get("name"), "start_line": 1, "language": "python", **n},
        )
    for e in edges:
        conn.execute(
            "INSERT INTO edges (source, target, kind, metadata, line) VALUES (:source, :target, :kind, :metadata, :line)",
            {"metadata": None, "line": 1, **e},
        )
    conn.commit()
    conn.close()
    return path


# --- ingest_db ---------------------------------------------------------------

def test_ingest_db_maps_and_tags(tmp_path):
    db = _make_cg_db(
        tmp_path / "svc" / ".codegraph" / "codegraph.db",
        nodes=[{"id": "function:1", "kind": "function", "name": "get_user",
                "file_path": "svc/main.py"}],
        edges=[{"source": "function:1", "target": "route:1", "kind": "references"}],
    )
    got = ingest_db(db, repo="svc")
    n = got["nodes"][0]
    assert n["id"] == "svc::function:1"          # namespaced
    assert n["label"] == "get_user" and n["kind"] == "function"
    assert n["source_file"] == "svc/main.py" and n["source_location"] == "L1"
    assert n["metadata"]["repo"] == "svc"
    e = got["edges"][0]
    assert e["source"] == "svc::function:1" and e["target"] == "svc::route:1"
    assert e["relation"] == "references"


def test_ingest_db_normalises_relation_synonyms(tmp_path):
    db = _make_cg_db(
        tmp_path / "a" / ".codegraph" / "codegraph.db",
        nodes=[{"id": "f1", "kind": "function", "name": "a", "file_path": "a/x.py"},
               {"id": "f2", "kind": "function", "name": "b", "file_path": "a/x.py"}],
        edges=[{"source": "f1", "target": "f2", "kind": "call"}],
    )
    assert ingest_db(db)["edges"][0]["relation"] == "calls"


def test_ingest_db_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        ingest_db(tmp_path / "nope.db")


# --- discovery + estate -------------------------------------------------------

def test_discover_prefers_per_repo_dbs(tmp_path):
    _make_cg_db(tmp_path / ".codegraph" / "codegraph.db", [], [])
    _make_cg_db(tmp_path / "svc_a" / ".codegraph" / "codegraph.db", [], [])
    _make_cg_db(tmp_path / "svc_b" / ".codegraph" / "codegraph.db", [], [])
    dbs = discover_repo_dbs(tmp_path)
    assert set(dbs) == {"svc_a", "svc_b"}       # whole-tree index superseded


def test_ingest_estate_merges_without_collision(tmp_path):
    _make_cg_db(tmp_path / "svc_a" / ".codegraph" / "codegraph.db",
                [{"id": "f1", "kind": "function", "name": "a", "file_path": "svc_a/x.py"}], [])
    _make_cg_db(tmp_path / "svc_b" / ".codegraph" / "codegraph.db",
                [{"id": "f1", "kind": "function", "name": "b", "file_path": "svc_b/y.py"}], [])
    est = ingest_estate(tmp_path)
    assert est["repos"] == ["svc_a", "svc_b"]
    ids = {n["id"] for n in est["nodes"]}
    assert ids == {"svc_a::f1", "svc_b::f1"}     # same raw id, namespaced apart


def test_ingest_estate_requires_an_index(tmp_path):
    with pytest.raises(FileNotFoundError):
        ingest_estate(tmp_path)


# --- the enhancement: cross-service edges stitched onto CodeGraph nodes -------

def test_cross_service_edges_added_over_codegraph(tmp_path):
    # producer service: a FastAPI route+handler; consumer service: a fetch to it.
    (tmp_path / "svc_a").mkdir()
    (tmp_path / "svc_a" / "main.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "\n"
        '@app.get("/things/{id}")\n'
        "def get_thing(id):\n"
        "    return {}\n"
    )
    (tmp_path / "svc_b").mkdir()
    (tmp_path / "svc_b" / "handler.ts").write_text(
        "export class Worker {\n"
        "  async doWork(id) {\n"
        "    const t = await fetch(`http://svc-a/things/${id}`);\n"
        "    return t;\n"
        "  }\n"
        "}\n"
    )
    # CodeGraph-shaped index per repo, whose node names/files match the handlers so
    # reconcile can fold the cross-service edge onto them.
    _make_cg_db(tmp_path / "svc_a" / ".codegraph" / "codegraph.db",
                [{"id": "fn:get_thing", "kind": "function", "name": "get_thing",
                  "file_path": str(tmp_path / "svc_a" / "main.py"), "start_line": 5}], [])
    _make_cg_db(tmp_path / "svc_b" / ".codegraph" / "codegraph.db",
                [{"id": "fn:doWork", "kind": "function", "name": "doWork",
                  "file_path": str(tmp_path / "svc_b" / "handler.ts"), "start_line": 1}], [])

    merged = cross_service_extraction_from_codegraph(tmp_path)
    assert merged["reconciliation"]["matched"] >= 2   # both handlers folded onto CG nodes

    cs = [e for e in merged["edges"] if e["relation"] == "calls_service"]
    assert cs, "expected a calls_service edge stitched onto the CodeGraph nodes"
    # the edge connects the consumer's CodeGraph node → the producer's CodeGraph node
    assert any(e["source"] == "svc_b::fn:doWork" and e["target"] == "svc_a::fn:get_thing"
               for e in cs)

    # and plan_change now crosses the boundary on top of the CodeGraph graph
    G = graph_from_extraction(merged)
    plan = plan_change(G, "change how a thing is fetched", root=tmp_path,
                       top_services=2, top_seeds=4, depth=3)
    assert "svc_b" in plan.impacted_services or any(
        r.service == "svc_b" for r in plan.affected
    )
