"""Tests for pg_query — the Postgres query layer, with an injected fake DB.

The SQL runs against a real pgvector Postgres in integration; here we verify the
Python glue (param binding, row parsing, delegation) offline via a fake connect
seam — the part that must be correct regardless of the database."""
from __future__ import annotations

from graphify.pg_query import (
    DEFAULT_IMPACT_RELATIONS,
    blast_radius_pg,
    discover_seeds_pg,
)


class _FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []
    def execute(self, sql, params=None):
        self.executed.append((sql, params))
    def fetchall(self):
        return self.rows
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, rows):
        self.cur = _FakeCursor(rows)
        self.closed = False
    def cursor(self):
        return self.cur
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def close(self):
        self.closed = True


def test_blast_radius_pg_binds_params_and_parses_rows():
    conn = _FakeConn([("svc_b_fn_caller", 1), ("svc_c_fn_other", 2)])
    out = blast_radius_pg("myrepo", "svc_a_fn_seed", depth=3,
                          connect=lambda dsn: conn)
    # rows parsed into dicts
    assert out == [{"symbol": "svc_b_fn_caller", "depth": 1},
                   {"symbol": "svc_c_fn_other", "depth": 2}]
    # params bound correctly, default relations passed as the rels array
    sql, params = conn.cur.executed[0]
    assert "WITH RECURSIVE impact" in sql
    assert params["seed"] == "svc_a_fn_seed"
    assert params["repo"] == "myrepo"
    assert params["depth"] == 3
    assert params["rels"] == list(DEFAULT_IMPACT_RELATIONS)
    assert conn.closed


def test_blast_radius_pg_honours_custom_relations():
    conn = _FakeConn([])
    blast_radius_pg("r", "s", relations=["calls_service"], connect=lambda dsn: conn)
    assert conn.cur.executed[0][1]["rels"] == ["calls_service"]


def test_blast_radius_pg_qualifies_schema_in_sql():
    conn = _FakeConn([])
    blast_radius_pg("r", "s", schema="graphify", connect=lambda dsn: conn)
    sql = conn.cur.executed[0][0]
    assert "graphify.code_edges" in sql          # schema-qualified table


def test_blast_radius_pg_rejects_bad_schema():
    import pytest
    with pytest.raises(ValueError):
        blast_radius_pg("r", "s", schema="evil; DROP TABLE x", connect=lambda dsn: _FakeConn([]))


class _FakeEmbedder:
    def embed(self, texts):
        return [[0.1, 0.2, 0.3] for _ in texts]


def test_discover_seeds_pg_embeds_query_and_delegates():
    seen = {}
    def fake_search(query, qvec, *, repo, dsn, top_n, schema):
        seen.update(query=query, qvec=qvec, repo=repo, top_n=top_n, schema=schema)
        return [{"symbol_id": "s1", "name": "Foo"}]
    out = discover_seeds_pg("payment flow", repo="r", embedder=_FakeEmbedder(),
                            top_n=5, schema="graphify", search=fake_search)
    assert out == [{"symbol_id": "s1", "name": "Foo"}]
    assert seen["query"] == "payment flow"
    assert seen["qvec"] == [0.1, 0.2, 0.3]   # embedded once, passed through
    assert seen["repo"] == "r" and seen["top_n"] == 5 and seen["schema"] == "graphify"
