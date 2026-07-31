"""Tests for pg_chunks — the pure code_chunks row/schema core (no DB, no network).

Mirrors test_pg_export: exercise the I/O-free mapping and DDL generation; the
psycopg writer/search are integration-only (need a pgvector database)."""
from __future__ import annotations

import pytest

from graphify.pg_chunks import _vec_literal, chunk_rows, schema_sql
from graphify.semantic_index import Chunk


def _chunk(sid, name="Foo", path="a/foo.py", kind="function", text="foo bar"):
    return Chunk(symbol_id=sid, name=name, path=path, kind=kind, text=text)


def test_chunk_rows_maps_chunk_plus_embedding():
    chunks = [_chunk("s1", text="payment controller"), _chunk("s2", name="Auth")]
    embs = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    rows = chunk_rows(chunks, embs, repo="svc")
    assert len(rows) == 2
    repo, sid, name, path, kind, text, vec = rows[0]
    assert (repo, sid, text) == ("svc", "s1", "payment controller")
    assert vec == "[0.1,0.2,0.3]"          # pgvector text literal


def test_chunk_rows_rejects_misaligned_lengths():
    with pytest.raises(ValueError):
        chunk_rows([_chunk("s1")], [[0.1], [0.2]], repo="r")


def test_vec_literal_format():
    assert _vec_literal([1, 2, 3]) == "[1.0,2.0,3.0]"


def test_schema_sql_dimensions_the_vector_column():
    sql = schema_sql(1536)
    assert "vector(1536)" in sql
    assert "CREATE EXTENSION IF NOT EXISTS vector" in sql
    assert "USING hnsw" in sql and "USING GIN" in sql
    assert "PRIMARY KEY (repo, symbol_id)" in sql


def test_schema_sql_qualifies_a_named_schema():
    sql = schema_sql(256, "graphify")
    assert "CREATE SCHEMA IF NOT EXISTS graphify" in sql
    assert "graphify.code_chunks" in sql
    assert "ON graphify.code_chunks" in sql   # indexes qualified too


def test_schema_sql_rejects_injection():
    with pytest.raises(ValueError):
        schema_sql(256, "public; DROP TABLE users")
