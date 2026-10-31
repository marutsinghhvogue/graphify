---
title: "GitNexus vs Graphify"
subtitle: "How the client-side editor-companion (GitNexus) compares to our polyglot impact-analysis + PRD→plan engine, and what's worth borrowing"
date: "August 2026"
---

# Summary

**GitNexus** ([abhigyanpatwari/GitNexus](https://github.com/abhigyanpatwari/GitNexus))
is a client-side code knowledge-graph that indexes a repo and exposes ~17 MCP
tools so coding agents (Claude Code, Cursor, Codex) can understand impact before
they edit. It runs 100% locally — Node native for the CLI, **browser WASM** for
the web UI — and stores the graph in **LadybugDB**, an embedded graph DB with
vector support.

We converged on the same thesis independently — *precompute the graph so the
agent doesn't grep* — but invested in different places. GitNexus optimized for
**frictionless local install + editor ergonomics + a browser demo**. Graphify
optimized for **polyglot depth, cross-service correctness, Postgres scale, and a
PRD → execution-plan pipeline**.

**One-liner:** GitNexus is *"a zero-config, client-side code-graph editor
companion."* Graphify is *"a confidence-tiered, polyglot code+docs knowledge
graph that computes cross-service blast radius and turns a PRD into an ordered
change plan."*

# Side-by-side

| | **Graphify (ours)** | **GitNexus** |
|---|---|---|
| Runtime | Python lib + CLI + MCP | Node native + browser WASM |
| Parser | tree-sitter (Python bindings) | tree-sitter (native + WASM) |
| Graph store | NetworkX in-memory + **Postgres (recursive CTE)** | **LadybugDB** (embedded graph DB + vectors, WAL) |
| Search | BM25 + pluggable embedder + RRF | BM25 + transformers.js embeddings + RRF |
| Languages | **~50 extractors** | 14 |
| Clustering | `cluster.py` communities | **Leiden** + cohesion scoring → **per-cluster skills** |
| Cross-service | **binding-rule engine** (HTTP/route→handler, schedulers, events, DI) | "group sync" + Contract Registry (HTTP shape-matching) |
| Taint / PDG | inter-procedural, summary-based, **polyglot** | opt-in PDG, **TS/JS only** |
| Embeddings | hashing / OpenAI / Gemini (API-key) | **transformers.js, local, no API key** |
| Flagship workflow | **PRD → seeds → blast radius → change plan (benchmarked)** | edit-safety + repo exploration |
| Extra ingestion | **docs / PDF / image / video** into the same graph | code-only |
| Distribution | PyPI `graphifyy`, YC S26 | zero-config MCP `setup`, editor hooks |

# Where we're ahead

- **Language breadth** — ~50 extractors (Apex, Blade, Delphi, Fortran, Verilog,
  Zig, Terraform, SQL, Elixir…) vs their 14. Real moat for polyglot/enterprise
  estates.
- **Cross-service edges are first-class and principled** — `contract_introspect.py`
  + `reconcile_contract`, contract-as-pivot, derive-service-from-controllers,
  schedulers/events/DI via the binding-rule engine. Their Contract Registry
  reads as HTTP shape-matching, not a pluggable rule engine.
- **Confidence policy** — our SCIP + tree-sitter **demote-not-delete** merge is
  more sophisticated than their flat per-edge confidence score.
- **PRD → plan is a different product** — seed discovery → blast radius →
  ordered change plan → **benchmarked** vs a grep agent (~92% fewer tool calls).
  They stop at "understand impact before you edit."
- **Postgres backend** — recursive-CTE blast radius scales cross-repo; their
  embedded/WASM DB has a ~5k-file browser cap and 50k-node embedding cap.
- **Non-code ingestion** — docs/PDF/image/video into the same graph.

# Where they're ahead (the borrowable ideas → backlog)

Each item below is a proposed work item. Status is **NOT STARTED** unless noted.

## 1. Local, no-API-key embedder  — priority: HIGH
**What they do:** semantic embeddings via transformers.js, CPU or WebGPU, with a
50k-node safety cap. No API key, no network.
**Our gap:** `semantic_index.py` ships a pluggable `EmbeddingProvider`, but real
vectors only come from OpenAI/Gemini (API-key) or the hashing fallback. This is
our #1 open item from the [CBM adoption plan](./cbm-adoption-plan.md).
**Proposed:** add a local `EmbeddingProvider` (e.g. a small sentence-transformer
via `sentence-transformers`/ONNX) behind `get_embedder`, with a node cap and CPU
default. Drops straight into `discover_seeds` and `export-chunks`.
**Why it matters:** removes the API-key dependency for Stage-2 seed discovery;
makes the hybrid search usable offline and by default.

## 2. Cluster → agent-consumable skills/summaries  — priority: HIGH
**What they do:** Leiden communities with cohesion scores, then **auto-generate a
skill per cluster** describing entry points and cross-area connections that the
agent reads.
**Our gap:** we cluster (`cluster.py`) but don't turn clusters into navigable
docs the agent consumes.
**Proposed:** a generator that, per community, emits "what lives here + entry
points + cross-service edges out" as a short markdown/skill. Feeds directly into
Stage-1 "which services must change" (`map_services`).
**Why it matters:** legibility — gives the planning LLM a map instead of a node
dump.

## 3. First-class "process" / execution-flow nodes  — priority: HIGH
**What they do:** trace entry-point → call-chain as a first-class node type and
**group query results by process**.
**Our gap:** `blast_radius` traverses edges and returns edge/node sets, not named
flows.
**Proposed:** materialize entry-point→sink chains as named "process" nodes so
`blast_radius` and `plan_change` return legible flows, not raw edge sets.
**Why it matters:** far more readable impact output for the planning LLM.

## 4. Zero-config `setup` + staleness hook  — priority: MEDIUM
**What they do:** `gitnexus setup` auto-detects Claude Code/Cursor/Codex and
writes the correct global MCP config; PostToolUse hooks detect a stale index
after commits and prompt a reindex.
**Our gap:** we have `watch.py` and skill files for 15+ assistants, but not the
auto-wire + staleness-nudge loop.
**Proposed:** a `graphify setup` that writes MCP config for detected assistants,
plus a PostToolUse-style staleness nudge on top of `watch.py`.
**Why it matters:** cheap adoption parity.

## 5. `rename` — graph-validated multi-file rename  — priority: MEDIUM
**What they do:** coordinated multi-file rename validated against both the graph
and text.
**Our gap:** we have `shortest_path` but no write-side refactor tool.
**Proposed:** a `rename` MCP tool that uses call/import edges to find all sites
and validates the rename set.
**Why it matters:** a concrete, high-value action tool; natural extension of the
edges we already have.

## 6. `detect_changes` — git diff → affected processes  — priority: MEDIUM
**What they do:** map a git diff directly to affected processes.
**Our gap:** we have `prs.py` / `get_pr_impact` (close), but not a tight
diff→process loop.
**Proposed:** a `detect_changes` tool that takes a diff and returns the affected
processes/flows (pairs well with #3).
**Why it matters:** tightest possible "is my change safe?" loop.

## 7. Cypher / graph-query escape hatch  — priority: LOW
**What they do:** a `cypher` tool for raw Neo4j-compatible queries over the full
schema.
**Our gap:** our MCP surface is fixed tools; no arbitrary structural query.
**Proposed:** a read-only graph-query tool (Cypher-ish or a constrained DSL) so
the LLM can ask structural questions our fixed tools don't cover.
**Why it matters:** power-user + agent flexibility without new bespoke tools.

# The honest head-to-head

- **MCP surface** is roughly at parity. We have `find_callers/callees`,
  `blast_radius`, `discover_seeds`, `map_services`, `plan_change`, `taint`,
  `blast_radius_pg`, PR triage. We're missing their `cypher`,
  `route_map`/`shape_check`, and `rename`; they're missing our `map_services`,
  `plan_change`, and taint-as-a-service.
- **Taint/PDG:** we're ahead — polyglot vs their TS/JS-only opt-in PDG.
- **Bet:** they bet on developer-adoption ergonomics; we bet on trustworthy
  impact analysis at enterprise/polyglot scale. Their ergonomics (local
  embeddings, cluster-skills, process nodes, zero-config) are borrowable and
  don't threaten our core differentiators.

# Recommended order

1. Local embedder (#1) — closes the top CBM gap, unblocks offline seeds.
2. Cluster→skills (#2) — strengthens Stage-1 service mapping.
3. Process nodes (#3) — makes blast_radius / plan_change legible.
4. Zero-config setup + staleness (#4) — adoption parity.
5. `rename` (#5), `detect_changes` (#6), Cypher (#7) — as capacity allows.
