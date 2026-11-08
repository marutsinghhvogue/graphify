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
    # a really calls b and Logger.save (SCIP-resolved); tree-sitter bound the
    # `save()` call by name to the wrong save (c, User.save): a name collision.
    base = {
        "nodes": [_n("a", "a()", "m.py", 2), _n("b", "b()", "m.py", 5), _n("c", "save()", "m.py", 8),
                  _n("l", "save()", "log.py", 3)],
        "edges": [_e("a", "b", "calls", "INFERRED", context="call"),
                  _e("a", "c", "calls", "INFERRED", context="call")],
    }
    scip = {
        "nodes": [_n("sa", "a()", "m.py", 1), _n("sb", "b()", "m.py", 4), _n("sl", "save()", "log.py", 2)],
        "edges": [_e("sa", "sb", "calls", "EXTRACTED", context="scip"),
                  _e("sa", "sl", "calls", "EXTRACTED", context="scip")],
    }
    return base, scip


def test_edge_scip_is_silent_about_is_kept_not_demoted():
    # SCIP resolved a→b but said nothing about `save`: unconfirmed, not
    # contradicted (SCIP is not complete), so the tree-sitter edge is untouched.
    base = {
        "nodes": [_n("a", "a()", "m.py", 2), _n("b", "b()", "m.py", 5), _n("c", "save()", "m.py", 8)],
        "edges": [_e("a", "b", "calls", "INFERRED", context="call"),
                  _e("a", "c", "calls", "INFERRED", context="call")],
    }
    scip = {"nodes": [_n("sa", "a()", "m.py", 1), _n("sb", "b()", "m.py", 4)],
            "edges": [_e("sa", "sb", "calls", "EXTRACTED", context="scip")]}
    out = reconcile_scip(base, scip)
    calls = {(e["source"], e["target"]): e for e in out["edges"] if e["relation"] == "calls"}
    assert calls[("a", "c")]["confidence"] == "INFERRED"
    assert out["reconciliation"]["demoted"] == 0


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


# --- Java/Spring shapes (scip-java on a Spring Boot service) ---

def _sym(desc):
    return "semanticdb maven maven/com.acme/svc 1.0 " + desc


def test_annotated_method_joins_to_node_starting_at_its_annotations():
    # tree-sitter's node starts at `@Bean` (L24); SCIP points at the name (0-based 25 = L26)
    base = {"nodes": [_n("jm", ".mapper()", "cfg/MapperConfig.java", 24)], "edges": []}
    scip = {"nodes": [_n("s_jm", "mapper", "cfg/MapperConfig.java", 25,
                         metadata={"scip_symbol": _sym("cfg/MapperConfig#mapper()."),
                                   "scip_kind": "method"})], "edges": []}
    out = reconcile_scip(base, scip)
    assert out["reconciliation"]["matched"] == 1 and out["reconciliation"]["matched_by_name"] == 1
    assert "s_jm" not in {n["id"] for n in out["nodes"]}


def test_annotation_fallback_picks_the_overload_nearest_above():
    base = {"nodes": [_n("r1", ".read()", "web/VisitResource.java", 56),
                      _n("r2", ".read()", "web/VisitResource.java", 66)], "edges": []}
    scip = {"nodes": [_n("s2", "read", "web/VisitResource.java", 67,
                         metadata={"scip_symbol": _sym("web/VisitResource#read(+1).")})], "edges": [
            ]}
    out = reconcile_scip(base, scip)
    r2 = {n["id"]: n for n in out["nodes"]}["r2"]
    assert r2["metadata"]["scip_symbol"].endswith("read(+1).")


def test_annotation_fallback_is_bounded_and_needs_the_same_name():
    base = {"nodes": [_n("far", ".run()", "A.java", 1), _n("other", ".stop()", "A.java", 90)], "edges": []}
    scip = {"nodes": [_n("s_run", "run", "A.java", 99,
                         metadata={"scip_symbol": _sym("A#run().")})], "edges": []}
    out = reconcile_scip(base, scip)
    assert out["reconciliation"]["matched"] == 0      # same name is 98 lines up; other name nearby


def test_constructor_joins_by_its_type_name():
    base = {"nodes": [_n("ctor", ".ReminderJob()", "svc/ReminderJob.java", 30)], "edges": []}
    scip = {"nodes": [_n("s_ctor", "init", "svc/ReminderJob.java", 31,
                         metadata={"scip_symbol": _sym("svc/ReminderJob#`<init>`().")})], "edges": []}
    out = reconcile_scip(base, scip)
    assert out["reconciliation"]["matched"] == 1


def test_external_type_folds_onto_the_tree_sitter_stub_with_the_same_fqn():
    stub = {"id": "stub_hs", "label": "org.springframework.http.HttpStatus", "file_type": "code",
            "source_file": "", "source_location": ""}
    base = {"nodes": [_n("h", ".statusFor()", "web/C.java", 10), stub],
            "edges": [_e("h", "stub_hs", "references")]}
    scip = {"nodes": [_n("s_h", "statusFor", "web/C.java", 9,
                         metadata={"scip_symbol": _sym("web/C#statusFor().")}),
                      {"id": "s_hs", "label": "HttpStatus", "file_type": "code", "source_file": "web/C.java",
                       "source_location": "", "metadata": {
                           "scip_symbol": "semanticdb maven maven/org.springframework/spring-web 7.0 "
                                          "org/springframework/http/HttpStatus#", "scip_external": "true"}}],
            "edges": [_e("s_h", "s_hs", "references", context="scip")]}
    out = reconcile_scip(base, scip)
    assert out["reconciliation"]["external_folded"] == 1
    refs = [e for e in out["edges"] if e["relation"] == "references"]
    assert [(e["source"], e["target"], e.get("context")) for e in refs] == [("h", "stub_hs", "scip")]
    assert out["reconciliation"]["demoted"] == 0


def test_stub_reference_confirmed_by_a_same_named_scip_target():
    # tree-sitter's unresolved `Builder` stub vs SCIP's nested RouteClient#Builder#
    stub = {"id": "stub_b", "label": "Builder", "file_type": "code", "source_file": "", "source_location": ""}
    base = {"nodes": [_n("c", ".RouteClient()", "R.java", 20), _n("b", "Builder", "R.java", 40), stub],
            "edges": [_e("c", "stub_b", "references")]}
    scip = {"nodes": [_n("s_c", "init", "R.java", 20, metadata={"scip_symbol": _sym("R#`<init>`().")}),
                      _n("s_b", "Builder", "R.java", 39, metadata={"scip_symbol": _sym("R#Builder#")})],
            "edges": [_e("s_c", "s_b", "references", context="scip")]}
    out = reconcile_scip(base, scip)
    ref = [e for e in out["edges"] if e["source"] == "c" and e["target"] == "stub_b"]
    assert ref and ref[0]["confidence"] == "EXTRACTED"
    assert out["reconciliation"]["demoted"] == 0


def test_field_type_reference_credited_to_the_owning_class():
    # tree-sitter: class --references--> TimeSource. SCIP attributes it to the
    # field `clock` (a node tree-sitter lacks), which the class owns.
    base = {"nodes": [_n("impl", "CartServiceImpl", "svc/CartServiceImpl.java", 25),
                      _n("clock", "TimeSource", "cfg/TimeSource.java", 7),
                      _n("other_clock", "TimeSource", "legacy/TimeSource.java", 3)],
            "edges": [_e("impl", "clock", "references")]}
    scip = {"nodes": [_n("s_impl", "CartServiceImpl", "svc/CartServiceImpl.java", 24,
                         metadata={"scip_symbol": _sym("svc/CartServiceImpl#")}),
                      _n("s_field", "clock", "svc/CartServiceImpl.java", 30,
                         metadata={"scip_symbol": _sym("svc/CartServiceImpl#clock.")}),
                      _n("s_clock", "TimeSource", "cfg/TimeSource.java", 6,
                         metadata={"scip_symbol": _sym("cfg/TimeSource#")})],
            "edges": [_e("s_field", "s_clock", "references", context="scip")]}
    out = reconcile_scip(base, scip)
    assert out["reconciliation"]["demoted"] == 0
    # ...but had tree-sitter bound the class to the OTHER TimeSource, that contradicts SCIP
    base["edges"] = [_e("impl", "other_clock", "references")]
    for n in base["nodes"]:
        n.pop("metadata", None); n.pop("kind", None)
    out = reconcile_scip(base, scip)
    assert out["reconciliation"]["demoted"] == 1


def test_constructor_call_confirms_tree_sitter_edge_to_the_class():
    # `new X()`: tree-sitter targets the class, SCIP the constructor X#`<init>`().
    base = {"nodes": [_n("m", ".find()", "svc/S.java", 10), _n("x", "XException", "svc/XException.java", 3),
                      _n("x_ctor", ".XException()", "svc/XException.java", 5)],
            "edges": [_e("m", "x", "calls")]}
    scip = {"nodes": [_n("s_m", "find", "svc/S.java", 9, metadata={"scip_symbol": _sym("svc/S#find().")}),
                      _n("s_x", "XException", "svc/XException.java", 2, metadata={"scip_symbol": _sym("svc/XException#")}),
                      _n("s_ctor", "init", "svc/XException.java", 4,
                         metadata={"scip_symbol": _sym("svc/XException#`<init>`().")})],
            "edges": [_e("s_m", "s_ctor", "calls", context="scip")]}
    out = reconcile_scip(base, scip)
    assert out["reconciliation"]["demoted"] == 0


def test_call_through_interface_confirms_edge_to_the_implementation():
    base = {"nodes": [_n("t", ".fanOut_test()", "svc/ImplTest.java", 20),
                      _n("api", ".broadcast()", "svc/Service.java", 4), _n("impl", ".broadcast()", "svc/ServiceImpl.java", 30)],
            "edges": [_e("t", "impl", "calls"), _e("impl", "api", "implements")]}
    scip = {"nodes": [_n("s_t", "fanOut_test", "svc/ImplTest.java", 19,
                         metadata={"scip_symbol": _sym("svc/ImplTest#fanOut_test().")}),
                      _n("s_api", "broadcast", "svc/Service.java", 3, metadata={"scip_symbol": _sym("svc/Service#broadcast().")})],
            "edges": [_e("s_t", "s_api", "calls", context="scip")]}
    out = reconcile_scip(base, scip)
    assert out["reconciliation"]["demoted"] == 0


def test_external_symbol_defined_by_an_earlier_merged_module_folds_onto_it():
    # module A (a shared library) was merged first and defines Perm#; module B's
    # index references it as an external symbol with the identical string.
    perm_sym = "semanticdb maven maven/com.acme/auth 1.0 com/acme/auth/RequiresRole#"
    base = {"nodes": [_n("perm", "RequiresRole", "libs/auth/RequiresRole.java", 4,
                         metadata={"scip_symbol": perm_sym}),
                      _n("h", ".onEvent()", "svc-b/Handler.java", 12)],
            "edges": [_e("h", "perm", "references")]}
    scip = {"nodes": [_n("s_h", "onEvent", "svc-b/Handler.java", 11,
                         metadata={"scip_symbol": _sym("svc/Handler#onEvent().")}),
                      {"id": "s_perm", "label": "RequiresRole", "file_type": "code",
                       "source_file": "svc-b/Handler.java", "source_location": "",
                       "metadata": {"scip_symbol": perm_sym, "scip_external": "true"}}],
            "edges": [_e("s_h", "s_perm", "references", context="scip")]}
    out = reconcile_scip(base, scip)
    assert out["reconciliation"]["external_folded"] == 1
    assert out["reconciliation"]["demoted"] == 0
    assert "s_perm" not in {n["id"] for n in out["nodes"]}


def test_record_type_keeps_its_own_symbol_not_its_constructor():
    # `public record Created(...)`: SCIP defines Created# and Created#`<init>`(). on one line
    base = {"nodes": [_n("rec", "Created", "ev/Created.java", 5)], "edges": []}
    scip = {"nodes": [_n("s_t", "Created", "ev/Created.java", 4, metadata={"scip_symbol": _sym("ev/Created#")}),
                      _n("s_c", "init", "ev/Created.java", 4,
                         metadata={"scip_symbol": _sym("ev/Created#`&lt;init&gt;`().")})], "edges": []}
    out = reconcile_scip(base, scip)
    rec = {n["id"]: n for n in out["nodes"]}["rec"]
    assert rec["metadata"]["scip_symbol"].endswith("ev/Created#")


def test_escaped_constructor_symbol_still_joins_by_type_name():
    base = {"nodes": [_n("ctor", ".OrderService()", "svc/OrderService.java", 30)], "edges": []}
    scip = {"nodes": [_n("s_ctor", "init", "svc/OrderService.java", 31,
                         metadata={"scip_symbol": _sym("svc/OrderService#`&lt;init&gt;`().")})], "edges": []}
    assert reconcile_scip(base, scip)["reconciliation"]["matched"] == 1
