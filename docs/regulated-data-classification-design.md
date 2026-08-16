# Regulated-data classification & compliance-path detection — design

**Status:** PROPOSED (spec). Closes gap #1 from the workflow gap analysis — the
missing layer that Stage 2 ("detect regulated data & compliance-sensitive paths")
of `docs/loan-repayment-change-workflow.md` depends on, and which in turn gates
Stage 4 (scope restriction) and Stage 7 (reviewer routing).

**Thesis:** compliance classification is the *same shape* as scheduler/event
detection — a signal in the source marks a definition, and we want a typed label
on it — so it reuses the proven `binding_rules` engine + `learn_bindings`
author-time loop rather than a bespoke module. Rules are **data**, the LLM
**authors** rules (never runs at extraction time), and execution is
**deterministic, reproducible, and free**. See [[binding-rule-engine]].

---

## What's different from a binding rule (and why it needs its own family)

A `BindingRule` binds a **construct → handler** (annotation on the *next def*) and
emits a synthetic node + edge. Compliance is not a construct→handler binding — it
**labels existing data-bearing nodes** (fields, columns, params, entities,
routes) with a sensitivity classification, then **propagates** those labels along
the graph to derive sensitive *paths*. So it is a sibling rule family with:

- a **`signal`** (what to match: a name, a type, an annotation, a DB column, a
  path segment, a doc keyword) instead of a fixed "annotation on next def";
- a **`label`** (`category` + `regulations`) instead of a `relation`/`node_kind`;
- output that **stamps `metadata.compliance` on nodes** and adds propagation
  edges, instead of emitting construct nodes.

Everything else — the rules-as-data dataclass, JSON plug-in file, `validate_rule`
fail-loud discipline, built-in + external + llm origins, and the author-time loop
— is reused verbatim in spirit.

## The two passes

```
  classify (deterministic)                 propagate (graph)
  ────────────────────────                 ──────────────────────────────
  field "ssn"        ─rule→ PII            PII field ──handles──▶ route  ┐
  column "card_no"   ─rule→ PCI            PCI col   ──returns──▶ DTO     ├─▶ sensitive
  field "interest_rate"─rule→ FINANCIAL    FIN field ──calls_service──▶ B ┘   path + rollup
  → stamp node.metadata.compliance         → tag paths, roll up to service/endpoint
```

1. **Classify** (`compliance_rules.run_compliance`, analog of `run_bindings`):
   deterministic scan; every matching rule stamps a compliance tag on the node it
   applies to. No LLM.
2. **Propagate** (`compliance_propagate`): a taint-style graph pass turns
   node-level labels into **compliance-sensitive paths** and **service/endpoint
   rollups**. Recall-favoring, directional, depth-capped, confidence-decaying.

---

## The rule schema (`ComplianceRule`)

Mirrors `BindingRule`; validated the same way (`validate_rule` sibling that
fails loud on a bad LLM proposal).

```jsonc
{
  "id": "financial.name.interest",          // unique dotted id
  "category": "financial",                   // pii | pci | phi | financial | consent
  "regulations": ["TILA/Reg-Z", "GLBA"],     // extensible list
  "signal": "name",                          // name | type | annotation | column | path | doc
  "languages": ["python", "java", "ts"],     // or ["*"] for signal=column/path/doc
  "pattern": "(?i)\\b(interest_rate|apr|apy|finance_charge)\\b",
  "applies_to": ["field", "param", "column", "entity"],  // node kinds it may tag
  "confidence": "INFERRED",                  // EXTRACTED (explicit annotation) | INFERRED (name/type) | AMBIGUOUS
  "description": "APR / interest / finance-charge fields (Reg Z disclosure)"
}
```

Signal types and their high-precision cases:

| `signal` | Matches against | Confidence | Example |
|----------|-----------------|-----------|---------|
| `annotation` | a decorator/marker on the def | **EXTRACTED** | `@PII`, `@Sensitive`, `@Encrypted`, `@Column(name="ssn")` |
| `type` | the declared type | INFERRED | `EmailStr`, `SSN`, `CardNumber`, `Money` |
| `name` | identifier / field / param name | INFERRED | `ssn`, `dob`, `card_number`, `iban`, `interest_rate` |
| `column` | DB column from `pg_introspect` | INFERRED | `payments.card_last4` |
| `path` | endpoint path segment | INFERRED | `/statements`, `/payments`, `/kyc` |
| `doc` | comment / docstring keyword | AMBIGUOUS | `# PCI scope`, `regulated` |

Built-in seed registry (extended without code via the JSON file):

```
pii.annotation.marker    @PII|@Sensitive|@PersonalData                → PII / GDPR,CCPA   (EXTRACTED)
pii.name.identity        ssn|social_security|passport|dob|date_of_birth→ PII / GDPR        (INFERRED)
pii.type.email           EmailStr|Email                               → PII / GDPR        (INFERRED)
pci.name.card            card_number|pan|cvv|card_last4|expiry         → PCI / PCI-DSS     (INFERRED)
phi.name.health          diagnosis|icd10|mrn|health_plan               → PHI / HIPAA       (INFERRED)
financial.name.interest  interest_rate|apr|apy|finance_charge          → FINANCIAL / TILA  (INFERRED)
financial.name.account   account_number|routing_number|iban|balance    → FINANCIAL / GLBA  (INFERRED)
consent.name.contact     phone|mobile|marketing_opt_in                 → CONSENT / TCPA    (INFERRED)
```

## The node tag (output)

Classification stamps a list onto existing nodes (a node can carry several):

```jsonc
"metadata": {
  "compliance": [
    { "category": "financial", "regulations": ["TILA/Reg-Z"],
      "confidence": "INFERRED", "rule": "financial.name.interest",
      "evidence": "field name 'interest_rate' at interest_calc.py:L42",
      "propagated": false }
  ]
}
```

Propagated tags carry `"propagated": true`, `"via": ["node→node→…"]`, and a
**decayed** confidence, so a hard gate can require `propagated=false` (a direct
hit) while review surfaces the propagated ones.

## Propagation & rollup

Sensitivity flows **from a definition outward to anything that reads or transmits
it**, along existing relations, direction-aware:

- `field/column/param (regulated) --handles/returns--> route` ⇒ the route carries it
- `route --calls_service--> handler` ⇒ the consumer service's call path carries it
- `entity --contains--> field` and `function --references--> entity` ⇒ propagate up
- `event/topic --consumes--> handler` ⇒ the listener path carries it

Bounded to stay useful (not taint the whole graph): a **depth cap**, **confidence
decay** per hop, and a **stop set** (generic/non-data nodes). Recall-favoring
(never drop a candidate — the `demote-not-delete` policy) but ranked by
confidence so the noise is filterable.

**Rollup:** a *service* is compliance-sensitive if it owns or transmits any
regulated node; an *endpoint* is sensitive if its handler touches regulated
fields. These rollups are exactly what Stages 4/7 consume.

---

## The author-time loop (`learn_compliance`, mirrors `learn_bindings`)

Same reconciliation of "an LLM knows every domain vocabulary" with "the gate must
be deterministic": the LLM writes rules; the engine runs them.

1. **`harvest_data_candidates`** (pure): find data-bearing nodes/identifiers that
   **no compliance rule matches** — grounds the LLM in real fields/columns/paths
   that exist, not hallucinated ones. (The compliance analog of
   `harvest_candidates`, seeded from AST fields, `pg_introspect` columns, and
   contract params.)
2. **`propose_compliance_rules`** — ask the LLM to author `ComplianceRule` rows
   for the estate's own domain terms (e.g. lending vocabulary: `emi`, `disbursal`,
   `foreclosure_charge`, `principal_outstanding`). Every proposal is
   `validate_rule`-checked; invalid rows dropped, loud.
3. **`persist_compliance_rules`** — a human **ratifies** (compliance owner), then
   rules are written to `.graphify_compliance_rules.json` and run deterministically
   forever after. **Only authoring touches an LLM.**

This is the propose → verify → promote loop from the entity-resolution discussion
(gap #2), applied to sensitivity labels.

---

## CLI surface

```bash
graphify extract --compliance          # classify + propagate; stamp tags; source='compliance'
graphify learn-compliance              # harvest → LLM-author rules → ratify → persist
graphify compliance "<node|label>"     # why is this tagged? show rules + propagation path
graphify affected "X" --regulated-only # blast radius filtered to compliance-sensitive paths
```

Tags merge into `graph.json` with `source='compliance'`, so they persist through
`graphify export-pg --source compliance` — the same idiom as `source='contract'`.

## Where it plugs into the workflow

- **Stage 2** — *is* this pass (classify + propagate + rollup).
- **Stage 4 (restrict scope)** — service/endpoint/file rollups auto-derive the
  agent's **deny/read-only set**: any node tagged `confidence=EXTRACTED|INFERRED,
  propagated=false` in a regulated category is human-only. Enforced via a
  pre-edit hook, not convention.
- **Stage 7 (reviewer routing)** — the tag `category` maps to a required reviewer:
  `pci|pii|phi → Security`; `financial|consent → Compliance`; cross-service
  propagation → Architecture.
- **Stage 6 (PR evidence)** — every tag's `evidence` + `rule` is auditable, so the
  compliance reviewer sees *why* a path was flagged.

## Precision / recall policy

The dangerous error is a **false negative** (missing regulated data → an agent
edits it unnoticed). So the pass is **recall-favoring**: over-tag, never silently
drop, and expose confidence so humans triage. But hard gates key only on
**high-confidence, non-propagated** tags to avoid alert fatigue; propagated and
`doc`/AMBIGUOUS tags inform review, not block.

## Non-goals / accountability

This is a **detector that assists**, not a legal determination. It never certifies
compliance and never *clears* a path — a human compliance owner decides, and (per
[[binding-rule-engine]] and `ros-accountability`) the agent may propose a diff on a
regulated path but never authors or closes it.

## Testing

- Fixtures with known regulated fields (`ssn`, `card_number`, `interest_rate`,
  `phone`) across Python/Java/TS **and** a `pg_introspect` schema; assert node
  tags, propagation to the serving endpoint, and service rollup.
- `validate_rule` regression: a malformed compliance rule fails loud (bad regex,
  bad category, missing field) — one bad LLM row never corrupts the graph.
- **Determinism**: same input → identical tags every run (no LLM at extraction).
- Golden `stats`: `{classified, by_category, by_regulation, propagated, rules}`.

## Phasing

| Tier | Scope | LLM? | Depends on |
|------|-------|------|-----------|
| **A** | Deterministic built-in rules, node-level tags (name/annotation/type/column) | no | ships now |
| **B** | Propagation + sensitive-path + service/endpoint rollup; `--regulated-only` blast radius | no | Tier A |
| **C** | `learn-compliance` — LLM authors estate-specific rules, ratify + persist | author-time only | Tier A |
| **D** | Field-level tags inside request/response bodies (precise Q2 breakage) | no | **schema/field nodes (gap #3)** |

Tier A+B is the MVP that unblocks Stages 4/7 of the workflow. Tier D is where
compliance classification and field-level granularity (gap #3) reinforce each
other: once request/response fields are nodes, a *field* change gets a precise
regulated-or-not answer instead of an endpoint-level over-approximation.

## Related
- [[binding-rule-engine]] — the engine + author-time pattern this reuses.
- `docs/loan-repayment-change-workflow.md` — Stage 2 consumer of this output.
- `docs/cross-service-impact-analysis.md` — the `source=`/reconcile idiom and the
  field-level (gap #3) dependency for Tier D.
