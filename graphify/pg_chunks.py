"""pg_chunks.py — persist the Stage-2 semantic index into Postgres (code_chunks).

The persistence/scale counterpart to ``semantic_index.py``: real embeddings are
worth computing once and querying many times, so chunks + embeddings live in
Postgres alongside ``symbols``/``code_edges`` (see pg_export.py). This owns the
``code_chunks`` table the architecture reserves for the discovery pipeline:

  code_chunks(repo, symbol_id, name, path, kind, chunk_text,
              embedding vector(N), fts tsvector)  PRIMARY KEY (repo, symbol_id)

``symbol_id`` is the shared key back to ``symbols`` (and thus ``code_edges``), so
a discovered seed joins straight into blast radius. ``fts`` is a generated
tsvector; ``embedding`` is pgvector for nearest-neighbour. Hybrid search fuses
the two (vector ⊕ FTS) — the SQL analog of ``retrieve_seeds``.

Pure core (``chunk_rows``, ``schema_sql``) is unit-tested with no I/O; the writer
and search need the ``postgres`` extra (psycopg + a pgvector-enabled database).
"""
from __future__ import annotations

from typing import Any

from graphify.semantic_index import Chunk


def schema_sql(dim: int) -> str:
    """DDL for ``code_chunks`` at a fixed embedding dimension (pgvector columns
    are dimensioned at DDL time). Idempotent; requires the ``vector`` extension."""
    return f"""
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS code_chunks (
    repo         text NOT NULL,
    symbol_id    text NOT NULL,
    name         text,
    path         text,
    kind         text,
    chunk_text   text NOT NULL,
    embedding    vector({dim}),
    fts          tsvector GENERATED ALWAYS AS (to_tsvector('english', chunk_text)) STORED,
    PRIMARY KEY (repo, symbol_id)
);

CREATE INDEX IF NOT EXISTS code_chunks_fts_idx ON code_chunks USING GIN (fts);
CREATE INDEX IF NOT EXISTS code_chunks_vec_idx ON code_chunks
    USING hnsw (embedding vector_cosine_ops);
"""


def _vec_literal(embedding: list[float]) -> str:
    """pgvector text input form: ``[0.1,0.2,...]``."""
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


def chunk_rows(
    chunks: list[Chunk],
    embeddings: list[list[float]],
    repo: str,
) -> list[tuple]:
    """Pure map from chunks + their embeddings to ``code_chunks`` rows. No I/O —
    the unit-testable core. Raises if the counts don't line up."""
    if len(chunks) != len(embeddings):
        raise ValueError(f"chunks ({len(chunks)}) and embeddings ({len(embeddings)}) must align")
    rows: list[tuple] = []
    for c, emb in zip(chunks, embeddings):
        rows.append((
            repo, c.symbol_id, c.name, c.path, c.kind, c.text, _vec_literal(emb),
        ))
    return rows


_UPSERT = """
INSERT INTO code_chunks (repo, symbol_id, name, path, kind, chunk_text, embedding)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (repo, symbol_id) DO UPDATE SET
    name = EXCLUDED.name, path = EXCLUDED.path, kind = EXCLUDED.kind,
    chunk_text = EXCLUDED.chunk_text, embedding = EXCLUDED.embedding
"""


def _connect(dsn: str | None):
    try:
        import psycopg
    except ImportError:  # pragma: no cover - exercised only without the extra
        raise ImportError(
            "psycopg is required for pg_chunks. Install the 'postgres' extra: "
            "pip install 'graphifyy[postgres]'"
        ) from None
    try:
        return psycopg.connect(dsn or "")
    except psycopg.OperationalError as exc:
        info = psycopg.conninfo.conninfo_to_dict(dsn or "")
        where = f"{info.get('host', '?')}/{info.get('dbname', '?')}"
        raise ConnectionError(f"could not connect to PostgreSQL ({where}): {exc}") from None


def export_chunks_to_postgres(
    chunks: list[Chunk],
    embeddings: list[list[float]],
    *,
    repo: str,
    dsn: str | None = None,
) -> dict[str, int]:
    """Create ``code_chunks`` (at the embeddings' dimension) and upsert one row per
    chunk. Upsert (not replace) so a re-embed refreshes in place. ``dsn=None`` uses
    libpq ``PG*`` env vars. Returns the count written."""
    rows = chunk_rows(chunks, embeddings, repo)
    if not rows:
        return {"chunks": 0}
    dim = len(embeddings[0])
    conn = _connect(dsn)
    with conn:
        with conn.cursor() as cur:
            cur.execute(schema_sql(dim))
            cur.executemany(_UPSERT, rows)
    conn.close()
    return {"chunks": len(rows)}


_SEARCH = """
WITH v AS (
    SELECT symbol_id, name, path, kind,
           1 - (embedding <=> %(qvec)s::vector) AS vscore,
           ts_rank(fts, plainto_tsquery('english', %(qtext)s)) AS lscore
    FROM code_chunks
    WHERE repo = %(repo)s
)
SELECT symbol_id, name, path, kind, vscore, lscore
FROM v
ORDER BY (0.6 * vscore + 0.4 * (lscore / NULLIF(GREATEST(lscore, 0.0001), 0))) DESC
LIMIT %(top_n)s
"""


def search_chunks_postgres(
    query_text: str,
    query_embedding: list[float],
    *,
    repo: str,
    dsn: str | None = None,
    top_n: int = 10,
) -> list[dict[str, Any]]:
    """Hybrid seed search over persisted chunks: pgvector cosine ⊕ FTS rank. The
    SQL analog of ``retrieve_seeds`` for the embed-once/query-many path."""
    conn = _connect(dsn)
    with conn:
        with conn.cursor() as cur:
            cur.execute(_SEARCH, {
                "qvec": _vec_literal(query_embedding), "qtext": query_text,
                "repo": repo, "top_n": top_n,
            })
            cols = [d.name for d in cur.description]
            hits = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()
    return hits
