---
title: "EPIC — Precision & Scale Refinements"
subtitle: "The remaining tail after the PRD→plan pipeline landed end-to-end"
date: "July 2026"
status: in-progress
---

> **Progress:** R1 ✅, R2 ✅, R3 ✅ built & pushed; R5 ◑ partial (broader Java
> consumer harvest + Feign near-exact done; field/schema-level nodes deferred).
> R4 (large, needs a live multi-repo pgvector DB to verify) and R6 (needs
> read-only cloud credentials) are held pending environment.

# Context

The core PRD→execution-plan pipeline is built end-to-end, with both an in-memory
and a Postgres path:

- **Stage 2 (prose → seeds)** — `semantic_index.py` (BM25 + hybrid embeddings),
  MCP `discover_seeds` / `discover_seeds_pg`, CLI `seeds` / `search-chunks`.
- **Stage 3 (blast radius)** — in-memory `blast_radius` and Postgres recursive-CTE
  `blast_radius_pg`, traversing every entry-point edge.
- **Entry points** — HTTP (`contract_introspect`), in-code + cloud schedulers,
  events, DI, via the pluggable [binding-rule engine](./binding-rules.md) and
  `cloud_schedule_introspect`.
- **Persistence** — `export-pg` (symbols/edges) + `export-chunks` (pgvector/FTS).

Everything below is a **refinement** — precision, recall, or scale on top of a
working system. None is a missing pipeline stage. Ordered roughly by
value-to-effort.

# Refinements

## R1 — Cross-resource cloud target resolution  · ✅ DONE
**Area:** schedulers (Tier B) · **Confidence impact:** INFERRED → higher recall
Tier-B cloud schedules currently emit `triggers` edges to a raw target hint;
EventBridge→Lambda and k8s-container targets are opaque. Resolve them via the
IaC reference graph so blast radius crosses infra→code.
- Follow the Terraform reference edges (`aws_cloudwatch_event_target.arn =
  aws_lambda_function.X.arn` → `X.handler` → the code symbol).
- k8s `CronJob` container `command`/`image` → service dir mapping where possible.
- **Acceptance:** on a fixture, a Terraform EventBridge rule's `triggers` edge
  resolves onto the Lambda handler's AST node; blast_radius(handler) surfaces the
  cloud schedule.

## R2 — DI: constructor & token injection, non-Java  · ✅ DONE
**Area:** binding engine (`inject` resolution) · **Effort:** small–medium
The `inject` resolution covers Java field/setter injection today. Extend the same
mode to constructor-parameter injection (Spring, NestJS `constructor(private x:
Foo)`) and token injection (`@Inject('TOKEN')`), plus Python/TS DI frameworks.
- **Acceptance:** a NestJS constructor-injected provider and a Spring
  constructor-injected bean each emit an `injects` edge resolved onto the
  provider's node.

## R3 — Per-caller demote before persist (recall-safety)  · ✅ DONE
**Area:** SCIP / reconciliation · **Policy:** demote-not-delete
Implement the designed retag: where SCIP resolved a caller, contradicted
tree-sitter `calls` edges for *that caller* are retagged AMBIGUOUS (low
confidence) rather than deleted — precision consumers filter to EXTRACTED, recall
consumers (blast radius) keep them. Hard delete only behind `--strict-scip`.
- **Acceptance:** a same-named-method collision (`User.save` vs `Logger.save`)
  keeps both edges post-merge, the SCIP-contradicted one tagged AMBIGUOUS.

## R4 — Cross-repo estate rollup over Postgres  · ⏸ HELD (needs live multi-repo pgvector DB)
**Area:** Postgres query layer · **Scale**
Today a `graph.json` (and its cross-service/reconcile step) is per-extraction.
Persist multiple repos into one Postgres and resolve cross-service / DI targets
*at the SQL layer* so blast radius spans the whole estate, not one repo.
- Global symbol identity across repos (dedupe by scip_symbol / path-suffix+name).
- `blast_radius_pg` walks edges across `repo` boundaries when a target resolves
  in another repo.
- **Acceptance:** two exported repos where a cross-service call in repo A reaches
  a handler in repo B via a single `blast_radius_pg` query.

## R5 — Contract cross-service: field/schema granularity + broader harvest  · ◑ PARTIAL (Feign/RestTemplate harvest done; field-level deferred)
**Area:** cross-service · **Precision**
`contract_introspect` matches at endpoint granularity. Add field/schema-level
nodes so "which contract change breaks whom" is answerable below the endpoint,
and broaden the consumer harvest (Feign, RestTemplate/WebClient, Go http, Ruby
Faraday) and producer coverage (Django REST, Express, gin, Rails).
- **Acceptance:** a request/response field node links producer↔consumer; a Feign
  `@FeignClient(name=...)` call resolves near-exact to the target service.

## R6 — Live cloud-API scheduler tier (ClickOps)  · ⏸ HELD (needs cloud read creds)
**Area:** schedulers (Tier B+) · **Optional, credentialed**
Schedules created in the console / via CLI exist only in the running account and
are invisible to static scans. A live-introspection tier — mirroring
`pg_introspect` reading a live DB — pulls ground truth
(`aws events list-rules` / `scheduler list-schedules`, `gcloud scheduler jobs
list`, `az functionapp ...`).
- Read-only cloud credentials; **query-time, not persisted** (snapshots go stale).
- **Acceptance:** with configured AWS read creds, a console-created EventBridge
  rule appears as a `schedule` node tagged `source=live`.

# Out of scope (explicitly)
- Real embeddings *quality* tuning / provider benchmarking — providers are
  pluggable; choosing one is a config decision, not code.
- A bespoke graph database — the extractor+Postgres architecture is settled.

# Tracking
Each refinement is independently shippable and composes with the existing
uniform pipeline (emit `{nodes, edges}` + a relation → reconcile → merge →
blast_radius). Convert to individual issues as picked up.
