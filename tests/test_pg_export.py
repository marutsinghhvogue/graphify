"""Tests for pg_export — persisting Graphify extraction into Postgres."""
from __future__ import annotations

import sys
import types

import pytest

from graphify.pg_export import (
    core_schema_sql,
    export_to_postgres,
    extraction_to_rows,
    safe_schema,
)

_EXTRACTION = {
    "nodes": [
        {"id": "a", "label": "a()", "source_file": "m.py", "source_location": "L2",
         "kind": "function", "metadata": {"scip_symbol": "SYM_A"}},
        {"id": "b", "label": "b()", "source_file": "m.py", "source_location": "L9",
         "metadata": {"scip_kind": "method"}},   # kind falls back to scip_kind
    ],
    "edges": [
        {"source": "a", "target": "b", "relation": "calls", "confidence": "EXTRACTED",
         "confidence_score": 1.0, "context": "scip", "source_file": "m.py", "source_location": "L3"},
        {"source": "a", "target": "https://api.stripe.com/charges", "relation": "calls_service",
         "confidence": "INFERRED", "confidence_score": 0.9, "source_file": "m.py",
         "source_location": "L4"},
    ],
}


# --- pure mapping ---

def test_symbol_rows_carry_kind_line_and_scip_symbol():
    syms, _ = extraction_to_rows(_EXTRACTION, "repoX", "scip")
    by_id = {r[1]: r for r in syms}
    # (repo, symbol_id, path, name, kind, source_line, scip_symbol)
    assert by_id["a"] == ("repoX", "a", "m.py", "a()", "function", 2, "SYM_A")
    assert by_id["b"] == ("repoX", "b", "m.py", "b()", "method", 9, None)  # kind <- scip_kind


def test_edge_known_target_uses_dst_symbol():
    _, edges = extraction_to_rows(_EXTRACTION, "repoX", "scip")
    calls = next(e for e in edges if e[2] == "calls")
    # (repo, src, edge_type, dst_symbol, target, conf, score, source, file, loc)
    assert calls[3] == "b" and calls[4] is None          # resolved -> dst_symbol
    assert calls[5] == "EXTRACTED" and calls[7] == "scip"  # confidence + source label


def test_edge_unknown_target_kept_as_raw_target():
    _, edges = extraction_to_rows(_EXTRACTION, "repoX", "scip")
    ext = next(e for e in edges if e[2] == "calls_service")
    assert ext[3] is None                                   # no dst_symbol
    assert ext[4] == "https://api.stripe.com/charges"       # kept, never dropped
    assert ext[5] == "INFERRED"


def test_source_label_overrides_edge_context():
    _, edges = extraction_to_rows(_EXTRACTION, "repoX", "contract")
    assert all(e[7] == "contract" for e in edges)  # source column = param, not edge['context']


def test_links_key_accepted():
    ex = {"nodes": [{"id": "a", "label": "a", "source_file": "m", "source_location": "L1"}],
          "links": [{"source": "a", "target": "a", "relation": "self"}]}
    _, edges = extraction_to_rows(ex, "r", "s")
    assert len(edges) == 1


# --- write path (mocked psycopg) ---

class _Cur:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.log.append(("execute", sql, params))

    def executemany(self, sql, seq):
        self.log.append(("executemany", sql, list(seq)))


class _Conn:
    def __init__(self, log):
        self.log = log
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _Cur(self.log)

    def close(self):
        self.closed = True


def _fake_psycopg(log, capture_dsn=None):
    m = types.ModuleType("psycopg")

    def _connect(dsn):
        if capture_dsn is not None:
            capture_dsn.append(dsn)
        return _Conn(log)

    m.connect = _connect
    m.OperationalError = type("OperationalError", (Exception,), {})
    m.conninfo = types.SimpleNamespace(conninfo_to_dict=lambda d: {"host": "h", "dbname": "d"})
    return m


def test_export_replaces_by_repo_and_source_then_upserts(monkeypatch):
    log: list = []
    monkeypatch.setitem(sys.modules, "psycopg", _fake_psycopg(log))
    counts = export_to_postgres(_EXTRACTION, repo="repoX", source="scip", dsn="")

    assert counts == {"symbols": 2, "edges": 2}
    kinds = [row[0] for row in log]
    assert kinds == ["execute", "execute", "executemany", "executemany"]

    # schema first (default public, qualified), then a scoped delete
    assert "CREATE TABLE IF NOT EXISTS public.symbols" in log[0][1]
    assert "DELETE FROM public.code_edges WHERE repo = %s AND source = %s" in log[1][1]
    assert log[1][2] == ("repoX", "scip")
    # symbols upserted (2), edges inserted (2)
    assert "INSERT INTO public.symbols" in log[2][1] and len(log[2][2]) == 2
    assert "INSERT INTO public.code_edges" in log[3][1] and len(log[3][2]) == 2


def test_export_passes_dsn_through(monkeypatch):
    seen: list = []
    monkeypatch.setitem(sys.modules, "psycopg", _fake_psycopg([], capture_dsn=seen))
    export_to_postgres(_EXTRACTION, repo="r", source="s", dsn="postg:///mine")
    assert seen == ["postg:///mine"]


def test_core_schema_has_no_pgvector_dependency():
    # structural sink must run on vanilla Postgres (embeddings live elsewhere)
    sql = core_schema_sql()
    assert "vector" not in sql.lower()
    assert "symbols" in sql and "code_edges" in sql


def test_core_schema_qualifies_a_named_schema():
    sql = core_schema_sql("graphify")
    assert "CREATE SCHEMA IF NOT EXISTS graphify" in sql
    assert "graphify.symbols" in sql and "graphify.code_edges" in sql
    assert "ON graphify.code_edges" in sql   # indexes qualified


@pytest.mark.parametrize("bad", ["public; DROP TABLE x", "a-b", "1schema", "Graphify", "a b"])
def test_safe_schema_rejects_bad_names(bad):
    with pytest.raises(ValueError):
        safe_schema(bad)


def test_safe_schema_defaults_to_public():
    assert safe_schema(None) == "public"


# --- CLI: graphify export-pg ---

def _write_graph(tmp_path):
    import json
    p = tmp_path / "graph.json"
    p.write_text(json.dumps({
        "nodes": [{"id": "a", "label": "a()", "source_file": "m.py", "source_location": "L1"}],
        "links": [{"source": "a", "target": "a", "relation": "self",
                   "confidence": "EXTRACTED", "source_file": "m.py", "source_location": "L1"}],
    }))
    return p


def test_cli_export_pg(monkeypatch, tmp_path, capsys):
    import graphify.__main__ as mainmod
    log: list = []
    monkeypatch.setitem(sys.modules, "psycopg", _fake_psycopg(log))
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    gp = _write_graph(tmp_path)
    monkeypatch.setattr(mainmod.sys, "argv",
        ["graphify", "export-pg", str(gp), "--repo", "svcX", "--source", "scip"])
    mainmod.main()
    out = capsys.readouterr().out
    assert "repo=svcX, source=scip" in out
    assert "1 symbols, 1 edges" in out
    # scoped delete used the right (repo, source)
    assert any(k == "execute" and params == ("svcX", "scip")
               for (k, _sql, params) in log if k == "execute")


def test_cli_export_pg_requires_repo(monkeypatch, tmp_path, capsys):
    import graphify.__main__ as mainmod
    monkeypatch.setattr(mainmod, "_check_skill_version", lambda _: None)
    gp = _write_graph(tmp_path)
    monkeypatch.setattr(mainmod.sys, "argv", ["graphify", "export-pg", str(gp)])
    try:
        mainmod.main()
        raised = False
    except SystemExit:
        raised = True
    assert raised
    assert "--repo is required" in capsys.readouterr().err
