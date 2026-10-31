"""Tests for `graphify callers` / `graphify callees` CLI commands."""
from __future__ import annotations

import json

import pytest

import graphify.__main__ as mainmod


def _write_graph(tmp_path):
    graph_data = {
        "directed": False, "multigraph": False, "graph": {},
        "nodes": [
            {"id": "a", "label": "a()", "source_file": "m.py", "source_location": "L1", "community": 0},
            {"id": "b", "label": "b()", "source_file": "m.py", "source_location": "L2", "community": 0},
            {"id": "c", "label": "c()", "source_file": "m.py", "source_location": "L3", "community": 0},
            {"id": "cfg", "label": "cfg()", "source_file": "m.py", "source_location": "L4", "community": 0},
        ],
        "links": [
            {"source": "a", "target": "b", "relation": "calls", "confidence": "EXTRACTED",
             "source_file": "m.py", "source_location": "L10"},
            {"source": "a", "target": "c", "relation": "calls", "confidence": "INFERRED",
             "source_file": "m.py", "source_location": "L11"},
            {"source": "b", "target": "c", "relation": "calls", "confidence": "EXTRACTED",
             "source_file": "m.py", "source_location": "L20"},
            # non-call edge must be excluded from both directions
            {"source": "a", "target": "cfg", "relation": "imports", "confidence": "EXTRACTED"},
        ],
    }
    p = tmp_path / "graph.json"
    p.write_text(json.dumps(graph_data))
    return p


def _run(monkeypatch, graph_path, cmd, label, capsys):
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(mainmod.sys, "argv",
        ["graphify", cmd, label, "--graph", str(graph_path)])
    mainmod.main()
    return capsys.readouterr().out


def test_callees_lists_outgoing(monkeypatch, tmp_path, capsys):
    p = _write_graph(tmp_path)
    out = _run(monkeypatch, p, "callees", "a", capsys)
    assert "a() calls 2 function(s):" in out
    assert "--> b() (m.py:L10) [EXTRACTED]" in out
    assert "--> c() (m.py:L11) [INFERRED]" in out
    assert "imports" not in out  # non-call edge excluded


def test_callers_lists_incoming(monkeypatch, tmp_path, capsys):
    p = _write_graph(tmp_path)
    out = _run(monkeypatch, p, "callers", "c", capsys)
    assert "2 function(s) call c():" in out
    assert "<-- a()" in out
    assert "<-- b()" in out


def test_callers_none(monkeypatch, tmp_path, capsys):
    p = _write_graph(tmp_path)
    out = _run(monkeypatch, p, "callers", "a", capsys)
    assert "Nothing calls a()" in out


def test_unknown_node(monkeypatch, tmp_path, capsys):
    p = _write_graph(tmp_path)
    out = _run(monkeypatch, p, "callees", "zzz", capsys)
    assert "No node matching 'zzz' found." in out


def test_missing_graph_exits(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    monkeypatch.setattr(mainmod.sys, "argv",
        ["graphify", "callers", "a", "--graph", str(tmp_path / "nope.json")])
    with pytest.raises(SystemExit):
        mainmod.main()
    assert "graph file not found" in capsys.readouterr().err
