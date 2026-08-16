# CBM adoption — reimplementation plan (PLAN ONLY, no code yet)

Reference project: **`DeusData/codebase-memory-mcp` (CBM)** — MIT-licensed, C/C++ single-binary
code-intelligence MCP server (tree-sitter + homegrown "Hybrid LSP" + local embeddings +
Cypher + gRPC/GraphQL/pub-sub/IaC edges).

**Decision:** reimplement the high-value ideas **natively in Python**; do **not** vendor CBM's C
engine or take it as a runtime dependency. MIT lets us read their source as a reference spec
while writing our own. Rationale: runtime mismatch (C binary vs Python), and CBM-as-backend
would fork graphify's Postgres-one-store + LLM-over-graph architecture for the *commoditized*
half of the problem.

---

## 0. Corrected current state (grounding — the design memory was stale)

Verified against the actual code on branch `feat/find-callers-callees-mcp`:

| Capability | Status | Where |
|---|---|---|
| `code_chunks` semantic layer (BM25 + pluggable embedder + RRF fusion) | **BUILT** | `graphify/semantic_index.py` |
| Persist chunks to Postgres (pgvector hnsw + generated FTS tsvector) | **BUILT** | `graphify/pg_chunks.py` |
| Recursive-CTE blast radius (depth-bounded, cycle-guarded) | **BUILT** | `graphify/pg_query.py::blast_radius_pg` |
| Hybrid seed discovery in SQL (vector ⊕ FTS) | **BUILT** | `graphify/pg_query.py::discover_seeds_pg` |
| Cross-service edges already flow into blast radius | **BUILT** | `DEFAULT_IMPACT_RELATIONS` includes `calls_service, triggers, consumes, injects` |
| CLI `export-chunks` / `search-chunks` / `export-pg` | **BUILT** | `graphify/__main__.py` |
| Local, no-API-key **real** embedder | **MISSING** | only `hashing` (weak), `openai`, `gemini` |
| Non-HTTP cross-service (gRPC/GraphQL/pub-sub/IaC) | **MISSING** | `contract_introspect.py` is HTTP-only spike |
| Cross-service wired to a CLI flag / `extract` pipeline | **MISSING** | `cross_service_graph` is library-only |

**Implication:** the keystone we thought was "build the vector layer" is really "add one embedder
class." The genuine net-new work is the non-HTTP cross-service extractors.

---

## Item 1 — Local embedding provider  ·  effort: SMALL  ·  priority: 1 (do first)

**Goal.** Offline paraphrase recall for Stage-2 seed discovery with **no API key** — matters for
the regulated/air-gapped lending context. Everything downstream (`export-chunks`,
`search-chunks`, `discover_seeds_pg`) already routes through `get_embedder`, so a new provider
lights up the whole path end-to-end.

**Not** the literal 7B `nomic-embed-code` CBM vendors (too heavy for CPU-from-Python). Use
**`fastembed`** (ONNX, CPU, pip-installable) with a small model — `nomic-ai/nomic-embed-text-v1.5`
(137M, 768-d) as default, `jinaai/jina-embeddings-v2-base-code` as a code-tuned option.

**Files & signatures**
- `graphify/semantic_index.py` — add:
  ```python
  class LocalEmbedder:  # implements EmbeddingProvider
      def __init__(self, model: str = "nomic-ai/nomic-embed-text-v1.5",
                   *, dim: int = 768, batch: int = 256, cache_dir: str | None = None): ...
      def embed(self, texts: list[str]) -> list[list[float]]:
          # lazy `from fastembed import TextEmbedding`; L2-normalize outputs
          # (semantic_index.cosine assumes normalized vectors)
  ```
  Extend `get_embedder`: add keys `nomic` / `fastembed` → `LocalEmbedder`. **Decision needed:**
  repoint `local`/`offline` to `LocalEmbedder` (best-available, fall back to `HashingEmbedder`
  with a warning if `fastembed` absent) vs. leave them on `hashing`. Recommend repoint, keep
  `hashing`/`none` explicit for the deterministic offline path.
- `pyproject.toml` — new optional extra: `local-embed = ["fastembed>=0.3"]` (keeps base install lean).
- `graphify/__main__.py` — add `local|nomic|fastembed` to the `--embed` help text for
  `export-chunks` / `search-chunks` / `query-graph`.

**Tests** (`tests/test_semantic_index.py`)
- Factory wiring: `get_embedder("nomic")` returns a `LocalEmbedder`; unknown still raises.
- A **fake/monkeypatched** `TextEmbedding` so CI never downloads a model — assert `embed()`
  returns `len==dim`, L2-normalized, count-aligned.
- One `@pytest.mark.skipif(fastembed missing)` real-model smoke test (opt-in).

**Risks / open questions**
- First-run model download (~90–160 MB) + a `cache_dir`; document pre-seeding for air-gapped.
- `code_chunks.embedding` is `vector(dim)` fixed at DDL time → switching embedder dim needs a
  re-DDL/migration; `search-chunks` **must** use the same embedder as `export-chunks`
  (`discover_seeds_pg` already assumes this — document it).

---

## Item 2 — Non-HTTP cross-service extractors (gRPC / GraphQL / pub-sub / IaC)  ·  effort: LARGE  ·  priority: 2

**Goal.** Generalize the contract-as-pivot beyond HTTP so blast radius sees gRPC, GraphQL, and
event/queue edges. Closes gap **F** (recall holes = false "safe") and lays groundwork for **E**
(field-level). Blast radius already traverses `calls_service`/`consumes`/`triggers`, so **new
edges of those types flow in automatically** once emitted + persisted.

**Design — generalize, don't duplicate.** Refactor `contract_introspect.py`'s spike into a small
protocol-extractor framework (or sibling `graphify/rpc_introspect.py` to keep the HTTP spike
stable):
- Each protocol produces the existing `Endpoint` (producer) + `Call` (consumer) dataclasses,
  plus a `pivot` key and `protocol` tag.
- One generalized `cross_service_graph(root)` builds a global catalog keyed by
  `(protocol, pivot)`, matches consumer→producer, and reuses the current
  EXTRACTED/INFERRED(0.9)/AMBIGUOUS(0.5) + `confidence_score` + `to_service`/`via_endpoint`
  emission and the "fan-out to all candidates, never drop" recall policy.

**Per-protocol pivots**
- **gRPC** — producers: `.proto` `package` + `service S { rpc M(...) }` → pivot `grpc:pkg.S/M`;
  optionally link the server impl class. Consumers: `stub.M(...)` call sites (Py/TS/Go/Java).
  *Fully-qualified → near-exact match, rarely AMBIGUOUS (more precise than HTTP).*
- **GraphQL** — producers: SDL `type Query/Mutation/Subscription` fields + resolvers; pivot
  `gql:<Type>.<field>`. Consumers: `gql\`...\`` / `.query/.mutate` operations naming those fields.
- **pub-sub** (`EMITS` / `LISTENS_ON`) — publishers (`.emit(`, `producer.send(`, `ClientProxy.emit`,
  kafka produce) → `EMITS` edge to a `topic` node; subscribers (`@EventPattern`, `@MessagePattern`,
  `consumer.subscribe(`, `.on(`) → `LISTENS_ON`. Cross-service edge = publisher→handler via the
  topic pivot; emit as `edge_type="consumes"`/`"triggers"` so blast radius picks it up.
- **IaC** (lower priority) — Docker/K8s/Kustomize as nodes + service-name references; likely fold
  into existing `manifest_ingest.py` rather than a new module.

**Also closes gap C (wiring).** Add the CLI surface the spike lacks:
`graphify cross-service <root>` (standalone) and a `--cross-service` flag on `extract`; persist via
`pg_export` (`edge_type` per above, `source="contract"`/`"grpc"`/`"graphql"`/`"events"`).

**Tests** — new fixtures under `tests/fixtures/xservice-rpc/` (2–3 services each): a proto +
gRPC client pair; a GraphQL schema + client; a Kafka/Nest emit/subscribe pair. Assert:
`calls_service`/`consumes` edges created, AMBIGUOUS fan-out on colliding pivots, external/3rd-party
calls skipped, generic topics (`health`, `ping`) filtered.

**Reference.** Pull CBM's actual extractor source (edge taxonomy `HTTP_CALLS`/`ASYNC_CALLS`/
`EMITS`/`LISTENS_ON`, confidence scoring) as the port spec during implementation — MIT permits.

**Risks** — AMBIGUOUS explosion on generic topics/paths (filter); regex-first per spike style may
miss dynamic registration; proto/graphql parsing may later want a real parser (start regex, keep
the door open).

---

## Item 3 — Read-only Cypher-subset query surface  ·  effort: MEDIUM  ·  priority: 4 (DEFER)

**Reassessed down.** With `blast_radius_pg`, `discover_seeds_pg`, and callers/callees already
shipped, Cypher is **query ergonomics, not a capability gap**. It would mostly duplicate existing
SQL. Recommend **defer** unless there's concrete demand for ad-hoc graph queries.

**If pursued:** a read-only Cypher-subset (`MATCH`/`WHERE`/`RETURN`/bounded traversal) translated
to SQL over `symbols`/`code_edges`, exposed as one MCP tool. **Blocked on grounding:** confirm how
`serve.py` registers MCP tools first (not yet inspected), and whether to target SQL or the
in-memory `nx.DiGraph`.

---

## Item 4 — Compressed, committable graph artifact  ·  effort: SMALL  ·  priority: 5

CBM commits `.codebase-memory/graph.db.zst` so teammates/CI skip a full reindex. graphify analog:
`graphify export --compress` → `graph.json.zst` (optional `zstandard` dep) + a bootstrap that
imports it before incremental re-extraction. Low urgency; pure convenience.

---

## Item 5 — strict / zero-edge precision mode  ·  effort: TRIVIAL  ·  priority: 3 (fold into Gap A)

CBM's "emit no edge if the receiver is unresolved" is the precision-first counterpart to
graphify's demote-not-delete. Add a `--strict` toggle that **drops** contradicted/unresolved
relational edges instead of demoting them to AMBIGUOUS. Natural to implement **together with the
SCIP per-caller demote work (Gap A)** in `scip_ingest.py`, since both touch the same
supersede/confidence path.

---

## Sequencing

1. **Item 1** (local embedder) — small, self-contained, unblocks offline Stage-2. *Ship first.*
2. **Item 2** (non-HTTP cross-service) — the real differentiator; sub-phase per protocol
   (gRPC → GraphQL → pub-sub → IaC), each with fixtures. Also delivers gap C (CLI wiring).
3. **Item 5** (strict mode) — bundle with the SCIP per-caller demote (Gap A).
4. **Item 3** (Cypher) — defer; revisit only on demand, after a `serve.py` check.
5. **Item 4** (compressed artifact) — opportunistic polish.

## New dependencies
- `fastembed` — **optional** extra `local-embed` (Item 1). Base install unchanged.
- `zstandard` — **optional** (Item 4).
- Items 2/3/5 add **no runtime deps** (regex + stdlib + existing psycopg path).

## Attribution
Reimplementations informed by CBM (MIT, © 2025 DeusData). Where heuristics are ported closely,
note the source in a module docstring per MIT's notice requirement.
