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

## Baseline (2026-11-04)

upstream `0a8cd4e1` (v0.9.97) vs fork `392d6376`. Results are identical at depths 2, 4 and 6.

| transport | upstream | fork-ast | fork | FP on negative controls (all tools) |
|---|---|---|---|---|
| HTTP (13 cases, 9 consumer links) | 0/9 | 0/9 | **1/9**, precision 100% | 0 |
| gRPC (4 cases, 6 consumer links) | 0/6 | 0/6 | 0/6 | n/a |

The fork's only hit is petclinic's gateway `getOwner` → customers `findOwner`. Every
miss traces to a pattern the contract layer doesn't parse yet:

- Spring `@RequestMapping(value=…, method=RequestMethod.X)` on methods (piggymetrics
  yields no endpoints at all)
- Feign interface methods as calls
- `server.servlet.context-path` prefixes from service or config-server YAML
- bare `@GetMapping`/`@PostMapping` under a class-level prefix
- concatenated URLs such as `.uri(host + "/owners")`
- gRPC (not modeled)

Precision risks seen while diagnosing: `src/test` files are scanned as callers, and
`Map.put("/date", …)` is read as an HTTP PUT.
