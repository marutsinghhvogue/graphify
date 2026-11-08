"""merge_scip_estate: several modules' index.scip merged onto one estate graph."""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("google.protobuf")

from graphify import scip_pb2  # noqa: E402
from graphify.scip_ingest import (  # noqa: E402
    discover_scip_indexes,
    merge_scip_estate,
    order_scip_modules,
    scip_index_symbols,
)

AUTH = "semanticdb maven maven/com.acme/auth 1.0 com/acme/auth/RequiresRole#"
HANDLER = "semanticdb maven maven/com.acme/svc-a 1.0 com/acme/svc/Handler#"
ON_EVENT = "semanticdb maven maven/com.acme/svc-a 1.0 com/acme/svc/Handler#onEvent()."


def _write_index(path: Path, doc: str, defs: list[tuple[str, int, int]], refs: list[tuple[str, int]]) -> None:
    """defs: (symbol, line, last line of its body); refs: (symbol, line). Lines 0-based."""
    occs = [scip_pb2.Occurrence(range=[line, 4, 10], symbol=sym, symbol_roles=1,
                                enclosing_range=[line, 0, end, 1])
            for sym, line, end in defs]
    occs += [scip_pb2.Occurrence(range=[line, 8, 20], symbol=sym) for sym, line in refs]
    index = scip_pb2.Index(documents=[scip_pb2.Document(relative_path=doc, occurrences=occs)])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(index.SerializeToString())


def _estate(tmp_path: Path) -> Path:
    # svc-a sorts before libs/auth but depends on it.
    _write_index(tmp_path / "svc-a" / "index.scip", "src/Handler.java",
                 defs=[(HANDLER, 2, 9), (ON_EVENT, 4, 6)], refs=[(AUTH, 5)])
    _write_index(tmp_path / "libs" / "auth" / "index.scip", "src/RequiresRole.java",
                 defs=[(AUTH, 3, 8)], refs=[])
    (tmp_path / "svc-a" / "target").mkdir()
    (tmp_path / "svc-a" / "target" / "index.scip").write_bytes(b"not an index")
    return tmp_path


def _base() -> dict:
    def n(nid, label, f, line):
        return {"id": nid, "label": label, "file_type": "code", "source_file": f, "source_location": f"L{line}"}
    return {
        "nodes": [n("handler", "Handler", "svc-a/src/Handler.java", 3),
                  n("on_event", ".onEvent()", "svc-a/src/Handler.java", 5),
                  n("perm", "RequiresRole", "libs/auth/src/RequiresRole.java", 4)],
        "edges": [{"source": "on_event", "target": "perm", "relation": "references",
                   "confidence": "EXTRACTED", "source_file": "svc-a/src/Handler.java"}],
    }


def test_discovery_skips_build_output(tmp_path):
    root = _estate(tmp_path)
    assert sorted(p.relative_to(root).as_posix() for p in discover_scip_indexes(root)) == [
        "libs/auth/index.scip", "svc-a/index.scip"]


def test_providers_are_ordered_before_their_consumers(tmp_path):
    root = _estate(tmp_path)
    idx = discover_scip_indexes(root)
    defined, referenced = scip_index_symbols(root / "svc-a" / "index.scip")
    assert AUTH in referenced and HANDLER in defined
    order = order_scip_modules({p: scip_index_symbols(p) for p in idx})
    assert [p.parent.relative_to(root).as_posix() for p in order] == ["libs/auth", "svc-a"]


def test_cross_module_reference_joins_the_defining_module(tmp_path):
    root = _estate(tmp_path)
    out = merge_scip_estate(_base(), root)
    rec = out["reconciliation"]
    assert rec["modules"] == 2 and rec["matched"] == 3 and rec["demoted"] == 0
    assert [m["module"] for m in out["modules"]] == ["libs/auth", "svc-a"]
    # no separate external node for RequiresRole: svc-a's reference is the lib's node
    assert not [n for n in out["nodes"] if (n.get("metadata") or {}).get("scip_external")]
    refs = [e for e in out["edges"] if e["relation"] == "references"]
    assert [(e["source"], e["target"], e.get("context")) for e in refs] == [("on_event", "perm", "scip")]


def test_merge_scip_cli_estate_mode(tmp_path, monkeypatch, capsys):
    import json
    import sys

    from graphify.__main__ import main

    root = _estate(tmp_path / "estate")
    graph = tmp_path / "graph.json"
    graph.write_text(json.dumps(_base()))
    out = tmp_path / "merged.json"
    monkeypatch.setattr(sys, "argv", ["graphify", "merge-scip", str(root), "--graph", str(graph),
                                      "--out", str(out)])
    main()
    printed = capsys.readouterr().out
    assert "modules 2, merged providers first" in printed
    assert out.exists()
