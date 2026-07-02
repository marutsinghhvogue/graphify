"""SCIP ground-truth fixtures + reader contract.

Two layers:

1. ``TestFixtureGroundTruth`` — runs now, pure stdlib. It re-derives the
   precise call/implements edges from the committed ``index.observed.json``
   (a faithful, stable dump of the real ``index.scip`` produced by
   scip-python / scip-typescript) and asserts they match each project's
   ``expected_graph.json``. This proves the fixtures genuinely encode the
   precision that name-based (tree-sitter) resolution cannot reach, and the
   ``_derive_*`` helpers are a reference implementation of the algorithm the
   real reader must perform.

2. ``TestReaderContract`` — skipped until ``graphify.scip_ingest`` grows a
   real protobuf reader (``ingest_scip_index(path)``). It asserts the reader
   reproduces ``expected_graph.json`` from the binary ``index.scip``. This is
   the spec for the next phase.

See tests/fixtures/scip/README.md for how the fixtures were generated.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "scip"
PROJECTS = ["py_sample", "ts_sample"]

# SCIP SymbolRole bitmask (scip.proto).
ROLE_DEFINITION = 0x1


# ── SCIP symbol helpers ─────────────────────────────────────────────────────

def descriptor(symbol: str) -> str:
    """Return the stable SCIP *descriptor* — the tail after the 4 metadata
    fields ``<scheme> <manager> <package> <version>``. Locals pass through."""
    if symbol.startswith("local "):
        return symbol
    parts = symbol.split(" ")
    return " ".join(parts[4:]) if len(parts) > 4 else symbol


def is_local(symbol: str) -> bool:
    return symbol.startswith("local ")


def is_callable(desc: str) -> bool:
    """Method/function descriptors end in ')." (e.g. ``User#save().``)."""
    return desc.endswith(").")


def is_param(desc: str) -> bool:
    """Parameter symbols look like ``User#save().(self)``."""
    return desc.endswith(")") and not desc.endswith(").")


def _norm_range(r: list[int]) -> tuple[int, int, int, int]:
    """SCIP ranges are [l, c1, c2] (single line) or [l1, c1, l2, c2]."""
    if len(r) == 3:
        return (r[0], r[1], r[0], r[2])
    if len(r) == 4:
        return (r[0], r[1], r[2], r[3])
    raise ValueError(f"bad range {r!r}")


def _contains(outer: tuple[int, int, int, int], inner: tuple[int, int, int, int]) -> bool:
    osl, osc, oel, oec = outer
    isl, isc, iel, iec = inner
    return (isl, isc) >= (osl, osc) and (iel, iec) <= (oel, oec)


def _line_span(rng: tuple[int, int, int, int]) -> int:
    return rng[2] - rng[0]


# ── reference implementation of the reader's core algorithm ─────────────────

def _scopes(doc: dict) -> list[tuple[str, tuple[int, int, int, int]]]:
    """Definitions that own a body (have ``enclosing_range``) — candidate
    enclosing callers. Locals excluded."""
    out = []
    for occ in doc["occurrences"]:
        if "enclosing_range" in occ and not is_local(occ["symbol"]):
            out.append((occ["symbol"], _norm_range(occ["enclosing_range"])))
    return out


def _innermost_scope(scopes, ref_range, exclude_symbol):
    """Smallest-line-span scope that contains ref_range (the caller)."""
    best = None
    for sym, enc in scopes:
        if sym == exclude_symbol:
            continue
        if _contains(enc, ref_range):
            if best is None or _line_span(enc) < _line_span(best[1]):
                best = (sym, enc)
    return best[0] if best else None


def _derive_call_edges(observed: dict) -> list[tuple[str, str]]:
    """(caller_desc, callee_desc) for every reference to a callable symbol,
    attributed to its innermost enclosing definition."""
    edges = []
    for doc in observed["documents"]:
        scopes = _scopes(doc)
        for occ in doc["occurrences"]:
            sym = occ["symbol"]
            if is_local(sym):
                continue
            if occ.get("symbol_roles", 0) & ROLE_DEFINITION:
                continue  # this occurrence IS a definition, not a use
            callee = descriptor(sym)
            if not is_callable(callee):
                continue
            caller_sym = _innermost_scope(scopes, _norm_range(occ["range"]), sym)
            if caller_sym is None:
                continue
            edges.append((descriptor(caller_sym), callee))
    return edges


def _derive_implements_edges(observed: dict) -> list[tuple[str, str]]:
    edges = []
    for doc in observed["documents"]:
        for s in doc["symbols"]:
            for rel in s.get("relationships", []):
                if rel.get("is_implementation"):
                    edges.append((descriptor(s["symbol"]), descriptor(rel["symbol"])))
    return edges


def _definitions(observed: dict) -> set[str]:
    """Descriptors of all real (non-local, non-param) defined symbols."""
    defs = set()
    for doc in observed["documents"]:
        for occ in doc["occurrences"]:
            if not (occ.get("symbol_roles", 0) & ROLE_DEFINITION):
                continue
            if is_local(occ["symbol"]):
                continue
            d = descriptor(occ["symbol"])
            if is_param(d) or d.endswith("/__init__:") or d.endswith("`/") or d.endswith(".ts`/"):
                continue
            defs.add(d)
    return defs


def _load(project: str, name: str) -> dict:
    return json.loads((FIXTURES / project / name).read_text())


# ── Layer 1: fixture ground truth (runs now) ────────────────────────────────

class TestFixtureGroundTruth:
    @pytest.mark.parametrize("project", PROJECTS)
    def test_fixtures_present(self, project: str) -> None:
        for fname in ("index.scip", "index.observed.json", "expected_graph.json"):
            assert (FIXTURES / project / fname).exists(), f"{project}/{fname} missing"

    @pytest.mark.parametrize("project", PROJECTS)
    def test_expected_call_edges_are_derivable(self, project: str) -> None:
        observed = _load(project, "index.observed.json")
        expected = _load(project, "expected_graph.json")
        derived = set(_derive_call_edges(observed))
        for edge in expected["expected_edges"]:
            if edge["relation"] != "calls":
                continue
            assert (edge["from"], edge["to"]) in derived, (
                f"{project}: expected call {edge['from']} -> {edge['to']} "
                f"not derivable from SCIP; got {sorted(derived)}"
            )

    @pytest.mark.parametrize("project", PROJECTS)
    def test_expected_implements_edges_are_derivable(self, project: str) -> None:
        observed = _load(project, "index.observed.json")
        expected = _load(project, "expected_graph.json")
        wanted = [(e["from"], e["to"]) for e in expected["expected_edges"]
                  if e["relation"] == "implements"]
        if not wanted:
            pytest.skip(f"{project} has no implements edges")
        derived = set(_derive_implements_edges(observed))
        for frm, to in wanted:
            assert (frm, to) in derived, f"{project}: missing implements {frm} -> {to}"

    @pytest.mark.parametrize("project", PROJECTS)
    def test_precision_no_spurious_save_edges(self, project: str) -> None:
        """The headline claim: each save() call site resolves to exactly one
        class. A name-based resolver would cross-link (2 sites x 2 defs = 4)."""
        observed = _load(project, "index.observed.json")
        expected = _load(project, "expected_graph.json")
        inv = expected["precision_invariants"]
        count_key = next(k for k in inv if k.endswith("_call_edge_count"))
        expected_count = inv[count_key]
        derived = _derive_call_edges(observed)
        save_edges = [(a, b) for (a, b) in derived if b.endswith("save().")]
        assert len(save_edges) == expected_count, (
            f"{project}: expected exactly {expected_count} save() call edges "
            f"(precise resolution); got {len(save_edges)}: {save_edges}"
        )

    @pytest.mark.parametrize("project", PROJECTS)
    def test_expected_nodes_are_defined_symbols(self, project: str) -> None:
        observed = _load(project, "index.observed.json")
        expected = _load(project, "expected_graph.json")
        defs = _definitions(observed)
        for node in expected["expected_nodes"]:
            assert node["descriptor"] in defs, (
                f"{project}: expected node {node['descriptor']} is not a defined "
                f"symbol in the index"
            )

    @pytest.mark.parametrize("project", PROJECTS)
    def test_no_edges_touch_locals_or_params(self, project: str) -> None:
        observed = _load(project, "index.observed.json")
        for a, b in _derive_call_edges(observed):
            for endpoint in (a, b):
                assert not endpoint.startswith("local "), f"{project}: local in edge"
                assert not is_param(endpoint), f"{project}: param symbol in edge {a}->{b}"


# ── Layer 2: reader contract (skipped until the reader exists) ───────────────

try:
    import graphify.scip_pb2  # noqa: F401  # requires the 'scip' extra (protobuf)
    from graphify.scip_ingest import ingest_scip_index  # noqa: F401

    _HAS_READER = True
except ImportError:
    _HAS_READER = False

_SKIP_REASON = (
    "SCIP protobuf reader unavailable — install the 'scip' extra (protobuf). "
    "Contract: tests/fixtures/scip/*/expected_graph.json"
)


def _reader_edges_by_descriptor(result: dict) -> set[tuple[str, str, str]]:
    sym_of = {n["id"]: n.get("metadata", {}).get("scip_symbol", "") for n in result["nodes"]}
    return {
        (descriptor(sym_of.get(e["source"], "")),
         descriptor(sym_of.get(e["target"], "")),
         e["relation"])
        for e in result["edges"]
    }


@pytest.mark.skipif(not _HAS_READER, reason=_SKIP_REASON)
class TestReaderContract:
    """Activates automatically once ingest_scip_index + protobuf are available."""

    @pytest.mark.parametrize("project", PROJECTS)
    def test_reader_emits_expected_edges(self, project: str) -> None:
        from graphify.scip_ingest import ingest_scip_index

        result = ingest_scip_index(FIXTURES / project / "index.scip")
        expected = _load(project, "expected_graph.json")
        got = _reader_edges_by_descriptor(result)
        for e in expected["expected_edges"]:
            assert (e["from"], e["to"], e["relation"]) in got, (
                f"{project}: reader missing {e['relation']} {e['from']} -> {e['to']}"
            )

    @pytest.mark.parametrize("project", PROJECTS)
    def test_reader_output_is_schema_valid_and_extracted(self, project: str) -> None:
        from graphify.scip_ingest import ingest_scip_index
        from graphify.validate import validate_extraction

        result = ingest_scip_index(FIXTURES / project / "index.scip")
        assert validate_extraction(result) == []  # endpoint-safe + required fields
        assert result["edges"], f"{project}: reader produced no edges"
        assert all(e["confidence"] == "EXTRACTED" for e in result["edges"])

    @pytest.mark.parametrize("project", PROJECTS)
    def test_reader_precision_no_spurious_save_edges(self, project: str) -> None:
        """The reader itself (not just the fixture) resolves each save() call
        site to exactly one class — no name-based cross-linking."""
        from graphify.scip_ingest import ingest_scip_index

        result = ingest_scip_index(FIXTURES / project / "index.scip")
        expected = _load(project, "expected_graph.json")
        inv = expected["precision_invariants"]
        count_key = next(k for k in inv if k.endswith("_call_edge_count"))
        sym_of = {n["id"]: n.get("metadata", {}).get("scip_symbol", "") for n in result["nodes"]}
        save_calls = [
            (descriptor(sym_of.get(e["source"], "")), descriptor(sym_of.get(e["target"], "")))
            for e in result["edges"]
            if e["relation"] == "calls" and descriptor(sym_of.get(e["target"], "")).endswith("save().")
        ]
        assert len(save_calls) == inv[count_key], (
            f"{project}: expected {inv[count_key]} save() call edges, got {len(save_calls)}: {save_calls}"
        )
