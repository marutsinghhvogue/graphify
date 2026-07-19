---
title: "Code Intelligence Architecture: Graphify + Postgres vs Sourcegraph"
subtitle: "How our extractor-plus-Postgres approach compares to Sourcegraph, the code-search + precise-navigation platform"
date: "July 2026"
---

# Summary

**Sourcegraph** is a code-search and precise-navigation **platform** — you run
it (or buy it hosted), and engineers, IDEs, and its AI assistant (Cody) query it
for "where is this symbol, who references it, search across all repos."
Comparing our architecture (Graphify as a stateless extractor, edges and vectors
in Postgres) against it clarifies what we share and where we diverge.

Sourcegraph sits in the **same SCIP/LSP precision lineage** as Meta's Glean —
in fact Sourcegraph *created SCIP* (the successor to LSIF). So the comparison
rhymes with [the Glean one](./glean-vs-graphify.md), but with one key
difference: Sourcegraph is a polished, multi-repo **product for humans and an AI
chat**, not internal monorepo infra.

**One-liner:** Sourcegraph is *"precise code search + navigation as a platform,
with an AI assistant on top."* Ours is *"a confidence-tiered code+docs knowledge
graph as an MCP substrate for an agent to compute blast radius — on Postgres —
because our job is impact analysis for planning, not interactive search."*

# What Sourcegraph actually is

Sourcegraph is a **code intelligence platform** with a few pillars:

- **Universal code search** — trigram-indexed (Zoekt) search across many repos:
  regex, structural, and symbol search, fast at massive scale.
- **Precise navigation** — go-to-definition / find-references / hover powered by
  **SCIP** indexers that lean on the real compiler/type-checker; a search-based
  (ctags-like) tier is the heuristic fallback where a SCIP index isn't present.
- **Cross-repo** navigation and a code graph over an org's repositories.
- **Batch Changes** — org-wide automated refactors across many repos.
- **Code Insights** — dashboards/trends over the codebase.
- **Cody** — an AI coding assistant doing RAG (embeddings + context fetching)
  over the indexed code.

It is a **heavy, deployed platform** (search backend, indexers, Postgres, UI,
auth/permissions), positioned for enterprise scale.

# The philosophical difference that drives everything

|                     | Sourcegraph                       | Our architecture                              |
|---------------------|-----------------------------------|-----------------------------------------------|
| **What it stores**  | Precise facts (SCIP) + a search index | Everything, confidence-tiered (EXTRACTED / INFERRED / AMBIGUOUS) |
| **Who queries it**  | Humans, IDEs, and Cody (a chat product) | An **autonomous agent** via MCP tools         |

Sourcegraph's precise layer, like all SCIP/LSP tooling, only asserts what the
compiler resolves. It has a search-based fallback (so, unlike Glean, it *does*
have a heuristic tier) — but it does **not infer** and does **not tag edges with
confidence**. For anything the compiler cannot resolve (dynamic dispatch,
reflection, **cross-service HTTP calls**), Sourcegraph offers *search results*,
not a *typed, confidence-scored edge in a graph*.

Our stack fills those gaps with tree-sitter (heuristic) and an LLM (inferred) as
**first-class, confidence-tagged edges**, because for **blast radius** a probable
edge beats a missing one (the "demote-not-delete" principle).

# Full comparison

| Axis                       | Sourcegraph (search + SCIP platform)          | Ours (Graphify + Postgres)                                  |
|----------------------------|-----------------------------------------------|-------------------------------------------------------------|
| **What it is**             | Hosted/self-hosted platform (search, UI, IDE, Cody, auth) | Library + CLI + MCP server — a building block   |
| **Primary job**            | Code **search** + precise **navigation** at scale | Build a **queryable graph** for **impact analysis / planning** |
| **Precision source**       | SCIP (compiler-grade) + search-based fallback | SCIP (where run) + tree-sitter (syntactic) + LLM (inferred) |
| **Confidence**             | Precise vs. search-based; **no inference tier** | First-class EXTRACTED / INFERRED / AMBIGUOUS + score        |
| **Cross-service (HTTP)**   | Not a first-class edge — SCIP stops at the boundary | First-class (contract-pivot inference)                  |
| **Docs in the graph**      | Code-centric; Cody RAGs over code (+ some docs) | Docs are first-class concept/document nodes             |
| **Schedulers / entry pts** | Symbols only; a scheduled handler is just a function | Planned `triggers` edges — schedules as entry points   |
| **Query surface**          | Web UI, IDE extensions, Cody chat, GraphQL API | MCP tools an external agent calls (find_callers, blast radius) |
| **AI integration**         | **Cody** — an end-user AI assistant product   | Graphify is the **tool layer an agent** (Claude Code / a planner) calls |
| **Optimizes for**          | Precise nav + fast search + human/IDE UX      | **Recall** for blast radius (probable edge beats missing)   |
| **Storage**                | Bespoke search index (Zoekt) + Postgres       | Postgres (pgvector + FTS + relational edges) — commodity    |
| **Ops cost**               | High: platform, indexers, enterprise infra    | Low-moderate: Postgres + tree-sitter; SCIP/LLM opt-in       |
| **Built for**              | Search & navigate large multi-repo estates    | PRD → plan blast radius over polyglot microservices         |

# Where Sourcegraph is genuinely better

- **Search at massive scale.** Trigram-indexed (Zoekt) search across millions of
  files is fast, mature, and battle-tested — we do not compete there.
- **Precise cross-repo navigation.** A large, maintained set of SCIP language
  indexers; go-to-def / find-references that just work at enterprise scale.
- **A polished product.** Web UI, IDE integrations, SSO/permissions, Batch
  Changes (org-wide automated refactors), Code Insights, and Cody as a full AI
  assistant. It is a thing you **buy and run today**.

# Where ours fits the target problem and Sourcegraph does not

Because Sourcegraph shares the SCIP/LSP precision lineage, its structural gaps
for our estate mirror Glean's:

1. **Cross-service HTTP edges.** A TypeScript service calling a Python service
   has no compiler link — SCIP (Sourcegraph's precision engine) does not produce
   that edge; at best you *search* for the URL string. Our contract-pivot layer
   emits it as a real, confidence-scored edge.
2. **Confidence-tiered recall.** Sourcegraph gives you *precise* facts or
   *search hits* — not "this edge probably exists, tagged AMBIGUOUS, keep it for
   blast radius." That tiering is our recall backbone.
3. **Agent-first, not human-first.** Sourcegraph's surface is a UI plus Cody
   chat. Ours is **MCP tools an autonomous planner calls** — the deterministic
   substrate under a PRD→plan loop, not an interactive product a person drives.
4. **Doc↔code + schedulers as graph structure.** Unifying documentation,
   cross-service calls, and (soon) scheduled entry points into one
   confidence-tiered graph is the point; Sourcegraph keeps to code symbols.

Additionally, Sourcegraph is a heavy platform to operate; we get the graph we
need on **commodity Postgres** without running a search cluster and a fleet of
indexers.

# Synthesis

**We share Sourcegraph's precise core** — SCIP. Sourcegraph *authored* SCIP, and
our `reconcile_scip` layer consumes those same exact facts. Where we diverge is
deliberate, and it is about **job**, not just infra:

> Sourcegraph maximizes code search and precise navigation for humans, IDEs, and
> an AI assistant, as a deployed platform. We trade the search platform and the
> polished UI for an agent-queryable, confidence-tiered graph that spans
> cross-service, doc↔code, and scheduled entry points — on commodity Postgres —
> because our target is not "let a person search and navigate the code," it is
> "given a PRD, let an agent find every affected service, including the fuzzy
> edges the compiler cannot see, and plan from them."

**Worth borrowing from Sourcegraph:** SCIP itself (already adopted), and the
discipline of precise indexes as the precision floor. Its search backend is a
genuinely strong capability we deliberately do not replicate.

**Not worth borrowing:** the platform weight — a search cluster, per-language
indexer fleet, and UI — for a use case whose consumer is an agent, not a person.
For an agent-driven, polyglot service graph with a vector layer, Postgres
recursive-CTE + pgvector is the right cost/power point.

# Running both: which is used when, and how

Sourcegraph and Graphify are not either/or — they **layer**, and they meet at
SCIP. The mental model:

- **Sourcegraph = the precision retrieval / navigation engine.** Point queries
  answered exactly and at scale: "definition of X," "all references to X," "grep
  this string across every repo," symbol search.
- **Graphify = the impact / reasoning graph.** Aggregate queries answered with
  recall: "everything affected by changing X — across services, through docs,
  including schedulers — tiered by confidence," served as MCP tools an agent
  drives.

Sourcegraph answers *where is this / who references this*; Graphify answers *what
is the blast radius of changing this*. The first is a lookup; the second is a
traversal over a graph that includes edges the compiler cannot see.

## Shared substrate: index SCIP once, consume it twice

This is the integration seam. Sourcegraph *authored* SCIP and produces SCIP
indexes; Graphify's `reconcile_scip` *consumes* SCIP. So precision is not indexed
twice:

```
SCIP indexers  ──►  Sourcegraph  (search + precise nav for humans / IDEs)
      └──────────►  graphify.reconcile_scip  (precise backbone of the impact graph)
                        + tree-sitter breadth
                        + contract-pivot (cross-service)
                        + doc↔code + schedulers
                        + confidence tiers  ──►  Postgres  ──►  agent (blast radius / plan)
```

Sourcegraph's precise within-language xrefs become the **high-confidence
backbone** of Graphify's graph; Graphify stitches the fuzzy edges on top.

## Routing table — which engine for which query

| Query | Engine | Why |
|---|---|---|
| Grep an identifier / error string / path across all repos | **Sourcegraph** | Zoekt trigram search — its core strength |
| Definition of `X` / all references to `X` (within a language) | **Sourcegraph** | Precise SCIP xrefs, maintained, authoritative |
| Find code matching a PRD phrase (by meaning) | **Graphify** | Vector / semantic over code_chunks + doc nodes |
| Who calls this — *including* across services / over HTTP | **Graphify** | Cross-service edges Sourcegraph doesn't emit |
| What runs this on a schedule / what's the entry point | **Graphify** | `triggers` / `handles` edges — not in SCIP |
| Blast radius of changing `X` (bounded, multi-service) | **Graphify** | Recursive-CTE over confidence-tiered edges |
| Does a doc concept map to this code? | **Graphify** | Doc↔code concept nodes + aliases |
| Interactive human browse / navigate | **Sourcegraph** | UI, IDE integration, permissions |

Rule of thumb: **exact + within-language + human-facing → Sourcegraph; fuzzy +
cross-service + aggregate + agent-facing → Graphify.**

## Mapped onto the PRD → plan pipeline

- **Stage 2 (PRD prose → seeds):** hybrid. Graphify's **vector** arm handles
  paraphrase ("the checkout flow" → `PaymentController`); Sourcegraph's
  **search** arm handles exact tokens (identifiers, endpoint paths, error
  codes). Let Sourcegraph *be* the keyword/symbol-retrieval arm and Graphify the
  semantic arm — no need to rebuild Zoekt in Postgres.
- **Stage 2→3 (seed → precise callers/callees):** prefer **Sourcegraph's**
  precise xrefs as authoritative within-language, feed them in.
- **Stage 3 (blast radius across services + fuzzy edges):** **Graphify** owns
  this end to end — it is where Sourcegraph structurally stops.
- **Stage 4 (write the plan):** the **agent** queries Graphify's MCP tools (the
  graph is now complete) and can cite Sourcegraph URLs so a human can click
  through to the exact lines.

## Consumer split

- **Human developer / IDE / interactive** → Sourcegraph (and Cody for chat).
- **Autonomous PRD → plan agent computing impact** → Graphify (MCP).

They coexist without stepping on each other because they serve different
consumers.

## Two cautions

1. **Don't double-do the overlap.** Both can find callers and both can search
   text. Make Sourcegraph authoritative for *exact within-language* xrefs and
   search; let Graphify's `find_callers` fill the *gaps* (dynamic, cross-service)
   rather than re-deriving what SCIP already proved — otherwise you get two
   answers to reconcile.
2. **You may not need both.** If the goal is *only* the agentic planning loop,
   Graphify + raw SCIP indexers (without the Sourcegraph platform) can cover it —
   you lose human search/nav and the maintained indexer fleet. Running both is
   justified when you *also* want first-class human code search/navigation:
   Sourcegraph earns its keep on the human side, Graphify on the agent/impact
   side.
