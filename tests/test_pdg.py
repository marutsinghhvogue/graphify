"""Tests for pdg — intra-procedural Python data dependence."""
from __future__ import annotations

from graphify.pdg import build_pdg


def _by_line(g):
    return {n["source_location"]: n["metadata"] for n in g["nodes"]
            if n["metadata"]["node_type"] != "function_entry"}


def test_def_use_chain():
    g = build_pdg(
        "def f(request, cursor):\n"
        "    uid = request.args.get('id')\n"
        "    q = 'SELECT ' + uid\n"
        "    cursor.execute(q)\n",
        path="v.py",
    )
    m = _by_line(g)
    assert m["L2"]["defs"] == ["uid"] and m["L2"]["uses"] == ["request"]  # attr names excluded
    assert m["L3"]["defs"] == ["q"] and m["L3"]["uses"] == ["uid"]
    assert m["L4"]["defs"] == [] and m["L4"]["uses"] == ["cursor", "q"]


def test_data_dep_edges_follow_the_value():
    g = build_pdg(
        "def f(request, cursor):\n"
        "    uid = request.args.get('id')\n"
        "    q = 'SELECT ' + uid\n"
        "    cursor.execute(q)\n",
        path="v.py",
    )
    deps = {(e["source"].rsplit("_", 1)[-1], e["target"].rsplit("_", 1)[-1], e["metadata"]["var"])
            for e in g["edges"] if e["relation"] == "data_dep"}
    # entry(0)->uid(1) via request ; uid(1)->q(2) via uid ; q(2)->execute(3) via q
    assert ("0", "1", "request") in deps
    assert ("1", "2", "uid") in deps
    assert ("2", "3", "q") in deps
    assert all(e["confidence"] == "INFERRED" for e in g["edges"])


def test_params_are_entry_definitions():
    g = build_pdg("def f(a, b):\n    c = a + b\n    return c\n", path="v.py")
    # a,b defined at entry; c depends on both
    vars_into_c = {e["metadata"]["var"] for e in g["edges"]
                   if e["relation"] == "data_dep" and e["target"].endswith("_1")}
    assert {"a", "b"} <= vars_into_c


def test_reassignment_uses_latest_definition():
    g = build_pdg(
        "def f():\n"
        "    x = 1\n"
        "    x = 2\n"
        "    y = x\n",
        path="v.py",
    )
    # y should depend on the SECOND x (stmt 2), not the first (stmt 1)
    y_edge = next(e for e in g["edges"] if e["relation"] == "data_dep" and e["target"].endswith("_3"))
    assert y_edge["source"].endswith("_2")


def test_empty_and_no_function():
    assert build_pdg("x = 1\n", path="v.py")["nodes"] == []   # module-level only, no function
    g = build_pdg("def f():\n    pass\n", path="v.py")
    assert any(n["metadata"]["node_type"] == "function_entry" for n in g["nodes"])
