# Cross-service impact: fork vs upstream graphify

**Question:** if I change this endpoint handler, which *other* services are affected?

```bash
python benchmarks/vs_upstream/run.py                      # all estates, tools, depths 2/4/6
python benchmarks/vs_upstream/run.py --estates piggymetrics --tools upstream,fork --rebuild
```

The first run shallow-fetches each estate at its pinned commit and creates a detached
worktree of `upstream/v8` under `.cache/` (gitignored). Later runs reuse cached graphs
unless you pass `--rebuild`.

## Method

- **Estates** (`estates.json`): public microservice repos pinned by SHA. Each immediate
  child of the estate root is a service.
- **Cases** (`cases/<estate>.json`): one endpoint handler per case (`file` + `symbol`)
  and the services whose source code calls it. Labels were made by reading the code,
  never from either tool's output. Every expected service cites its call site in
  `evidence`. Cases with `expected: []` are negative controls (endpoints only a
  browser UI calls) and can only add false positives.
- **Tools**, each run in its own interpreter against its own code:

  | tool | code | extraction |
  |---|---|---|
  | `upstream` | `upstream/v8` worktree | `extract --code-only` |
  | `fork-ast` | this repo | same command (ablation: our code, no framework layers) |
  | `fork` | this repo | plus `--cross-service --bindings --schedulers --cloud-schedulers` |

- **Answer**: each tool's own `affected_nodes` with its own default relation set,
  walked from every node in `file` whose label is `symbol`. Each hit's service is the
  first path component of its `source_file`.
- **Score**: service-level, ignoring the seed's own service. The runner reports
  recall, precision, cases answered exactly, false positives on negative controls,
  and seeds the tool's graph doesn't contain. HTTP and gRPC estates are reported
  separately.

## Adding your own estate

Add an entry to `estates.json` (url, sha, root, transport) and a
`cases/<name>.json`. Private repos work if `git fetch` can reach them. The best
cases come from real incidents or changes where you know which services broke or
had to change.

## Not scored

- Calls reaching a service only through a gateway route (YAML) or a browser UI.
- Message and event consumers. No estate here has them yet; add one with
  Kafka/RabbitMQ consumers to cover `consumes` edges.

## Results

### 2026-11-05: held-out Spring estates (detector frozen at `c13fe303`)

Six public Spring estates that nobody had looked at while building the
detector: in28minutes-v3, ewolff-microservice, bookstore, eazybytes-s20,
mall-swarm, kbastani-event-sourcing. Their labels were committed in `d15fb6cb`
before any tool ran on them. Consumers were found by searching for service
names, URL literals and URL config, not by the detector's patterns. Results are
identical at depths 2, 4 and 6.

| | upstream | fork-ast | fork |
|---|---|---|---|
| recall (26 consumer links) | 0/26 | 0/26 | **11/26 (42%)** |
| precision | n/a | n/a | 85% |
| exact cases (22 positive) | 0 | 0 | 8 |
| FP on 15 negative controls | 0 | 0 | 0 |

**This is the generalization number.** The dev estates (9/9) were tuned on.

The 15 missed links, by cause:

| cause | links | cases |
|---|---|---|
| same-named handlers in one service share an emitted node id (`svc_<service>_fn_<name>`), so edges land on another controller's method | 2 | ms-3, ms-5 |
| package-private handler methods (no `public`) resolve to `<unknown>` | 1 (and blocks the 4 below) | bs-1 |
| Spring 6 HTTP interfaces (`@GetExchange`/`@PostExchange`) through a prefix-stripping gateway | 4 | bs-1 to bs-4 |
| `String.format("http://svc/path/%s", …)` URLs | 3 | kb-4, kb-6, kb-7 |
| Spring Boot 1 `server.contextPath` (camelCase key) | 2 | kb-1 |
| Spring Data REST repositories as producers (plus format- or helper-built URLs) | 3 | ew-1, ew-2, kb-8 |

About the false positives:

- kb-2's extra service (shopping-cart) is real transitive impact: the cart's
  `checkout` calls order-service's `createOrder`, which calls the seeded
  account endpoint. Labels count only direct consumers, so it is scored as an
  FP. It was not relabelled after seeing results.
- kb-8's extra service (order-service) comes from an AST import link that
  upstream makes too: every kbastani service uses the Java package `demo`.

ms-4 is a lucky hit. Its Feign method's parameters span two lines, so the
caller resolves to `<unknown>`, but that stray node sits in the right file.

Fixing any of these makes these estates dev data. Measure each fix on a fresh
held-out set.

### 2026-11-05: after fixing the five Spring HTTP gaps

upstream `0a8cd4e1` (v0.9.97) vs fork `feat/bench-vs-upstream`. Results are identical at depths 2, 4 and 6.

| transport | upstream | fork-ast | fork | FP on negative controls |
|---|---|---|---|---|
| HTTP (13 cases, 9 consumer links) | 0/9 | 0/9 | **9/9**, precision 100%, 9/9 cases exact | 0 for every tool |
| gRPC (4 cases, 6 consumer links) | 0/6 | 0/6 | 0/6 | n/a |

What changed in `contract_introspect.py`:

- every Spring `@*Mapping` form, including `@RequestMapping(value=…, method=…)` and bare mappings
- type-level prefixes taken only from above the class declaration
- Feign interface methods read as calls
- `context-path` and `spring.application.name` read from the service's own config or config-server YAML
- concatenated URLs, and fluent `.post().uri(…)` verbs
- service-discovery hosts used as named targets
- test sources skipped, and `Map.put` no longer read as an HTTP call

`extract` also now writes introspector nodes with root-relative paths. Before
this, scheduler and event nodes landed under a bogus `/` service; this caused
the two false positives in the first post-fix run.

**Caveat: these cases are not held out.** The patterns were fixed while reading
these two estates, so 9/9 shows the fixes work, not how well they generalize.
The next honest measurement is a Spring estate nobody has looked at.

### 2026-11-04: baseline

upstream `0a8cd4e1` vs fork `392d6376`: HTTP upstream 0/9, fork-ast 0/9, fork 1/9 (precision 100%); gRPC 0/6 for all. Misses traced to:

- method-level `@RequestMapping(value=…, method=…)` (piggymetrics yielded no endpoints)
- Feign interface methods not read as calls
- `context-path` from config-server YAML
- bare `@GetMapping` under a class prefix
- concatenated `.uri(host + "/path")` URLs
- gRPC
