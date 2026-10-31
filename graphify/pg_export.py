"""pg_export.py — persist Graphify's structural extraction into Postgres.

The counterpart to ``pg_introspect.py`` (which *reads* a database): this *writes*
Graphify's ``{nodes, edges}`` output into a Postgres code-graph so Graphify stays
a stateless **extractor**, not a datastore. Structural edges live alongside the
semantic ``code_chunks`` (embeddings + FTS) in one Postgres, so discovery and
blast radius share a single store.

Two tables are owned here (the semantic pipeline owns ``code_chunks``):

  symbols(repo, symbol_id, path, name, kind, source_line, scip_symbol)
     — the IDENTITY ANCHOR. ``symbol_id`` is Graphify's canonical node id;
       ``scip_symbol`` carries the SCIP descriptor when available. Upserted
       (never deleted on structural re-export) so it stays the shared key that
       code_chunks and code_edges both reference.

  code_edges(repo, src_symbol, edge_type, dst_symbol|target, confidence,
             confidence_score, source, source_file, source_location)
     — structural relationships. ``dst_symbol`` when the target is a known
       symbol; otherwise NULL and ``target`` holds the raw target (external
       URL, cross-service endpoint, unresolved call) so edges are never
       dropped. Confidence is a first-class column, preserving the
       EXTRACTED/INFERRED/AMBIGUOUS precision/recall tiering.

Incremental model: edges are replaced per ``(repo, source)`` — re-exporting the
'scip' edges for a repo replaces only its 'scip' edges, leaving 'contract' /
'treesitter' edges intact. Symbols are upserted (COALESCE-merged) so no source
clobbers another's kind/scip_symbol, and embeddings in code_chunks are never
touched by a structural re-export.

Requires the 'postgres' extra (psycopg). The import is lazy so this module
stays importable without it.
"""
from __future__ import annotations

import re
from typing import Any

# A Postgres schema name. Validated (identifiers can't be parameterized with %s),
# so a --schema value can't inject SQL. Lets graphify's tables live in a dedicated
# schema (e.g. `graphify`) on a shared database without colliding with other apps.
_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def safe_schema(schema: str | None) -> str:
    s = (schema or "public").strip()
    if not _SCHEMA_RE.fullmatch(s):
        raise ValueError(f"invalid schema name {schema!r}; must match [a-z_][a-z0-9_]* (lowercase)")
    return s


def core_schema_sql(schema: str = "public") -> str:
    """Structural core DDL in ``schema``. Idempotent; no pgvector dependency
    (embeddings live in code_chunks, owned by the semantic pipeline)."""
    s = safe_schema(schema)
    return f"""
CREATE SCHEMA IF NOT EXISTS {s};

CREATE TABLE IF NOT EXISTS {s}.symbols (
    repo         text NOT NULL,
    symbol_id    text NOT NULL,
    path         text,
    name         text,
    kind         text,
    source_line  integer,
    scip_symbol  text,
    PRIMARY KEY (repo, symbol_id)
);

CREATE TABLE IF NOT EXISTS {s}.code_edges (
    id                bigserial PRIMARY KEY,
    repo              text NOT NULL,
    src_symbol        text NOT NULL,
    edge_type         text NOT NULL,
    dst_symbol        text,
    target            text,
    confidence        text,
    confidence_score  real,
    source            text NOT NULL,
    source_file       text,
    source_location   text
);

CREATE INDEX IF NOT EXISTS code_edges_dst_idx ON {s}.code_edges (repo, dst_symbol, edge_type);
CREATE INDEX IF NOT EXISTS code_edges_src_idx ON {s}.code_edges (repo, src_symbol, edge_type);
CREATE INDEX IF NOT EXISTS code_edges_repo_source_idx ON {s}.code_edges (repo, source);
"""


def _symbol_upsert(schema: str) -> str:
    return f"""
INSERT INTO {schema}.symbols (repo, symbol_id, path, name, kind, source_line, scip_symbol)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (repo, symbol_id) DO UPDATE SET
    path        = EXCLUDED.path,
    name        = EXCLUDED.name,
    kind        = COALESCE(EXCLUDED.kind, {schema}.symbols.kind),
    source_line = COALESCE(EXCLUDED.source_line, {schema}.symbols.source_line),
    scip_symbol = COALESCE(EXCLUDED.scip_symbol, {schema}.symbols.scip_symbol)
"""


def _edge_insert(schema: str) -> str:
    return f"""
INSERT INTO {schema}.code_edges
    (repo, src_symbol, edge_type, dst_symbol, target,
     confidence, confidence_score, source, source_file, source_location)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def _parse_line(source_location: object) -> int | None:
    if isinstance(source_location, str) and source_location.startswith("L"):
        try:
            return int(source_location[1:])
        except ValueError:
            return None
    return None


def extraction_to_rows(
    extraction: dict[str, Any],
    repo: str,
    source: str,
) -> tuple[list[tuple], list[tuple]]:
    """Pure map from a Graphify ``{nodes, edges}`` extraction to
    ``(symbol_rows, edge_rows)`` tuples ready for Postgres. No I/O — this is the
    unit-testable core.

    ``source`` labels every edge from this extraction (e.g. 'scip', 'contract',
    'treesitter'), driving the per-(repo, source) replace on write.
    """
    nodes = extraction.get("nodes", [])
    edges = extraction.get("edges", extraction.get("links", []))
    node_ids = {n.get("id") for n in nodes}

    symbol_rows: list[tuple] = []
    for n in nodes:
        meta = n.get("metadata") or {}
        symbol_rows.append((
            repo,
            n.get("id"),
            n.get("source_file"),
            n.get("label"),
            n.get("kind") or meta.get("scip_kind"),
            _parse_line(n.get("source_location")),
            meta.get("scip_symbol"),
        ))

    edge_rows: list[tuple] = []
    for e in edges:
        dst = e.get("target")
        known = dst in node_ids
        edge_rows.append((
            repo,
            e.get("source"),
            e.get("relation"),
            dst if known else None,           # dst_symbol
            None if known else dst,           # target (raw, when unresolved)
            e.get("confidence"),
            e.get("confidence_score"),
            source,
            e.get("source_file"),
            e.get("source_location"),
        ))
    return symbol_rows, edge_rows


def _connect(dsn: str | None):
    try:
        import psycopg
    except ImportError:  # pragma: no cover - exercised only without the extra
        raise ImportError(
            "psycopg is required for pg_export. Install the 'postgres' extra: "
            "pip install 'graphifyy[postgres]'"
        ) from None
    try:
        return psycopg.connect(dsn or "")  # empty string = PG* env vars
    except psycopg.OperationalError as exc:
        info = psycopg.conninfo.conninfo_to_dict(dsn or "")
        where = f"{info.get('host', '?')}/{info.get('dbname', '?')}"
        raise ConnectionError(f"could not connect to PostgreSQL ({where}): {exc}") from None


def init_postgres(*, schema: str = "public", dsn: str | None = None) -> dict[str, str]:
    """Create graphify's schema + structural tables (no data). Idempotent — used to
    provision a dedicated schema on a shared database before any export."""
    s = safe_schema(schema)
    conn = _connect(dsn)
    with conn, conn.cursor() as cur:
        cur.execute(core_schema_sql(s))
    conn.close()
    return {"schema": s, "tables": "symbols, code_edges"}


def export_to_postgres(
    extraction: dict[str, Any],
    *,
    repo: str,
    source: str,
    dsn: str | None = None,
    schema: str = "public",
) -> dict[str, int]:
    """Write a Graphify extraction into Postgres (in ``schema``, default public).

    Upserts ``symbols`` (identity anchor; never clobbers another source's kind/
    scip_symbol) and replaces ``code_edges`` for this ``(repo, source)`` pair.
    ``dsn=None`` uses libpq ``PG*`` env vars. Returns counts written.
    """
    s = safe_schema(schema)
    symbol_rows, edge_rows = extraction_to_rows(extraction, repo, source)
    conn = _connect(dsn)
    with conn:
        with conn.cursor() as cur:
            cur.execute(core_schema_sql(s))
            # Replace only this source's edges — leaves other sources intact.
            cur.execute(f"DELETE FROM {s}.code_edges WHERE repo = %s AND source = %s", (repo, source))
            if symbol_rows:
                cur.executemany(_symbol_upsert(s), symbol_rows)
            if edge_rows:
                cur.executemany(_edge_insert(s), edge_rows)
    conn.close()
    return {"symbols": len(symbol_rows), "edges": len(edge_rows)}
