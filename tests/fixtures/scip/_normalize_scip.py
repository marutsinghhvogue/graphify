"""Fixture helper: turn a binary ``index.scip`` into a faithful, stable JSON dump.

This is NOT graphify's SCIP reader. It is a fixture-generation tool that
captures exactly what a real indexer (scip-python / scip-typescript) emitted,
in a form that is:

  * canonical — only the SCIP protobuf fields we depend on
    (relative_path, occurrences[range, symbol, symbol_roles, enclosing_range],
    symbols[symbol, kind, display_name, relationships]);
  * stable — drops the index-level ``metadata`` (tool version etc.) and the
    Go-specific ``Typed*`` keys that ``scip print --json`` leaks, so the
    committed dump does not churn when the indexer version changes;
  * pure-JSON readable — committed alongside ``index.scip`` so tests can assert
    against the ground truth WITHOUT needing the ``scip`` CLI or protobuf
    bindings in CI.

Regenerate (from this directory), pointing SCIP_BIN at a built ``scip`` CLI:

    SCIP_BIN=/path/to/scip python _normalize_scip.py py_sample/index.scip
    SCIP_BIN=/path/to/scip python _normalize_scip.py ts_sample/index.scip

Writes ``<dir>/index.observed.json`` next to the input.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# SCIP SymbolRole bitmask (github.com/sourcegraph/scip scip.proto).
ROLE_DEFINITION = 0x1

_REL_FLAGS = ("is_reference", "is_implementation", "is_type_definition", "is_definition")


def _print_json(scip_bin: str, scip_path: Path) -> dict:
    out = subprocess.run(
        [scip_bin, "print", "--json", str(scip_path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(out)


def _norm_occurrence(occ: dict) -> dict:
    out = {
        "range": occ.get("range", []),
        "symbol": occ.get("symbol", ""),
        "symbol_roles": occ.get("symbol_roles", 0),
    }
    if "enclosing_range" in occ:
        out["enclosing_range"] = occ["enclosing_range"]
    return out


def _norm_symbol(sym: dict) -> dict:
    out: dict = {"symbol": sym.get("symbol", "")}
    if sym.get("kind"):
        out["kind"] = sym["kind"]
    if sym.get("display_name"):
        out["display_name"] = sym["display_name"]
    rels = []
    for rel in sym.get("relationships", []) or []:
        r = {"symbol": rel.get("symbol", "")}
        for flag in _REL_FLAGS:
            if rel.get(flag):
                r[flag] = True
        rels.append(r)
    if rels:
        out["relationships"] = rels
    return out


def normalize(raw: dict) -> dict:
    docs = []
    for doc in raw.get("documents", []):
        docs.append(
            {
                "relative_path": doc.get("relative_path", ""),
                "language": doc.get("language", ""),
                "occurrences": [_norm_occurrence(o) for o in doc.get("occurrences", [])],
                "symbols": [_norm_symbol(s) for s in doc.get("symbols", [])],
            }
        )
    # Stable ordering: documents by path, occurrences by (range, symbol).
    docs.sort(key=lambda d: d["relative_path"])
    for d in docs:
        d["occurrences"].sort(key=lambda o: (o["range"], o["symbol"]))
        d["symbols"].sort(key=lambda s: s["symbol"])
    return {"documents": docs}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    scip_bin = os.environ.get("SCIP_BIN", "scip")
    scip_path = Path(argv[1]).resolve()
    raw = _print_json(scip_bin, scip_path)
    normalized = normalize(raw)
    out_path = scip_path.with_name("index.observed.json")
    out_path.write_text(json.dumps(normalized, indent=2, sort_keys=False) + "\n")
    n_occ = sum(len(d["occurrences"]) for d in normalized["documents"])
    print(f"wrote {out_path}  ({len(normalized['documents'])} docs, {n_occ} occurrences)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
