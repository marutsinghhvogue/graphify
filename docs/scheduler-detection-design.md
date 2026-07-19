---
title: "Scheduler / Job Detection"
subtitle: "Finding timed & event-driven entry points across app frameworks and clouds (AWS / GCP / Azure / k8s)"
date: "July 2026"
---

# TL;DR

Graphify has **no first-class notion of a scheduler**. A cron'd task shows up
only as an ordinary function node — the *fact that something fires it on a
timer* is invisible, so blast radius misses it. This doc designs a
`schedule_introspect.py` module (sibling to `contract_introspect.py` /
`pg_introspect.py`) that discovers **schedule triggers** and links each to the
**handler it invokes**, tagged with confidence.

The hard part is not the app-framework decorators — it is that the trigger
frequently lives **outside application code**: in IaC (Terraform / CloudFormation
/ serverless.yml), in a Kubernetes manifest, or only in the cloud console. The
*binding from trigger to handler crosses the code↔config boundary*, and it looks
different on every cloud. That boundary crossing is exactly the valuable edge —
and the same pivot pattern that powers cross-service edges applies here.

# Why this matters for the objective

Blast radius must answer "what are the **entry points** into the code I'm about
to change?" HTTP endpoints are one class of entry point (`contract_introspect`).
**Schedules are the other** — the outside world also reaches the code graph on a
timer. If a PRD touches `rollup_daily()` and an EventBridge rule invokes it at
02:00, the plan must know that. `triggers` (schedule→handler) is the **sibling of
`handles`/`calls_service`** — together they are the graph's entry-point edges.

# The core insight: a schedule is a consumer with no caller

`contract_introspect` matches a *consumer HTTP call* to a *producer endpoint* by
`(method, normalized-path)`. A scheduler is the same shape with the caller
replaced by a clock:

```
HTTP consumer   —calls_service→   handler        (contract_introspect)
schedule trigger —triggers→        handler        (schedule_introspect)
```

So an **HTTP-target scheduler resolves through the exact same endpoint catalog** —
a GCP Cloud Scheduler job with `http_target.uri = ".../internal/rollup"` is just
another consumer of `POST /internal/rollup`. `schedule_introspect` reuses
`normalize_path()` and the endpoint catalog; the only new work is the
non-HTTP targets (direct Lambda ARN, PubSub topic, container command).

# Detection surfaces

## Tier A — In application code (deterministic, tree-sitter, already collected)

The trigger *decorates/annotates the handler in source*, so handler binding is
**EXTRACTED** (like `handles`). All these languages are already parsed today.

| Stack | Signal |
|---|---|
| Python — Celery | `beat_schedule = {...}`, `@periodic_task`, `crontab(...)` |
| Python — APScheduler | `@scheduler.scheduled_job(...)`, `add_job(...)` |
| Python — Airflow | DAG `schedule_interval=` (a `.py` file → Tier A) |
| Spring (Java) | `@Scheduled(cron=… / fixedRate=…)`, Quartz `JobDetail` |
| NestJS (TS) | `@Cron() / @Interval() / @Timeout()` (`@nestjs/schedule`) |
| Node | `node-cron`, BullMQ `repeat:{cron}`, Agenda |
| Ruby | `sidekiq-cron`, `whenever` (`schedule.rb`), Rufus |
| Go | `robfig/cron` `AddFunc(spec, fn)` |
| .NET | Hangfire `RecurringJob.AddOrUpdate`, Quartz.NET |

## Tier B — Cloud / IaC (the multi-cloud part — trigger is *not* in app code)

Handler binding here is **INFERRED** (resolve target → handler) or **AMBIGUOUS**
(unresolved / opaque), never EXTRACTED.

**AWS**
- EventBridge / CloudWatch: `aws_cloudwatch_event_rule.schedule_expression =
  "cron(…)"|"rate(…)"` + `aws_cloudwatch_event_target { arn }` → Lambda/ECS/StepFn.
- EventBridge Scheduler: `aws_scheduler_schedule.schedule_expression` + `target{arn}`.
- ECS scheduled task (run-task target).
- SAM / CloudFormation: `AWS::Events::Rule` `ScheduleExpression`; SAM `Schedule`
  event on a `Function` (YAML/JSON).
- `serverless.yml`: `functions.X.events[].schedule`.

**GCP**
- `google_cloud_scheduler_job` — `schedule` (cron) + one of
  `http_target{uri}` / `pubsub_target{topicName}` / `app_engine_http_target`.
- Cloud Composer / Airflow (DAG `.py` → Tier A).

**Azure**
- Functions **Timer trigger**: `function.json` `bindings[].type=="timerTrigger"`,
  `schedule` = NCRONTAB (JSON — already collected); or in-code
  `[TimerTrigger("…")]` (C#) / `@TimerTrigger` (Java) / v2 Python `app.timer()`.
- Logic Apps recurrence: `azurerm_logic_app_trigger_recurrence` (TF) or ARM/Bicep.

**Kubernetes**
- `CronJob` — `spec.schedule` + `jobTemplate…containers[].{image,command,args}` (YAML).

**OS-level**
- `crontab` files, `systemd` `.timer` units.

# Handler resolution — the pivot, tiered by confidence

The trigger→handler link is resolved by a strategy chosen per target type, and
tagged per the **demote-not-delete** recall policy (unresolved is *kept*, not
dropped):

| Target type | Resolution | Confidence |
|---|---|---|
| In-code decorator (Tier A) | decorator annotates the function directly | **EXTRACTED** |
| **HTTP target** (Cloud Scheduler URI, API target) | **reuse `contract_introspect` `(method, path)` catalog** | INFERRED / AMBIGUOUS on path collision |
| Lambda/function ARN | target refs `aws_lambda_function` → its `handler` attr (`app.handler`) → code symbol | INFERRED |
| PubSub / SQS / queue topic | trigger → topic node; subscriber found via consumer harvest | INFERRED, often AMBIGUOUS |
| k8s CronJob container | `command`/`image` → service dir if mappable, else opaque | AMBIGUOUS (kept, flagged) |
| Unresolved | edge to a **placeholder target node** (raw ARN/URI/topic), never dropped | AMBIGUOUS |

For Tier B on **Terraform specifically**, the reference edges already exist:
`extract_terraform` emits `aws_cloudwatch_event_target →references→
aws_lambda_function`. `schedule_introspect` adds a **semantic tagging pass** over
those HCL nodes (recognize the scheduler resource types + read
`schedule_expression`), rather than re-parsing.

# Graph model (mirrors contract_introspect)

```jsonc
// schedule node
{ "id": "sched_aws_rollup_daily", "label": "cron(0 2 * * ? *)",
  "file_type": "code", "kind": "schedule",
  "metadata": { "provider": "aws", "expr": "cron(0 2 * * ? *)",
                "timezone": "UTC", "trigger": "eventbridge" } }

// triggers edge  (schedule → handler)
{ "source": "sched_aws_rollup_daily", "target": "svc_billing_fn_rollup_daily",
  "relation": "triggers", "confidence": "INFERRED", "confidence_score": 0.85,
  "context": "schedule",
  "metadata": { "provider": "aws", "via": "lambda_arn", "resolved": true } }
```

New node `kind: "schedule"`; new edge `relation: "triggers"`. Emits the standard
`{nodes, edges, stats}`; `stats` counts schedules found / resolved / unresolved
**per provider** (no silent caps).

# Known blind spots (state them, don't hide them)

- **ClickOps** — a rule created in the console or via `aws events put-rule` at
  runtime has no static artifact. *Invisible to static analysis.* Mitigation:
  an optional **Tier-4 live introspection** sibling to `pg_introspect` —
  connect to the cloud and pull ground truth (`aws events list-rules /
  scheduler list-schedules`, `gcloud scheduler jobs list`,
  `az functionapp function show`). Same idiom as pg_introspect reading a live DB.
- **Dynamic cron** built from env/vars → expression captured as raw text, not evaluated.
- **YAML gap** — YAML is currently a *DOC* extension (LLM path), so k8s CronJob,
  `serverless.yml`, and SAM/CloudFormation YAML are **not deterministically
  parsed today**. Needs a small targeted structural reader for the handful of
  known schedule schemas (keyed off `kind: CronJob`, `events: - schedule`,
  SAM `Schedule`) — *not* full YAML-as-code. JSON (`function.json`,
  CloudFormation JSON) is already collected but not schedule-aware.

# Phasing (spike → productionize, mirroring contract_introspect)

0. **Spike** — `schedule_introspect.py`, static only, one fixture per surface:
   Spring `@Scheduled`, NestJS `@Cron`, Celery beat, AWS EventBridge+Lambda (TF),
   GCP Cloud Scheduler `http_target` (proves catalog reuse), k8s CronJob (YAML),
   Azure `function.json` timer. Prove the trigger→handler pivot across the
   code↔config boundary.
1. **Confidence tiering + demote-not-delete** for unresolved targets; per-provider stats.
2. **Wire through `pg_export`** — `edge_type='triggers'`, `source='schedule'`
   (exactly the cross-service wiring path).
3. **Optional Tier-4** — live cloud-API introspection for ClickOps ground truth.

# One-line summary

> Schedules are entry points the compiler can't see; `schedule_introspect` finds
> them across app frameworks and three clouds, and resolves each to its handler
> through the same contract pivot — reusing the endpoint catalog for HTTP
> targets — so blast radius includes "what runs this on a timer."
