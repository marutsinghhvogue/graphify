---
title: "Binding Rules: pluggable framework-construct detection"
subtitle: "One declarative engine for schedulers, events, and any construct that binds a handler — extended by data, authored by an LLM, executed deterministically"
date: "July 2026"
---

# Why

Cross-service, scheduler, and event detection are the **same shape**: *a
decorator/annotation binds the function below it to a construct (a schedule, a
topic, an endpoint), and we want a typed edge to that handler.* Hand-writing a
bespoke module with hardcoded regexes per concern doesn't scale — every new
framework meant editing code.

The binding-rule engine makes a construct a **data row** (`BindingRule`) and runs
every rule through one deterministic engine. **Adding a framework is a rule, not
code** — and the rule can be authored by an LLM.

# The three layers

```
LLM (author-time)                Registry (data)            Engine (run-time)
  learn-bindings         →   BUILTIN_RULES +          →   run_bindings():
  proposes a rule            .graphify_binding_             regex → next def →
  (validated, ratified)      rules.json (external)          {nodes, edges}
                                                            reconciled onto AST
```

- **Deterministic execution.** `run_bindings` is pure regex over source lines —
  same input → same edges, every run. No LLM at run time. Edges are **EXTRACTED**
  (the binding is syntactic).
- **Pluggable by data.** Built-in rules plus any rules in
  `.graphify_binding_rules.json` at the scan root. A new framework needs no code
  change; invalid rows are skipped (never fatal), and a built-in id can't be
  shadowed.
- **LLM authors, human ratifies.** `graphify learn-bindings` grounds the LLM in
  real uncovered annotations, has it propose rules, validates every one, and (on
  `--write`) persists them for deterministic execution thereafter.

# The rule

```jsonc
{
  "id":        "event.kafka.listener",   // unique dotted id
  "category":  "event",                  // "scheduler" | "event"
  "provider":  "kafka",                  // framework, lower-case
  "languages": ["java"],                 // subset of python | java | ts
  "pattern":   "^\\s*@KafkaListener\\b", // regex, line-anchored (comment-safe)
  "relation":  "consumes",               // edge type (snake_case)
  "node_kind": "topic",                  // synthetic source node kind
  "resolution":"next_def",               // how the handler is found
  "confidence":"EXTRACTED"
}
```

Output per match: a synthetic construct node (`node_kind`) + an edge (`relation`)
to the handler. Handler nodes reuse the `svc_*_fn_*` id scheme, so
`reconcile_contract` folds them onto the tree-sitter AST — and once `relation` is
in `affected.DEFAULT_AFFECTED_RELATIONS`, `blast_radius` traverses it. `triggers`
(schedulers) and `consumes` (events) are already registered.

# Built-in rules

| Category | Provider | Construct | Edge |
|---|---|---|---|
| scheduler | Spring | `@Scheduled` | `triggers` |
| scheduler | NestJS | `@Cron` / `@Interval` / `@Timeout` | `triggers` |
| scheduler | APScheduler | `@scheduled_job` | `triggers` |
| scheduler | Celery | `@periodic_task` | `triggers` |
| event | Spring | `@EventListener` | `consumes` |
| event | Kafka | `@KafkaListener` | `consumes` |
| event | NestJS | `@EventPattern` / `@MessagePattern` | `consumes` |

# Extending

**By hand** — drop a rule into `.graphify_binding_rules.json`:

```json
{ "rules": [
  { "id": "event.sqs.listener", "category": "event", "provider": "sqs",
    "languages": ["java"], "pattern": "^\\s*@SqsListener\\b",
    "relation": "consumes", "node_kind": "topic" }
]}
```

**By LLM** — `graphify learn-bindings [path]` finds annotations no rule covers,
asks the model to author rules for the scheduler/event ones, validates them, and
prints proposals; `--write` persists them. Then `graphify extract --bindings`
executes them deterministically.

# Deliberately out of scope

- **Dependency injection.** Its target is a *type*, not the next def — it needs a
  distinct `inject` resolution and, to be precise, SCIP type resolution (name
  matching alone is low-confidence). The `resolution` field reserves it as the
  next rule family rather than shipping a low-precision regex version.
- **Tier B (cloud/IaC) schedulers** — EventBridge / Cloud Scheduler / k8s
  CronJob — live outside application code; see
  [scheduler-detection-design.md](./scheduler-detection-design.md).

# The principle

> Deterministic where there's a syntactic anchor (EXTRACTED, reproducible, free);
> the LLM to *author the rules* for the anchors it recognizes — not to be the
> run-time extractor. Framework knowledge with reproducible blast radius.
