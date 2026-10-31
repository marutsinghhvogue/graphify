"""pg_query.py — the Postgres query layer over persisted symbols/code_edges/chunks.

Once a repo (or a whole multi-repo estate) is exported via ``export-pg`` /
``export-chunks``, blast radius and seed discovery run in SQL, so they scale past
what a single in-memory ``graph.json`` holds:

- ``blast_radius_pg`` — bounded reverse reachability as a ``WITH RECURSIVE`` CTE
  (the ``UNION`` dedups → cycle guard; depth-bounded). Traverses the same
  cross-boundary relations as the in-memory tool.
- ``discover_seeds_pg`` — hybrid seed search over persisted ``code_chunks``
  (pgvector ⊕ FTS), the embed-once/query-many path.

Both take an injectable ``connect`` (and ``search``) seam so the param-binding and
row-parsing logic is unit-testable without a live database; the SQL itself is
exercised against a real pgvector Postgres.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from graphify.pg_chunks import search_chunks_postgres
from graphify.pg_export import safe_schema

# Relations along which impact propagates — the in-process call graph plus every
# cross-boundary entry-point edge (service / schedule / event / DI).
DEFAULT_IMPACT_RELATIONS: tuple[str, ...] = (
    "calls", "references", "implements", "inherits", "extends",
    "calls_service", "triggers", "consumes", "injects",
)

# Reverse reachability: from the seed, follow edges whose *target* is already in
# the impact set, adding their *source* (the dependent). UNION (not UNION ALL)
# deduplicates, bounding cycles; depth caps the walk.
def _blast_sql(schema: str) -> str:
    return f"""
WITH RECURSIVE impact(symbol, depth) AS (
        SELECT %(seed)s::text, 0
    UNION
        SELECT e.src_symbol, i.depth + 1
        FROM {schema}.code_edges e
        JOIN impact i ON e.dst_symbol = i.symbol
        WHERE e.repo = %(repo)s
          AND i.depth < %(depth)s
          AND e.edge_type = ANY(%(rels)s)
)
SELECT symbol, min(depth) AS depth
FROM impact
WHERE symbol <> %(seed)s
GROUP BY symbol
ORDER BY depth, symbol
"""


def _default_connect(dsn: str | None):
    try:
        import psycopg
    except ImportError:  # pragma: no cover - exercised only without the extra
        raise ImportError(
            "psycopg is required for pg_query. Install the 'postgres' extra: "
            "pip install 'graphifyy[postgres]'"
        ) from None
    try:
        return psycopg.connect(dsn or "")
    except psycopg.OperationalError as exc:
        info = psycopg.conninfo.conninfo_to_dict(dsn or "")
        where = f"{info.get('host', '?')}/{info.get('dbname', '?')}"
        raise ConnectionError(f"could not connect to PostgreSQL ({where}): {exc}") from None


def blast_radius_pg(
    repo: str,
    seed_symbol: str,
    *,
    depth: int = 3,
    relations: list[str] | tuple[str, ...] | None = None,
    dsn: str | None = None,
    schema: str = "public",
    connect: Callable | None = None,
) -> list[dict[str, Any]]:
    """Bounded reverse-reachability blast radius for ``seed_symbol`` in ``repo``,
    computed in Postgres (``schema``). Returns ``[{symbol, depth}, ...]``."""
    s = safe_schema(schema)
    rels = list(relations or DEFAULT_IMPACT_RELATIONS)
    conn = (connect or _default_connect)(dsn)
    with conn:
        with conn.cursor() as cur:
            cur.execute(_blast_sql(s), {"seed": seed_symbol, "repo": repo,
                                        "depth": int(depth), "rels": rels})
            rows = [{"symbol": r[0], "depth": r[1]} for r in cur.fetchall()]
    conn.close()
    return rows


def discover_seeds_pg(
    query: str,
    *,
    repo: str,
    embedder,
    dsn: str | None = None,
    top_n: int = 10,
    schema: str = "public",
    search: Callable | None = None,
) -> list[dict[str, Any]]:
    """Hybrid seed search over persisted ``code_chunks`` (``schema``): embed the
    prose query with the same ``embedder`` used at export time, then fuse pgvector
    cosine with FTS."""
    qvec = embedder.embed([query])[0]
    run = search or search_chunks_postgres
    return run(query, qvec, repo=repo, dsn=dsn, top_n=top_n, schema=schema)
