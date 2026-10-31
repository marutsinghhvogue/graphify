---
title: "User Guide: Code Intelligence (PRD → Plan)"
subtitle: "Find where a requirement lands, then compute its blast radius — across services, schedulers, events, and DI"
date: "July 2026"
---

This guide covers the code-intelligence features: cross-service edges, scheduler
/ event / DI detection, prose→code seed discovery, blast radius, and the Postgres
scale path. Commands use the `graphify` CLI (equivalently `python -m graphify …`).

---

## TL;DR — the whole loop in three commands

```bash
# 1. Build a graph that includes the edges the compiler can't see
graphify extract ./my-monorepo --cross-service --bindings --cloud-schedulers

# 2. Turn a requirement into the code that implements it (Stage 2)
graphify seeds "recompute the nightly billing rollup"

# 3. Compute the blast radius of the top seed (Stage 3)
graphify affected "rollup_daily" --depth 3
```

`seeds` tells you *where* to change; `affected` tells you *what else is impacted*
— including other services, scheduled jobs, event handlers, and injected types.

---

## 1. Build the graph

`graphify extract` runs tree-sitter (25 languages) for free. Add flags to layer
in the cross-boundary edges:

| Flag | Adds | Edge · confidence |
|---|---|---|
| `--cross-service` | HTTP consumer→producer across services (FastAPI/NestJS/Spring) | `calls_service` · INFERRED/AMBIGUOUS |
| `--schedulers` | In-code timers (`@Scheduled`, `@Cron`, `@scheduled_job`, `@periodic_task`) | `triggers` · EXTRACTED |
| `--bindings` | **All** framework constructs — schedulers + events + DI + your own rules (superset of `--schedulers`) | `triggers` / `consumes` / `injects` |
| `--cloud-schedulers` | Cloud/IaC schedules (Terraform, k8s CronJob, serverless.yml) | `triggers` · INFERRED |

```bash
graphify extract ./estate --cross-service --bindings --cloud-schedulers --out ./out
# → ./out/graphify-out/graph.json
```

> **Layout note:** `--cross-service` treats each immediate subdirectory of the
> path as a service. Point it at the directory that contains your services.

---

## 2. Stage 2 — find seeds from a prose requirement

`graphify seeds` maps a requirement written in English to the code symbols it
touches (BM25 over symbol names split into subtokens, paths, and doc text — so
"clean up stale orders" reaches `purgeStaleOrders`).

```bash
graphify seeds "clean up stale orders"                 # uses ./graphify-out/graph.json
graphify seeds "payment webhook handling" ./out/graphify-out/graph.json --top 5
```

Output ranks symbols with a match tag (`lexical` / `vector` / `both`):

```
Seed symbols for "clean up stale orders" (top 3):
  0.0164 [lexical] .purgeStaleOrders()  orders/cleanup.controller.ts
```

**Hybrid (paraphrase) recall** — add real embeddings:

```bash
export OPENAI_API_KEY=sk-...
graphify seeds "the checkout flow" --embed openai        # or --embed gemini
```

Default embedder is `hashing` (offline, deterministic, zero-cost). `openai` /
`gemini` need the respective API key.

---

## 3. Stage 3 — blast radius

`graphify affected` walks the graph *backwards* from a symbol: everything that
could be affected by changing it. It traverses in-process calls **and** every
cross-boundary edge (`calls_service`, `triggers`, `consumes`, `injects`).

```bash
graphify affected "rollup_daily" --depth 3
graphify affected "InventoryClient"          # who injects / depends on this type
```

```
Affected nodes for rollup_daily()
- "cron 0 2 * * *"  [triggers]  billing/report_job.py:L6     ← a nightly job runs it
```

Related in-memory commands: `graphify callers "<fn>"` / `graphify callees "<fn>"`.

### The whole chain in one call — `graphify plan`

`graphify plan "<requirement>" --root <dir>` runs Stages 1→3 and returns a change
plan: responsible services (with the *why*), seed symbols, the cross-service blast
radius, **the API contracts in the change surface + who consumes them (break
risk)**, and **third-party calls in the impacted code**.

```bash
graphify plan "let a customer update their profile and pay an invoice" --root ./services
```

```
Responsible services (Stage 1):
  2.83 [lexical] user_service   <- create_user()
  1.65 [lexical] order_service  <- doc: customer profile … charge/invoice from billing
Contracts changed — 3 endpoint(s) in the change surface:
  GET /users/{} {user_service}  ->  get_user()   user_service/main.py:L7
      ! BREAKING risk — consumed by: order_service
Third-party calls in the impacted code — 1 call(s):
  api.stripe.com  [GET] https://api.stripe.com/v1/charges {order_service}  <- getOrder()
```

- **Contracts changed** is graph-derived (routes/handlers in the surface + their
  `calls_service` consumers) — deterministic, no source needed.
- **Third-party calls** are scanned from the impacted services' source (needs
  `--root`); a call whose URL names an absolute host that no internal endpoint
  claims is flagged as an outbound dependency.
- `--detailed [BACKEND]` adds an LLM-written narrative (per-service what/why,
  contract migrations, ordered steps, risks) **over these grounded facts** — the
  model narrates verified structure, it doesn't invent the impact set.

---

## 4. Coverage & adding your own framework

Built-in constructs (all via `--bindings`):

| Construct | Frameworks | Edge |
|---|---|---|
| Scheduler | Spring `@Scheduled`, NestJS `@Cron`/`@Interval`/`@Timeout`, APScheduler, Celery | `triggers` |
| Event / message | Spring `@EventListener`, Kafka `@KafkaListener`, NestJS `@EventPattern`/`@MessagePattern` | `consumes` |
| Dependency injection | Spring `@Autowired`, JSR-330 `@Inject`, Jakarta `@Resource` (Java field/setter) | `injects` |

**Add a framework yourself — no code change.** Drop a rule into
`.graphify_binding_rules.json` at the scan root:

```json
{ "rules": [
  { "id": "event.sqs.listener", "category": "event", "provider": "sqs",
    "languages": ["java"], "pattern": "^\\s*@SqsListener\\b",
    "relation": "consumes", "node_kind": "topic" }
]}
```

Then `graphify extract . --bindings` picks it up. See
[binding-rules.md](./binding-rules.md) for the full schema.

**Let an LLM write the rule for you:**

```bash
export ANTHROPIC_API_KEY=...            # or --backend openai, etc.
graphify learn-bindings .               # dry run: proposes rules for uncovered annotations
graphify learn-bindings . --write       # persist the accepted rules
```

It finds annotations no rule covers, has the model author rules for the
scheduler/event ones, validates each, and (with `--write`) saves them for
deterministic execution.

---

## 5. Cloud / IaC schedules

`--cloud-schedulers` finds schedules bound in infrastructure, where a handler has
no in-code marker:

```bash
graphify extract ./estate --cloud-schedulers
```

Detects: Terraform (`aws_cloudwatch_event_rule`, `aws_scheduler_schedule`,
`google_cloud_scheduler_job`, azurerm logic-app), Kubernetes `CronJob`, and
`serverless.yml`. Emits `schedule` nodes (INFERRED) with the cron/rate expression;
serverless links the named handler.

> YAML (k8s/serverless) needs PyYAML: `pip install pyyaml`. Terraform works
> without it.

---

## 6. Serve the graph to an AI agent (MCP)

```bash
python -m graphify.serve ./out/graphify-out/graph.json
```

Tools your agent can call:

| Tool | Does |
|---|---|
| `discover_seeds` | Stage 2 — prose → ranked seed symbols |
| `blast_radius` | Stage 3 — impact of changing a symbol (cross-boundary, tiered) |
| `find_callers` / `find_callees` | call-graph navigation |
| `blast_radius_pg` | blast radius over persisted Postgres (see below) |
| `discover_seeds_pg` | hybrid seed search over persisted `code_chunks` |
| `query_graph`, `get_neighbors`, `shortest_path`, `god_nodes`, … | general graph queries |

A PRD→plan agent chains `discover_seeds` → `blast_radius` on the top seed, then
plans over the affected set.

---

## 7. Scale with Postgres

For a large or multi-repo estate, persist the graph and query it in SQL. Requires
the `postgres` extra (`pip install 'graphifyy[postgres]'`) and a pgvector-enabled
database. `--dsn` is optional — omit it to use libpq `PG*` env vars.

```bash
# persist structural edges + symbols
graphify export-pg ./out/graphify-out/graph.json --repo billing --source graphify

# embed symbols + persist code_chunks (pgvector + FTS)
graphify export-chunks ./out/graphify-out/graph.json --repo billing --embed openai

# query at scale
graphify blast-radius svc_billing_fn_rollup_daily --repo billing --depth 3
graphify search-chunks "nightly rollup" --repo billing --embed openai
```

> The embedder used at `export-chunks` time must match the one used at
> `search-chunks` / `discover_seeds_pg` time (same provider → comparable vectors).

### Multiple repositories

Every Postgres table is **keyed by `repo`**, and every PG tool takes `--repo`, so
one database serves a whole estate. Two modes:

```bash
# Independent repos — populate each, query each by name (no cross-talk):
graphify export-pg billing/graph.json  --repo billing --source graphify
graphify export-pg payments/graph.json --repo payments --source graphify
graphify blast-radius <seed> --repo billing --depth 3

# Cross-service estate — blast radius that crosses service boundaries.
# blast_radius_pg traverses within ONE repo value, so stitch the services into
# one graph (calls_service edges) and export under a single estate name:
graphify extract --cross-service ./estate           # subdir-per-service → calls_service edges
graphify export-pg ./graphify-out/graph.json --repo estate --source graphify
graphify blast-radius <seed> --repo estate --depth 3
```

For the file-graph tools (not Postgres), `graphify global add <graph.json> --as
<repo>` maintains a merged multi-repo graph; `graphify plan --root <dir>` (or
`--codegraph`) computes cross-service blast radius directly.

### A navigable wiki (flat or hierarchical)

Turn the graph into an agent-crawlable Markdown wiki:

```bash
graphify export wiki                 # flat: index.md + one article per community + god nodes
graphify export wiki --hierarchical  # module tree: overview.md + nested module articles
```

`--hierarchical` recursively decomposes the graph into a **module tree**
(`overview.md` → modules → sub-modules), writing one article per module plus a
`module_tree.json` (programmatic navigation) and `metadata.json` (commit +
params, the foundation for incremental regen). Tune it:

| Flag | Default | Effect |
|---|---|---|
| `--max-depth N` | `2` | recursion levels |
| `--max-nodes-per-module N` | `40` | split a module while it holds more nodes |
| `--min-module-size N` | `5` | sub-groups smaller than this fold into the parent |
| `--summarize BACKEND` | off | LLM authors a one-line blurb per module |

---

## 8. Confidence tiers — what to trust

Every edge is tagged, so you can trade precision for recall:

| Tier | Meaning | Use |
|---|---|---|
| **EXTRACTED** | Proven from syntax (a call, an import, a decorator binding) | Filter to this for precision |
| **INFERRED** | A grounded inference (cross-service match, name-resolved DI, cloud config) — with a score | Kept for blast radius |
| **AMBIGUOUS** | Uncertain (e.g. a path collision), fanned to all candidates | Never dropped; review before trusting |

Blast radius is **recall-first** — it keeps INFERRED/AMBIGUOUS so an affected path
is never silently missed. Filter the output to EXTRACTED when you need certainty.

---

## 9. Deploy as a service (Railway)

Run Graphify as an always-on service so agents and browsers query a **shared**
graph instead of building one locally. One image, two roles over the same
`graph.json`, backed by Postgres/pgvector:

```
Postgres (+ pgvector)      multi-repo store (symbols, code_edges, code_chunks)
  ├─ graphify-api  (Flask)  REST  /api/v1/{impact,seeds,subgraph,stats,taint}
  └─ graphify-mcp  (HTTP)   MCP streamable-http at /mcp
```

The `api` role populates Postgres on boot (`export-pg` + `export-chunks`,
idempotent); the `*_pg` MCP tools then serve **any repo** in the DB. Full setup,
env-var contract, verification, and multi-repo loading:
[deployment-railway.md](./deployment-railway.md).

---

## Requirements at a glance

| Feature | Needs |
|---|---|
| extract / cross-service / bindings / schedulers | nothing extra (tree-sitter) |
| cloud YAML (k8s/serverless) | `pip install pyyaml` |
| hybrid seeds (`--embed openai/gemini`), `learn-bindings` | provider API key |
| Postgres persistence & query (`export-*`, `blast-radius`, `search-chunks`) | `graphifyy[postgres]` + pgvector DB |
| Serve as a hosted service (REST + MCP) | `graphifyy[mcp,web,postgres]` + a container host |

## See also
- [deployment-railway.md](./deployment-railway.md) — deploy as a hosted REST + MCP service (multi-repo)
- [POSTGRES_CODEGRAPH.md](./POSTGRES_CODEGRAPH.md) — the multi-repo Postgres schema & write model
- [cross-service-impact-analysis.md](./cross-service-impact-analysis.md) — how cross-service edges are derived
- [binding-rules.md](./binding-rules.md) — the pluggable rule engine
- [scheduler-detection-design.md](./scheduler-detection-design.md) — Tier A + B design
- [architecture.html](./architecture.html) — the whole system at a glance
- [epic-refinements.md](./epic-refinements.md) — what's next
