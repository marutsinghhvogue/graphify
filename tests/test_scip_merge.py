"""Tests for reconcile_scip — merge-on-identity of SCIP onto tree-sitter graphs."""
from __future__ import annotations

from pathlib import Path

import pytest

from graphify.scip_ingest import reconcile_scip

FIXTURES = Path(__file__).parent / "fixtures" / "scip"


def _n(nid, label, file, line, **kw):
    return {"id": nid, "label": label, "file_type": "code",
            "source_file": file, "source_location": f"L{line}", **kw}


def _e(s, t, rel, conf="EXTRACTED", **kw):
    return {"source": s, "target": t, "relation": rel, "confidence": conf,
            "source_file": "m.py", **kw}


# --- core join: SCIP 0-based line joins base 1-based line (offset +1) ---

def test_join_offset_and_kind_stamp():
    base = {"nodes": [_n("u", "User", "pkg/models.py", 2)], "edges": []}
    scip = {"nodes": [_n("scip_u", "User", "pkg/models.py", 1,
                         metadata={"scip_symbol": "SYM", "scip_kind": "type"})],
            "edges": []}
    out = reconcile_scip(base, scip)
    assert out["reconciliation"]["matched"] == 1
    u = {n["id"]: n for n in out["nodes"]}["u"]
    assert u["kind"] == "type"                       # kind promoted to canonical node
    assert u["metadata"]["scip_symbol"] == "SYM"     # symbol stamped
    assert "scip_u" not in {n["id"] for n in out["nodes"]}  # not duplicated


def test_path_suffix_join_tolerates_prefix_difference():
    # base path has an extra leading component; suffix match still joins.
    base = {"nodes": [_n("u", "User", "root/pkg/models.py", 2)], "edges": []}
    scip = {"nodes": [_n("s", "User", "pkg/models.py", 1,
                         metadata={"scip_kind": "type"})], "edges": []}
    assert reconcile_scip(base, scip)["reconciliation"]["matched"] == 1


# --- edge rewrite + precedence ---

def test_scip_edge_supersedes_base_on_same_endpoints():
    base = {
        "nodes": [_n("a", "a()", "m.py", 2), _n("b", "b()", "m.py", 5)],
        "edges": [_e("a", "b", "calls", "INFERRED", context="call")],
    }
    scip = {
        "nodes": [_n("sa", "a()", "m.py", 1, metadata={"scip_kind": "function"}),
                  _n("sb", "b()", "m.py", 4, metadata={"scip_kind": "function"})],
        "edges": [_e("sa", "sb", "calls", "EXTRACTED", context="scip")],
    }
    out = reconcile_scip(base, scip)
    calls = [e for e in out["edges"] if e["relation"] == "calls"]
    assert len(calls) == 1                       # deduped, not doubled
    assert calls[0]["confidence"] == "EXTRACTED"  # SCIP won
    assert calls[0]["context"] == "scip"
    assert calls[0]["source"] == "a" and calls[0]["target"] == "b"  # rewritten to canonical


def test_base_edge_scip_did_not_reproduce_is_preserved():
    base = {
        "nodes": [_n("a", "a()", "m.py", 2), _n("b", "b()", "m.py", 5)],
        "edges": [_e("a", "b", "calls", "INFERRED", context="call")],  # e.g. dynamic
    }
    scip = {"nodes": [_n("sa", "a()", "m.py", 1)], "edges": []}
    out = reconcile_scip(base, scip)
    calls = [e for e in out["edges"] if e["relation"] == "calls"]
    # SCIP said nothing about a's calls → its base edge is untouched (not demoted)
    assert len(calls) == 1 and calls[0]["confidence"] == "INFERRED"


# --- per-caller demote (recall-safety) ---

def _collision_case():
    # a really calls b (SCIP-resolved); tree-sitter also guessed a→c (name collision)
    base = {
        "nodes": [_n("a", "a()", "m.py", 2), _n("b", "b()", "m.py", 5), _n("c", "save()", "m.py", 8)],
        "edges": [_e("a", "b", "calls", "INFERRED", context="call"),
                  _e("a", "c", "calls", "INFERRED", context="call")],
    }
    scip = {
        "nodes": [_n("sa", "a()", "m.py", 1), _n("sb", "b()", "m.py", 4)],
        "edges": [_e("sa", "sb", "calls", "EXTRACTED", context="scip")],
    }
    return base, scip


def test_per_caller_demote_of_contradicted_call():
    base, scip = _collision_case()
    out = reconcile_scip(base, scip)
    calls = {(e["source"], e["target"]): e for e in out["edges"] if e["relation"] == "calls"}
    assert calls[("a", "b")]["confidence"] == "EXTRACTED"    # SCIP-confirmed
    assert calls[("a", "b")]["context"] == "scip"
    assert calls[("a", "c")]["confidence"] == "AMBIGUOUS"    # demoted — kept, not deleted
    assert calls[("a", "c")]["metadata"]["demoted_by"] == "scip"
    assert out["reconciliation"]["demoted"] == 1


def test_strict_scip_drops_contradicted_call():
    base, scip = _collision_case()
    out = reconcile_scip(base, scip, strict=True)
    keys = {(e["source"], e["target"]) for e in out["edges"] if e["relation"] == "calls"}
    assert ("a", "b") in keys        # SCIP-confirmed kept
    assert ("a", "c") not in keys    # contradicted collision dropped under strict
    assert out["reconciliation"]["dropped_strict"] == 1


# --- keep-as-new / never-guess behavior ---

def test_scip_only_node_kept_when_no_base_match():
    base = {"nodes": [_n("a", "a()", "m.py", 2)], "edges": []}
    scip = {"nodes": [_n("iface_m", "speak", "m.py", 99,
                         metadata={"scip_kind": "method"})], "edges": []}
    out = reconcile_scip(base, scip)
    assert out["reconciliation"]["scip_only_new"] == 1
    kept = {n["id"]: n for n in out["nodes"]}["iface_m"]
    assert kept["kind"] == "method"  # kind stays on the new node


def test_ambiguous_match_kept_new_not_guessed():
    # two real symbols on the same (file, line) → ambiguous → do NOT link.
    base = {"nodes": [_n("x", "foo", "m.py", 2), _n("y", "bar", "m.py", 2)], "edges": []}
    scip = {"nodes": [_n("s", "foo", "m.py", 1, metadata={"scip_kind": "function"})],
            "edges": []}
    out = reconcile_scip(base, scip)
    assert out["reconciliation"]["ambiguous_kept_new"] == 1
    assert "s" in {n["id"] for n in out["nodes"]}


def test_file_container_node_does_not_shadow_symbol():
    # file node shares line 1 with the first def; must not cause ambiguity.
    base = {"nodes": [_n("file", "models.py", "pkg/models.py", 1),
                      _n("u", "User", "pkg/models.py", 1)], "edges": []}
    scip = {"nodes": [_n("s", "User", "pkg/models.py", 0,
                         metadata={"scip_kind": "type"})], "edges": []}
    out = reconcile_scip(base, scip)
    assert out["reconciliation"]["matched"] == 1
    assert out["reconciliation"]["ambiguous_kept_new"] == 0
    assert {n["id"]: n for n in out["nodes"]}["u"]["kind"] == "type"


def test_external_scip_node_kept_as_new():
    base = {"nodes": [_n("a", "a()", "m.py", 2)], "edges": []}
    scip = {"nodes": [_n("ext", "OtherLib", "m.py", 0,
                         metadata={"scip_external": "true"})], "edges": []}
    out = reconcile_scip(base, scip)
    assert out["reconciliation"]["external"] == 1
    assert "ext" in {n["id"] for n in out["nodes"]}


# --- integration: the save() collision precision claim, end to end ---

def test_fixture_collision_resolved_precisely():
    pytest.importorskip("google.protobuf")
    from graphify.extract import extract_python
    from graphify.scip_ingest import ingest_scip_index

    root = FIXTURES / "py_sample"
    base = {"nodes": [], "edges": []}
    for py in sorted(root.rglob("*.py")):
        r = extract_python(py)
        base["nodes"] += r["nodes"]
        base["edges"] += r.get("edges", [])
    merged = reconcile_scip(base, ingest_scip_index(root / "index.scip"))

    nodes = {n["id"]: n for n in merged["nodes"]}
    # process() must call BOTH save() defs, each resolved to the correct class.
    call_targets = {
        nodes[e["target"]].get("metadata", {}).get("scip_symbol", "")
        for e in merged["edges"]
        if e["relation"] == "calls" and e.get("context") == "scip"
        and nodes.get(e["source"], {}).get("label") == "process()"
    }
    assert any("User#save()." in s for s in call_targets)
    assert any("Logger#save()." in s for s in call_targets)
    assert merged["reconciliation"]["matched"] == 5


def test_merge_scip_cli(monkeypatch, tmp_path, capsys):
    """End-to-end: build a tree-sitter graph.json, then `graphify merge-scip`."""
    pytest.importorskip("google.protobuf")
    import json as _json

    import graphify.__main__ as mainmod
    from graphify.build import build_from_json
    from graphify.cluster import cluster
    from graphify.export import to_json
    from graphify.extract import extract_python

    root = FIXTURES / "py_sample"
    base = {"nodes": [], "edges": []}
    for py in sorted(root.rglob("*.py")):
        r = extract_python(py)
        base["nodes"] += r["nodes"]
        base["edges"] += r.get("edges", [])
    ids = {n["id"] for n in base["nodes"]}
    base["edges"] = [e for e in base["edges"] if e["source"] in ids and e["target"] in ids]
    gp = tmp_path / "graph.json"
    G = build_from_json(base, directed=True)
    to_json(G, cluster(G), str(gp), force=True)

    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(mainmod.sys, "argv",
        ["graphify", "merge-scip", str(root / "index.scip"), "--graph", str(gp)])
    mainmod.main()
    out = capsys.readouterr().out
    assert "Merged SCIP index into" in out

    g = _json.loads(gp.read_text())
    kinds = {n["label"]: n["kind"] for n in g["nodes"] if n.get("kind")}
    assert kinds.get("process()") == "function"
    assert kinds.get("User") == "type"
    # The precision win: build_from_json collapses User.save/Logger.save into one
    # ".save()" node; the SCIP merge recovers BOTH as distinct type-exact call
    # edges from process().
    links = g.get("links", g.get("edges", []))
    scip_calls = [e for e in links if e.get("relation") == "calls" and e.get("context") == "scip"]
    assert len(scip_calls) == 2
    assert all(e["confidence"] == "EXTRACTED" for e in scip_calls)
    nodes_by_id = {n["id"]: n for n in g["nodes"]}
    targets = {nodes_by_id[e["target"]].get("metadata", {}).get("scip_symbol", "") for e in scip_calls}
    assert any("User#save()." in s for s in targets)
    assert any("Logger#save()." in s for s in targets)
