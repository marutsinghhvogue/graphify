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

## Requirements at a glance

| Feature | Needs |
|---|---|
| extract / cross-service / bindings / schedulers | nothing extra (tree-sitter) |
| cloud YAML (k8s/serverless) | `pip install pyyaml` |
| hybrid seeds (`--embed openai/gemini`), `learn-bindings` | provider API key |
| Postgres persistence & query (`export-*`, `blast-radius`, `search-chunks`) | `graphifyy[postgres]` + pgvector DB |

## See also
- [binding-rules.md](./binding-rules.md) — the pluggable rule engine
- [scheduler-detection-design.md](./scheduler-detection-design.md) — Tier A + B design
- [architecture.html](./architecture.html) — the whole system at a glance
- [epic-refinements.md](./epic-refinements.md) — what's next
