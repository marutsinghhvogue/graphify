# Cross-service impact analysis — capability & limits

**Status:** WIRED. `graphify/contract_introspect.py` proves the linchpin
(consumer HTTP call → producer endpoint, across services and languages, without
host resolution). Now dispatched from the CLI: **`graphify extract --cross-service`**
treats each immediate subdirectory as a service, runs `cross_service_graph`, and
merges its `{nodes, edges}` into `graph.json` (edges tagged INFERRED/AMBIGUOUS,
`source='contract'`), so they persist via `graphify export-pg --source contract`.
Introduced as a spike in commit `0144602`; wired into the `extract` pipeline
afterward. Tests: `tests/test_contract_introspect.py` (inference) +
`tests/test_extract_cli.py::test_extract_cross_service_wires_calls_service_edges`
(CLI wiring); fixtures under `tests/fixtures/xservice`.

**Reconciled with the AST graph (`reconcile_contract`).** Contract handler and
consumer *function* nodes are joined onto the tree-sitter AST nodes for the same
functions on `(path-suffix, function-name)` — the contract analog of SCIP's
`reconcile_scip`. A handler that matches exactly one AST node is folded into it
(the AST node stays canonical and is stamped `metadata.service`; the `svc_*_fn_*`
node is dropped and its edges repointed), so `calls_service` edges connect **real
AST node → AST node across services** and blast radius traversing from a code
node crosses the boundary. Endpoint (`route`) nodes have no AST twin and are kept
as new; contract fn nodes matching 0 or >1 AST nodes are kept as new (never
name-guessed), preserving recall. Verified on `tests/fixtures/xservice`: 4/4
handlers reconciled; the NestJS `getOrder` consumer reaches the Python `get_user`
and Java `getInvoice` handlers as AST-to-AST edges.

**Remaining next step:** field/schema-level granularity (endpoint-level only
today) and broader consumer harvest — see the roadmap below.

## The two questions this targets

1. **Which APIs are used by which service?** — map each consumer's outbound
   HTTP calls to the producer endpoint that serves them.
2. **Which contract change will break which other services?** — given a change
   to an endpoint, find the services in its blast radius.

Both are *cross-process* questions. A pure code-reference index (SCIP/Glean)
resolves symbols **within** a language/repo but goes dark at the network
boundary: `httpx.get(f"{BASE}/orders/{id}")` has no symbol edge to the Spring
handler that serves it. This module exists to bridge that hop.

## How it works — the contract pivot

There is no service registry, gateway table, or host resolution. The join key is
the **HTTP contract itself**: `(method, normalized-path)`.

```
  service A (consumer)                      service B (producer)
  ─────────────────────                     ─────────────────────
  axios.get("/orders/42/items")             @Get(":id/items") on OrdersController
        │                                          │
        ▼  normalize                               ▼  normalize + compose prefix
  ("GET", "/orders/{}/items") ───── match ───► ("GET", "/orders/{}/items")
        │                                          │
        └──────── calls_service edge ─────────────┘   (INFERRED, cross-service)
```

- **Service identity is derived from where the controller lives** — each
  immediate subdirectory of the scan root is treated as one service
  (`cross_service_graph` iterates `root.iterdir()`). No gateway/registry assumed.
- A **global endpoint catalog** is keyed by `(method, normalized_path)`. Each
  consumer call is looked up in it; candidates in a *different* service become
  edges.
- **Path normalization** (`normalize_path`, the validated core) strips
  scheme+host, query, and fragment, and collapses every path-param shape to `{}`:
  `{id}` (FastAPI/Spring), `:id` (Nest/Express), `<id>` (Flask), `${id}` (JS
  template). So `http://user-service/users/${id}` and `/users/{user_id}` both
  become `/users/{}`.

## What it extracts

| Role | Frameworks / clients | How |
|------|----------------------|-----|
| **Producers** (endpoints) | FastAPI (`@app.get(...)`), NestJS (`@Controller` prefix + `@Get/@Post/...`), Spring (`@RequestMapping` class prefix + `@GetMapping/...`) | Decorator/annotation regex; route binds to the *next `def`/method* below it; controller/class prefixes composed |
| **Consumers** (calls) | `fetch(...)` and `axios.<m>(...)` (JS/TS); `requests`/`httpx`/`client`/`session`/`self.<x>`.`<m>(...)` (Python) | Regex over call sites; caller = nearest enclosing function; bare `fetch` assumed `GET` |

**Graph shape** (standard graphify `{nodes, edges}` plus a `stats` block):

- Nodes: `kind="route"` (an endpoint, label `GET /orders/{}`) and
  `kind="function"` (handler and caller functions).
- `handler --handles--> endpoint`, tagged `EXTRACTED` (1.0) — a local, certain fact.
- `caller --calls_service--> handler`, tagged:
  - `INFERRED` (0.9) when exactly one producer matches, or
  - `AMBIGUOUS` (0.5) when the same `(method, path)` exists in **multiple**
    services — the edge is **fanned out to all candidates, never dropped**
    (the demote-not-delete recall policy). Edge metadata carries `to_service`,
    `via_endpoint`, `path`, and the `raw_url`.
- Unmatched calls are counted as `external` in `stats` (e.g. a Stripe call) —
  reported, not silently discarded.

`stats = {endpoints, calls, matched_unique, matched_ambiguous, external}`.

## Verdict

| Question | Fit today | Notes |
|----------|-----------|-------|
| Q1 — service → API map | **Strong** | Cross-language, no host resolution needed. This is exactly what the module produces. |
| Q2 — contract-change blast radius | **Partial** | **Endpoint granularity only.** Gives the candidate set of services that touch an endpoint; does **not** know which ones read the specific field you changed. |

The honest positioning: graphify is well-placed to produce a **high-recall,
cross-language _candidate map_ of "who calls whom"** — a starting point for
impact review — **not** a precise, deploy-gating break oracle. That candidate map
is genuinely hard to get on a polyglot estate, which is the real advantage over a
symbol-reference index.

## Limits (why it is not yet an oracle)

- **Endpoint, not field.** No request/response **schema** is modelled — no
  fields, body, or types. A one-field contract change over-approximates to
  "everyone who touches this endpoint."
- **Recall holes = false "safe".** Consumer harvest covers only fetch/axios and
  requests/httpx. gRPC, GraphQL, message queues, generated clients, and URLs
  built from config/env are **not** seen. A missed call reads as "no impact",
  which is the dangerous direction for breakage analysis.
- **Known-deferred parsing gaps:** env-var/gateway host resolution; FastAPI
  `include_router(prefix=...)` composition; non-GET `fetch` options; method
  overloads.
- **Ambiguity is inherent.** Because hosts are deliberately not resolved, two
  services exposing `GET /users/{}` collapse to an `AMBIGUOUS` fan-out — a
  candidate set, not a single answer.
- **Spike maturity.** Static regexes targeting common shapes, fixture-tested
  only; not a hardened parser, and not exposed through the CLI.

## Usage (current)

No CLI flag yet — call the library directly (as the tests do). The scan root must
contain **one subdirectory per service**:

```python
from graphify.contract_introspect import cross_service_graph

g = cross_service_graph("/path/to/estate")   # each immediate subdir = a service
print(g["stats"])                             # matched_unique / matched_ambiguous / external
# g["nodes"], g["edges"] are standard graphify schema — mergeable into a graph.json
```

## Roadmap to authoritative

Roughly in priority order:

1. **Field/schema-level contract nodes** — parse OpenAPI/proto request/response
   schemas and link `call → field`, upgrading Q2 from endpoint to field
   granularity (the single biggest correctness lever).
2. **Host/gateway resolution** — resolve base URLs (env/config/gateway) to
   collapse `AMBIGUOUS` → `INFERRED`.
3. **Broader consumer harvest** — gRPC stubs, GraphQL operations, MQ topics,
   generated clients; close the false-"safe" recall holes.
4. **Edge-verification pass** — adversarially confirm inferred edges before they
   count toward a blast radius.
5. **Pair with contract-diff** — OpenAPI diff + consumer-driven contract tests
   (e.g. Pact) for the actual break prediction; graphify supplies the *who*, the
   diff supplies the *what*.

## Related

- `docs/POSTGRES_CODEGRAPH.md` — where these cross-service edges are persisted
  for blast-radius queries (Graphify is the stateless extractor).
- `docs/glean-vs-graphify.md` — why the contract pivot is the differentiator vs a
  symbol-reference index.
- `docs/doc-code-linkage-gap.md` — the sibling problem of tying doc-mentioned
  entities to real code symbols.
