---
title: "The Doc→Code Linkage Gap"
subtitle: "Why documentation and code sit in one graph but stay disconnected — and the semantic bridge that closes it"
date: "July 2026"
---

# TL;DR

Graphify already builds **one in-memory knowledge graph spanning code and
documentation** — docs become `document`/`concept` nodes in the same
`nx.DiGraph` as code. But those doc nodes and the code nodes they describe are
**not automatically linked**. Today the only bridge is a human-written alias
(`.graphify_aliases.json`). That is the weakest link in the PRD→execution-plan
pipeline: **Stage 2 — mapping PRD prose to code entry points** — currently has
no automatic path. The fix is a **semantic (vector + FTS) layer** that links
doc/PRD language to code by *meaning*, not exact name.

# Where docs already live in the graph

Documentation is a first-class citizen, not a separate store:

1. **Ingestion** — `graphify/ingest.py` fetches URLs / PDFs / arxiv / tweets /
   web pages and saves them as annotated markdown (`--google-workspace` pulls
   gdoc/gsheet/gslides).
2. **Extraction → nodes** — the LLM turns docs into graph nodes typed by
   `file_type`. Allowed types are `{code, document, paper, image, rationale,
   concept}`; a synonym map in `graphify/build.py` folds LLM labels in:
   `markdown → document`; `pattern / principle / constraint / tech /
   framework / gotcha / data-source → concept`.
3. **Unified graph** — those nodes land in the **same `nx.DiGraph`**
   (`build.py`) as code nodes and serialize to `graphify-out/graph.json`.

So the "in-memory knowledge graph from documentation" exists. The problem is
what connects it to code.

# The gap

From `graphify/aliases.py` (verbatim intent):

> A doc that calls something "User" and code that implements it as `Customer`
> produces **two disconnected nodes**, and neither dedup (label-similarity) nor
> symbol resolution (exact-name) will ever link them.

Concretely, the two linking mechanisms Graphify has both fail across the
doc/code boundary:

| Mechanism | What it links | Why it misses doc→code |
|---|---|---|
| **dedup** (`dedup.py`) | near-identical **labels** | doc "User" ≠ code "Customer" — different surface strings |
| **symbol_resolution** (`symbol_resolution.py`) | **exact** names / imports | resolves code↔code only; a prose noun is not a symbol |

The only working bridge today is **manual**: a human declares the equivalence in
`.graphify_aliases.json` and Graphify adds a `same_as` edge (EXTRACTED, 1.0) or
hard-`merge`s the nodes. This is what the `frontend/` **Alias Manager** and
**Review Queue** surface.

Manual aliases do not scale to the objective:

- A PRD is **new prose every time** — you cannot pre-write an alias for language
  you have not seen yet.
- Coverage is only as good as human diligence; a missed alias is a **silently
  missing edge**, which for blast radius is the dangerous failure mode
  (recall loss, not just noise).

# Why this blocks the objective

The north star is **PRD → execution plan** in four stages:

1. LLM extracts PRD intent.
2. **Map intent → code entry points.**  ← *this gap*
3. Blast radius across services/modules.
4. LLM writes the plan over the scoped subgraph.

Stage 2 is the hand-off from *human language* to *the code graph*. With only
exact-name resolution and hand-written aliases, that hand-off is either manual
or absent. Everything downstream (blast radius, the plan) is only as good as the
seed set Stage 2 produces — **garbage-in-garbage-out on the seeds caps the whole
pipeline.**

# The suggestion: a semantic bridge (vector + FTS)

Replace "match by exact name / hand-written alias" with "match by **meaning**."
This is the `code_chunks` layer already sketched in
[`POSTGRES_CODEGRAPH.md`](./POSTGRES_CODEGRAPH.md) (roadmap #2, issue #130) — this
doc is the *why* behind it.

**Shape:**

- Embed both sides into one vector space: **code chunks** (symbol +
  signature + docstring + surrounding lines) and **doc/concept nodes** (the
  prose Graphify already extracts). Store embeddings in `code_chunks` keyed to
  `symbols.symbol_id`, alongside a **full-text (FTS)** column for exact-token
  recall (identifiers, error strings, endpoint paths).
- **Stage-2 query** = hybrid retrieval: embed the PRD intent, take the
  pgvector nearest chunks **and** FTS matches, fuse the ranks → a ranked seed
  set of code symbols. (The SQL skeleton already lives in the Postgres doc's
  "discovery → structure" query.)
- Emit the resulting doc→code links as graph edges tagged
  **`INFERRED` + `confidence_score`** (cosine similarity). This composes with
  the existing tiering: precision consumers filter to EXTRACTED; recall
  consumers (blast radius) keep INFERRED. It is the **automatic, high-recall
  analog of a hand-written `same_as` alias.**
- **Keep humans in the loop, don't replace them:** surface the top INFERRED
  links in the existing Review Queue; confirming one **promotes it to a
  deterministic alias** (EXTRACTED). Vectors propose; humans ratify the ones
  worth locking in. Manual aliases remain the override, not the primary path.

**Why vector *and* FTS (not either alone):**

- Vectors catch **paraphrase** — "the checkout flow" → `PaymentController` — but
  can miss rare exact tokens.
- FTS catches **exact identifiers / paths / error codes** that embeddings blur.
- Hybrid fusion is the standard answer to "prose → code" and directly serves the
  recall-first posture blast radius needs.

# Where it fits the accepted architecture

Nothing here contradicts the direction of travel — it *is* that direction:

- Graphify stays a **stateless extractor**; embeddings + FTS live in **Postgres**
  (`code_chunks`), not in `graph.json`. No new datastore.
- The doc→code edges are ordinary `code_edges` rows
  (`edge_type='describes'`/`same_as`, `source='semantic'`, confidence columns).
- The in-memory NetworkX graph is still used, but only transiently for analytics
  (M1) — the semantic seeds are resolved in SQL first.

# Concrete next steps

1. **Populate `code_chunks`** — chunk + embed code symbols and doc/concept
   nodes; write embedding (pgvector) + FTS, keyed to `symbol_id`. (Roadmap #2.)
2. **Stage-2 retrieval tool** — an MCP tool / endpoint: PRD text in → ranked
   seed symbols out (hybrid vector + FTS), emitting INFERRED doc→code edges.
3. **Feed the Review Queue** — top INFERRED links become verification questions;
   confirmation writes back a deterministic alias (the human-ratify loop).
4. **Measure recall** — on a known PRD/code pair, compare seed recall of
   {exact-name only} vs {+ semantic}; this is the number that proves Stage 2 is
   no longer the bottleneck.

# One-line summary

> The knowledge graph already contains documentation; what it lacks is an
> *automatic* edge from doc/PRD language to the code it describes. A hybrid
> vector+FTS `code_chunks` layer supplies that edge as confidence-tiered,
> recall-safe INFERRED links — turning hand-written aliases from the only bridge
> into a human-ratification step on top of automatic discovery.
