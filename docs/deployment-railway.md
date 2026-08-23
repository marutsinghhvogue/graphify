---
title: "Deploy Graphify as a service (Railway)"
subtitle: "A live REST API + MCP HTTP server backed by Postgres/pgvector, serving one or many repositories"
date: "August 2026"
---

# Overview

Graphify can run as a hosted service so agents and browsers query a **shared,
always-on** code graph instead of building one locally. A full deployment has
three parts:

```
Postgres (+ pgvector)        the multi-repo CodeGraph store (symbols, code_edges, code_chunks)
  ├─ graphify-api  (Flask)   REST: /api/v1/{impact,seeds,subgraph,stats,taint}
  └─ graphify-mcp  (HTTP)    MCP streamable-http transport at /mcp
```

Both app services run the **same image** and differ only by a `GRAPHIFY_ROLE`
env var (`api` | `mcp`). The image bakes a `graph.json`; the PG-backed tools
(`blast_radius_pg`, `discover_seeds_pg`) read Postgres and are the **multi-repo**
surface (see [Multiple repositories](#multiple-repositories)).

This guide documents the reference deployment on [Railway](https://railway.com),
but the image + env contract is portable to any container host.

# The image contract

`deploy/Dockerfile` + `deploy/entrypoint.sh` build one image with two roles:

| Role (`GRAPHIFY_ROLE`) | Command | Serves |
|---|---|---|
| `api` (default) | `graphify serve-web --graph /app/graph.json --host 0.0.0.0 --port $PORT` | Flask REST |
| `mcp` | `python -m graphify.serve /app/graph.json --transport http --host 0.0.0.0 --port $PORT --api-key $GRAPHIFY_API_KEY` | MCP HTTP |

On boot the **`api`** role also populates Postgres (idempotent):

```sh
graphify export-pg    /app/graph.json --repo "$GRAPHIFY_REPO" --source llm
graphify export-chunks /app/graph.json --repo "$GRAPHIFY_REPO" --embed hashing
```

`export-chunks`' DDL runs `CREATE EXTENSION IF NOT EXISTS vector`, so pgvector
self-provisions on any image whose role can create extensions (Railway's
`postgres-ssl` bundles it).

## Environment variables

| Var | On | Purpose |
|---|---|---|
| `GRAPHIFY_ROLE` | both | `api` or `mcp` |
| `GRAPHIFY_API_KEY` | both | gates every endpoint except `/health` (`X-API-Key` or `Authorization: Bearer`) |
| `GRAPHIFY_CORS_ORIGINS` | api | comma list or `*` for the browser host |
| `PGHOST` `PGPORT` `PGUSER` `PGPASSWORD` `PGDATABASE` | both | libpq vars; Graphify calls `psycopg.connect("")` so **no `--dsn` needed** |
| `RAILWAY_DOCKERFILE_PATH` | both | `deploy/Dockerfile` (Railway) |
| `GRAPHIFY_REPO` | api | repo name to populate as (default `graphify`) |
| `PORT` | both | injected by the host; the server binds it |

> **mcp version pin.** `deploy/Dockerfile` pins `mcp==1.27.1` (the `uv.lock`
> version). Newer `mcp` releases dropped `mcp.types.AnyUrl`, which `serve.py`
> imports — an unpinned install crashes the `mcp` role on startup.

# Provision on Railway

```bash
# 1. project + database
railway init --name graphify
railway add -d postgres

# 2. two app services
railway add -s graphify-api
railway add -s graphify-mcp

# 3. wire variables (repeat the PG* refs for both services)
KEY=$(openssl rand -hex 24)
for SVC in graphify-api graphify-mcp; do
  railway variable set \
    GRAPHIFY_API_KEY="$KEY" \
    RAILWAY_DOCKERFILE_PATH=deploy/Dockerfile \
    'PGHOST=${{Postgres.PGHOST}}' 'PGPORT=${{Postgres.PGPORT}}' \
    'PGUSER=${{Postgres.PGUSER}}' 'PGPASSWORD=${{Postgres.PGPASSWORD}}' \
    'PGDATABASE=${{Postgres.PGDATABASE}}' \
    --service "$SVC" --skip-deploys
done
railway variable set GRAPHIFY_ROLE=api  'GRAPHIFY_CORS_ORIGINS=*' --service graphify-api --skip-deploys
railway variable set GRAPHIFY_ROLE=mcp                            --service graphify-mcp --skip-deploys

# 4. bake the graph you want to serve, then deploy
graphify graphify/                       # or any repo path → graphify-out/graph.json
cp graphify/graphify-out/graph.json deploy/graph.json
railway up --service graphify-api --detach
railway up --service graphify-mcp --detach

# 5. public URLs
railway domain --service graphify-api --port 8080
railway domain --service graphify-mcp --port 8080
```

`.railwayignore` keeps the upload/build context small (excludes `.venv/`,
`tests/`, `docs/`, the local `graphify-out/`, etc.).

# Verify

```bash
# open
curl https://<api-domain>/health                         # {"status":"ok", ...}
# gated (401 without the key)
curl -H "X-API-Key: $KEY" https://<api-domain>/api/v1/stats

# the Postgres paths — run inside a service (internal DB DNS):
railway ssh -s graphify-api graphify blast-radius <seed_symbol> --repo graphify --depth 2
railway ssh -s graphify-api graphify search-chunks <word> --repo graphify --embed hashing --top 5
```

> `railway ssh` flattens argv — pass single-token args (a multi-word
> `"a b c"` query gets word-split). The MCP endpoint is at `/mcp` and returns
> `401` without the key.

# Multiple repositories

The Postgres schema is **keyed by `repo`** in every table
(`symbols` / `code_edges` / `code_chunks` — see
[POSTGRES_CODEGRAPH.md](./POSTGRES_CODEGRAPH.md)), and every PG tool takes
`--repo`. One database serves an entire estate. Two modes:

### Independent repos — query each on its own

Populate each repo into the same DB; query by name. No redeploy — the PG-backed
MCP tools pick up new repos immediately.

```bash
# from inside a service so postgres.railway.internal resolves:
railway ssh -s graphify-api sh   # then, per repo you've mounted/built:
graphify export-pg   <repo-B>/graph.json --repo billing --source llm
graphify export-chunks <repo-B>/graph.json --repo billing --embed hashing
graphify blast-radius <seed> --repo billing --depth 3
```

Or export from any machine that can reach the DB (`--dsn "$DATABASE_PUBLIC_URL"`).

### Cross-service estate — blast radius that crosses repo boundaries

`blast_radius_pg` traverses **within one `repo` value** (`WHERE e.repo = %(repo)s`).
To get reachability that crosses service boundaries, build one **stitched estate
graph** that carries `calls_service` edges, then export it under a single estate
name:

```bash
# each immediate subdir of ./estate is a service:
graphify extract --cross-service ./estate         # → graph.json with calls_service edges
graphify export-pg ./graphify-out/graph.json --repo estate --source llm
graphify blast-radius <seed> --repo estate --depth 3   # crosses services
```

Alternatives that produce a stitched graph: `graphify global add <graph.json>
--as <repo>` (a merged multi-repo graph for the file-graph tools) and `graphify
plan --root <dir>` / `--codegraph` (cross-service blast radius, incl. ingesting a
CodeGraph index). See [cross-service-impact-analysis.md](./cross-service-impact-analysis.md).

# Operational notes

- **Flask dev server.** `serve-web` uses Flask's built-in server — fine for
  demo/internal use. For real load, front it with a WSGI server (gunicorn) via
  the `web` extra.
- **Cost.** Each service bills continuously. `railway down --service <s>` stops a
  service; delete the project to stop everything.
- **The baked graph vs the DB.** File-graph tools (`find_callers`, `impact`,
  the REST `/api/v1/*`) serve the single baked `graph.json`; the `*_pg` tools
  serve any repo in Postgres. Rebake + redeploy to change the file graph; just
  `export-*` to add/refresh a repo in the DB.

# See also
- [POSTGRES_CODEGRAPH.md](./POSTGRES_CODEGRAPH.md) — the multi-repo schema & write model
- [code-intelligence-guide.md](./code-intelligence-guide.md) — the build → seeds → blast-radius loop
- [cross-service-impact-analysis.md](./cross-service-impact-analysis.md) — how cross-service edges are derived
