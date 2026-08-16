---
title: "PDG + Taint Analysis"
subtitle: "Statement-level data-flow and source→sink taint as a new edge layer"
date: "August 2026"
---

> Clean-room: PDG and taint are decades-old, public program-analysis techniques.
> This is implemented from the public technique — no third-party (e.g. GitNexus,
> which is noncommercial) code, structure, or text is used.

# Why

graphify's graph is symbol-level: "who calls / imports / injects X." It cannot
answer *statement-level* questions — "does *this value* flow to *that* operation."
That's what a **Program Dependence Graph (PDG)** gives, and it's the substrate for
**taint analysis**: does untrusted (or sensitive) data reach a dangerous sink
without sanitization. For a regulated/commercial estate this also answers "does
PII/PCI data reach an unsafe sink or cross a boundary," and it makes `blast_radius`
security-aware.

# Scope

- **v1 (built):** Python, **intra-procedural data dependence** (def→use within a
  function) + rule-driven **source→sink taint**.
- **v2 (built):** **inter-procedural taint** — summary-based over the call graph
  (compute per-function summaries: which params reach a sink / the return, and
  whether the function returns internal taint; propagate to a fixpoint). Handles
  tainted-arg→callee-sink, source-wrapper return values, transitivity through
  chains, sanitizing callees, and cross-file flows (callee resolved by name).
  Taint is a *set* of tags per variable, so a param use can't mask a real source.
- **v3 (built):** **graph + surface integration of `flows_to`** — `graphify
  extract --taint` runs the scan and merges each finding into `graph.json` as a
  `flows_to` edge (source stmt → sink stmt, INFERRED, whole finding in metadata)
  plus the finding's path `statement` nodes (only the path, not the whole PDG —
  keeps graph.json lean). The persisted findings are then served three ways off
  the *graph* (no re-analysis): the MCP `taint` tool (`serve._format_taint`), the
  REST endpoint `GET /api/v1/taint` (`webapi._taint_findings`), and the React
  SDK's `<GraphifyTaint>` component / `useTaint` hook.
- **Later:** control dependence (the PDG's other half), field/index sensitivity,
  more languages, precise cross-file callee resolution (imports vs. name-match),
  and adding `flows_to` to `blast_radius`'s default relation set (security-aware
  impact) once the precision/recall trade-off is characterized.

Honest limitation of v1: last-write def-use over statement order is exact for
straight-line code and approximate around branches/loops (a CFG-based
reaching-definitions pass is the productionization step). Findings are therefore
**INFERRED**, never EXTRACTED.

# The two layers

## 1. PDG — data dependence (`pdg.py`)
Parse a function with tree-sitter (already a dependency). Walk its statements in
order; for each, compute `defs` (assignment targets, params, `for`/`with` binders)
and `uses` (identifiers read). Track the last definition of each variable; for each
use, add a **`data_dep`** edge from the defining statement to the using statement.

Emits graphify `{nodes, edges}`: `statement` nodes (id, line, text) + `data_dep`
edges. So it composes with the existing graph and `blast_radius`.

## 2. Taint — source→sink over the PDG (`taint.py`)
A **rule catalog** (reusing the binding-rule "rules-as-data" pattern, and
LLM-authorable via `learn-bindings`) declares:
- **sources** — untrusted/sensitive inputs (`request.args.get`, `input()`,
  `os.environ`, request bodies), each with a `category` (e.g. `untrusted-input`,
  `pii`).
- **sinks** — dangerous operations (`cursor.execute`, `subprocess.*`, `eval`,
  `os.system`), each with a `vuln` (e.g. `sql_injection`, `command_injection`).
- **sanitizers** — calls that clean taint (`escape`, `int`, parameterized query),
  which stop propagation.

Algorithm: mark source statements tainted → propagate taint transitively along
`data_dep` edges, **stopping at sanitizers** → any **sink** statement that is
tainted is a **finding** (source→…→sink path). Findings are emitted as a `flows_to`
edge (source→sink) tagged INFERRED with the vuln category + the path.

# Rule shape

```jsonc
{ "role": "source", "lang": "python", "pattern": "request\\.args\\.get",
  "category": "untrusted-input", "description": "Flask request arg" }
{ "role": "sink",   "lang": "python", "pattern": "\\.execute\\(",
  "vuln": "sql_injection" }
{ "role": "sanitizer", "lang": "python", "pattern": "\\bint\\(" }
```

Loaded from built-ins + `.graphify_taint_rules.json` at the scan root; validated
(bad rows skipped), same discipline as `binding_rules`.

# Surface

- CLI **`graphify taint <path>`** — standalone scan, renders source→…→sink flows
  (also `--json`).
- CLI **`graphify extract --taint`** — merge findings into `graph.json` as
  `flows_to` edges + path `statement` nodes (the wiring that feeds the surfaces
  below off the persisted graph).
- MCP tool **`taint`** (`vuln` filter) — "does untrusted input reach a dangerous
  sink? show the flow," reading the graph's `flows_to` edges (file:line + vuln +
  confidence). No re-analysis at query time.
- REST **`GET /api/v1/taint?vuln=…`** — the same findings as structured JSON
  (`{count, by_vuln, findings[]}`) for the embeddable UI.
- React SDK **`<GraphifyTaint>`** / **`useTaint`** — severity-grouped findings
  with the source→…→sink flow, over the REST endpoint.
- Later: MCP `pdg_query` (statement-level "what does this value depend on /
  affect"); adding `flows_to` to `blast_radius` so impact is security-aware.

# Example finding

```
sql_injection (INFERRED, 0.8)
  source: uid = request.args.get("id")     views.py:41   [untrusted-input]
    → q = f"SELECT ... WHERE id={uid}"      db.py:88
  sink:   cursor.execute(q)                 db.py:90      [sql_injection]
  no sanitizer on the path
```

# Reuse (don't rebuild)
- `binding_rules` rules-as-data + validation + `learn-bindings` → the taint catalog.
- tree-sitter Python (already used by `extract.py`).
- confidence tiering, `{nodes, edges}`, MCP-tool + module-level-formatter pattern,
  the v1 REST API + React SDK for the UI.
