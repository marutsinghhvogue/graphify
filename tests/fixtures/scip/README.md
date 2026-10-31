# SCIP ground-truth fixtures

Real `index.scip` indexes produced by **actual** SCIP indexers
(`scip-python`, `scip-typescript`) over two tiny, hand-understood projects,
plus the expected graph graphify's SCIP reader must produce from them.

These are the oracle for the "true code graph" work: they prove the precision
that name-based (tree-sitter) call resolution **cannot** reach, and they pin
the contract for `graphify.scip_ingest.ingest_scip_index()` — the binary
protobuf reader (parses `index.scip` via the vendored `graphify/scip_pb2.py`).

Consumed by `tests/test_scip_fixtures.py`. The tests need **no** external
tools — they read the committed `index.observed.json`, never the `scip` CLI.

## Why these projects

Each project defines the **same method name on two unrelated classes** and
calls each once. That collision is precisely what defeats a name-based
resolver: it sees two `save()` definitions and two `save()` call sites and
cannot tell which call hits which class, so it cross-links (2×2 = 4 edges,
2 of them wrong). SCIP resolves each call to exactly one class.

| Project | Indexer | Collision | Also exercises |
|---------|---------|-----------|----------------|
| `py_sample` | scip-python | `User.save` vs `Logger.save`, both called by `process()` | call resolution via **occurrence containment** |
| `ts_sample` | scip-typescript | `Dog.save` vs `Database.save`, both called by `run()` | the above **plus** interface `implements` via **symbol relationships** |

The headline assertion (`test_precision_no_spurious_save_edges`): each project
yields **exactly 2** `save()` call edges, not 4.

## Files per project

| File | Role | Committed? |
|------|------|-----------|
| source (`pkg/*.py`, `src/*.ts`, configs) | the project being indexed | yes |
| `index.scip` | real binary SCIP index (protobuf) — input for the future reader | yes |
| `index.observed.json` | faithful, stable JSON dump of `index.scip` (canonical fields only) — read by the ground-truth tests | yes |
| `expected_graph.json` | the graph graphify must emit (nodes/edges by SCIP descriptor) — the reader contract | yes |

`node_modules/`, `dist/`, caches: never committed (see `.gitignore`).

## Regenerating

Requires `node`/`npx` and a built `scip` CLI. The `scip` CLI does not
`go install` cleanly (replace directives); build it from a clone:

```bash
git clone --depth 1 https://github.com/sourcegraph/scip /tmp/scip-src
( cd /tmp/scip-src && go build -o /tmp/scip-src/scip ./cmd/scip )
export SCIP_BIN=/tmp/scip-src/scip
```

### py_sample
The local `pyproject.toml` (name `py_sample`, version `1.0.0`) is load-bearing:
it stops scip-python from climbing to the repo's own `pyproject.toml`, which
would leak graphify's release version and a `tests.fixtures.…` module prefix
into every symbol. Keep it.

```bash
cd py_sample
npx --yes @sourcegraph/scip-python@latest index \
    --project-name py_sample --project-version 1.0.0 --output index.scip .
python ../_normalize_scip.py index.scip          # -> index.observed.json
```

### ts_sample
```bash
cd ts_sample
npx --yes @sourcegraph/scip-typescript@latest index --output index.scip
python ../_normalize_scip.py index.scip          # -> index.observed.json
```

After regenerating, re-run `pytest tests/test_scip_fixtures.py`. If symbols
shifted, reconcile `expected_graph.json` (the descriptors are stable across
indexer versions; the metadata prefix and line numbers are not).

### graphify/scip_pb2.py (protobuf bindings)

`ingest_scip_index()` parses `index.scip` with `graphify/scip_pb2.py`, generated
from `scip.proto`. It is checked in (the proto is stable) so the `scip` extra
only needs the `protobuf` runtime, not a compiler. To regenerate after a proto
bump, point at the same `scip` clone used for the CLI:

```bash
uv pip install grpcio-tools          # build-only; not a runtime/dev dep
uv run python -m grpc_tools.protoc -I /tmp/scip-src \
    --python_out=graphify /tmp/scip-src/scip.proto
```

The gencode pins a minimum `protobuf` runtime (currently 6.33.5); keep the
`scip` extra's lower bound in `pyproject.toml` in sync with it.

## SCIP data model (what the reader must implement)

- A **definition** occurrence carries an `enclosing_range`
  `[startLine, startChar, endLine, endChar]` spanning its body. Plain
  occurrence ranges are `[line, startChar, endChar]` (single line).
- **Call edge**: for each non-definition occurrence referencing a callable
  symbol (descriptor ends `).`), the caller is the **innermost** definition
  whose `enclosing_range` contains the reference. → `caller --calls--> callee`.
- **Implements/extends edge**: `SymbolInformation.relationships[]` with
  `is_implementation` → `symbol --implements--> related`.
- Skip `local N` symbols and `…(self)` parameter symbols.
- All SCIP-derived edges are `EXTRACTED` (type-resolved), unlike the
  heuristic `INFERRED` edges from the tree-sitter path.
