# Postgres CodeGraph — architecture & plan

**Status:** accepted architecture. `pg_export.py` (structural sink) implemented;
semantic (`code_chunks`) and graph-analytics wiring are on the roadmap.

## Objective

Serve the **PRD → execution-plan** pipeline (discover where a change lands →
compute blast radius across services → plan) from a **single, battle-tested
store**. Graphify is a stateless **extractor**, not a datastore: it parses code
and emits `{nodes, edges}`; those edges are persisted into Postgres alongside
the semantic vectors/FTS.

```
  code ──▶ Graphify / SCIP / contract extractor  (stateless step)
                          │  {nodes, edges}
                          ▼
                     Postgres  (single store)
       ┌───────────────────────┬───────────────────────────┐
       │ code_chunks           │ code_edges                 │
       │ embeddings (pgvector) │ structural relationships   │
       │ + FTS (tsvector)      │ (calls/impl/imports/…)     │
       │  → discovery (stage2) │  → blast radius (stage3)    │
       └───────────────────────┴───────────────────────────┘
                          ▲ shared identity: symbols(repo, symbol_id)
```

Why this shape:

- **Graphify stays stateless** — plays to its strength (parsing), avoids the
  "is Graphify a database?" problem. (`pg_export.py` mirrors the existing
  `pg_introspect.py`, which already speaks Postgres.)
- **Discovery + structure co-located** — a single query finds semantic seeds
  *and* joins to edges for impact. One store, one query surface, transactional,
  incremental, concurrent.
- **No separate graph DB / vector DB** to keep in sync with a `graph.json`.

## Schema

`pg_export.py` owns the **structural core** (`symbols` + `code_edges`); the
semantic pipeline owns `code_chunks`. Structural core requires **no pgvector**,
so it runs on vanilla Postgres.

```sql
-- IDENTITY ANCHOR (owned by pg_export; upserted, never deleted on re-export)
CREATE TABLE symbols (
    repo         text NOT NULL,
    symbol_id    text NOT NULL,   -- Graphify canonical node id
    path         text,
    name         text,
    kind         text,            -- class/interface/method/function/type/... (from SCIP merge)
    source_line  integer,
    scip_symbol  text,            -- SCIP descriptor when available
    PRIMARY KEY (repo, symbol_id)
);

-- STRUCTURAL EDGES (owned by pg_export)
CREATE TABLE code_edges (
    id                bigserial PRIMARY KEY,
    repo              text NOT NULL,
    src_symbol        text NOT NULL,
    edge_type         text NOT NULL,   -- calls|references|imports|implements|inherits|contains|calls_service|handles
    dst_symbol        text,            -- when target is a known symbol
    target            text,            -- raw target when unresolved (external URL, cross-service endpoint)
    confidence        text,            -- EXTRACTED|INFERRED|AMBIGUOUS
    confidence_score  real,
    source            text NOT NULL,   -- scip|treesitter|contract|llm  (replace-scope key)
    source_file       text,
    source_location   text
);
CREATE INDEX code_edges_dst_idx  ON code_edges (repo, dst_symbol, edge_type);  -- callers
CREATE INDEX code_edges_src_idx  ON code_edges (repo, src_symbol, edge_type);  -- callees
CREATE INDEX code_edges_repo_source_idx ON code_edges (repo, source);          -- replace-scope

-- SEMANTIC (owned by the discovery pipeline; needs pgvector)
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE code_chunks (
    repo       text NOT NULL,
    symbol_id  text NOT NULL,
    path       text,
    embedding  vector,       -- HNSW index
    fts        tsvector,     -- GIN index
    PRIMARY KEY (repo, symbol_id),
    FOREIGN KEY (repo, symbol_id) REFERENCES symbols(repo, symbol_id) ON DELETE CASCADE
);
```

### Incremental / write model

- **Symbols** are **upserted** (COALESCE-merged) so no source clobbers another's
  `kind`/`scip_symbol`, and structural re-export never touches the embeddings in
  `code_chunks`.
- **Edges** are replaced per **`(repo, source)`** — re-exporting the `scip` edges
  replaces only `scip` edges, leaving `contract` / `treesitter` intact.

### Multiple repositories

`repo` is the leading column of every table (and every index), so one database is
a **multi-repo store** with no cross-talk: `export-pg --repo A` and `--repo B`
coexist, and `blast_radius_pg` / `discover_seeds_pg` scope to one `repo` value.

- **Independent repos** — populate and query each by name.
- **Cross-service estate** — `blast_radius_pg` traverses within a single `repo`
  (`WHERE e.repo = %(repo)s`), so for reachability that crosses service
  boundaries, export a **stitched estate graph** (one that carries `calls_service`
  edges — `graphify extract --cross-service`, `global_graph`, or a CodeGraph
  ingest) under a single estate `repo` name. The `calls_service` / `handles`
  edge types are already part of the traversal set.

See [deployment-railway.md](./deployment-railway.md) for loading multiple repos
into a live deployment.

## Accepted mitigations

These are the known limits of "Postgres as the code-graph store," accepted with
explicit mitigations rather than ignored.

### M1 — Postgres is a store, not a graph-analytics engine

- **Bounded blast radius** (depth 2–3, Graphify's default) → `WITH RECURSIVE`
  CTEs with a visited-set `UNION` for cycle-guarding. Fine at this depth.
- **Deep/whole-graph analytics** — community detection (Louvain), centrality,
  god-nodes — are **not** done in SQL. They load the relevant subgraph into
  NetworkX/graspologic on demand (Graphify's existing `cluster.py`/`analyze.py`,
  reading from Postgres instead of `graph.json`).
- **Rule:** SQL for identity, lookups, and shallow reachability; in-memory graph
  for analytics. Don't force Louvain into SQL.

### M2 — Symbol identity is the make-or-break key

`code_edges.src_symbol` must reference the same identity as `code_chunks.symbol_id`
*and* the SCIP descriptor, or the discovery→structure join silently breaks.

- **Mitigation:** the `symbols` table is the single anchor. `symbol_id` =
  Graphify's canonical node id (per `(repo, symbol_id)`); `scip_symbol` carries
  the SCIP descriptor where available (the globally-stable key for
  SCIP-covered languages). All producers upsert into `symbols` with the same
  normalization the merge-on-identity join uses.
- `dst_symbol` is nullable with a raw `target` fallback, so an edge to an
  external/unresolved/cross-service target is **kept, never dropped**.

### M3 — Confidence/provenance must be first-class columns

The precision/recall tiering (`EXTRACTED` vs `INFERRED` vs `AMBIGUOUS`, the
demote-not-delete policy, SCIP-vs-heuristic) dies if edges are untyped.

- **Mitigation:** `confidence`, `confidence_score`, `source` are columns.
  Precision consumers filter `WHERE confidence = 'EXTRACTED'`; blast radius
  includes all tiers. Nothing is amputated silently.

## How the built pieces map

| Producer | → Postgres |
|---|---|
| `reconcile_scip` (SCIP merge) | `symbols` (kind, scip_symbol) + `code_edges` `source='scip'`, `EXTRACTED` |
| `contract_introspect` (cross-service) | `code_edges` `edge_type='calls_service'`, `dst_symbol` NULL + `target`, `INFERRED`/`AMBIGUOUS` |
| tree-sitter extraction | `symbols` + `code_edges` `source='treesitter'` |
| `find_callers`/`callees` | `SELECT … WHERE edge_type='calls' AND dst_symbol=? / src_symbol=?` |
| blast radius (`affected.py`) | `WITH RECURSIVE` over `code_edges` (bounded) |
| communities / god-nodes | load subgraph → NetworkX (M1) |

### Example queries

```sql
-- callers of a symbol (precise only)
SELECT src_symbol FROM code_edges
WHERE repo = $1 AND dst_symbol = $2 AND edge_type = 'calls'
  AND confidence = 'EXTRACTED';

-- bounded blast radius (reverse reachability, depth ≤ 3)
WITH RECURSIVE impact(symbol, depth) AS (
    SELECT $2, 0
  UNION
    SELECT e.src_symbol, i.depth + 1
    FROM code_edges e JOIN impact i ON e.dst_symbol = i.symbol
    WHERE e.repo = $1 AND i.depth < 3
      AND e.edge_type IN ('calls','references','implements','calls_service')
)
SELECT DISTINCT symbol, min(depth) FROM impact GROUP BY symbol;

-- discovery → structure (stage 2 → 3): semantic seeds, then their callers
WITH seeds AS (
    SELECT symbol_id FROM code_chunks
    WHERE repo = $1 ORDER BY embedding <=> $2 LIMIT 10   -- pgvector nearest
)
SELECT e.* FROM code_edges e JOIN seeds s ON e.dst_symbol = s.symbol_id;
```

## `pg_export.py` API

```python
from graphify.pg_export import export_to_postgres
export_to_postgres(extraction, repo="user_service", source="scip", dsn=None)
# dsn=None → libpq PG* env vars; returns {"symbols": n, "edges": m}
```
Pure core (unit-tested, no I/O): `extraction_to_rows(extraction, repo, source)`.

## Roadmap

1. **CLI wiring** — `graphify export-pg <graph.json> --repo R --source S [--dsn …]`.
2. **Semantic pipeline** (#130) — populate `code_chunks` (embeddings + FTS),
   keyed to `symbols.symbol_id`.
3. **Query layer** — recursive-CTE blast radius + discovery→structure join as
   MCP tools / service endpoints (M1 in-memory load for analytics).
4. **Cross-service** — wire `contract_introspect` output through `pg_export`
   (`source='contract'`).
5. **Per-caller demote** — SCIP recall-safety before edges land (retag, keep).
