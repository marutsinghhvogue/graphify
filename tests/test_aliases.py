"""Human-supplied entity aliases: merge / same_as, and the write path."""
from __future__ import annotations

import json

from graphify.aliases import append_alias, load_aliases
from graphify.build import build_from_json


def _base_extraction():
    # A doc concept "User" and a code class "Customer" — different names, same
    # concept — plus a caller that references the doc node.
    return {
        "nodes": [
            {"id": "readme_user", "label": "User", "file_type": "document", "source_file": "README.md"},
            {"id": "models_customer", "label": "Customer", "file_type": "code", "source_file": "models.py"},
            {"id": "auth_login", "label": "login", "file_type": "code", "source_file": "auth.py"},
        ],
        "edges": [
            {"source": "auth_login", "target": "readme_user", "relation": "references", "confidence": "EXTRACTED"},
        ],
    }


def _write_aliases(root, aliases):
    (root / ".graphify_aliases.json").write_text(
        json.dumps({"version": 1, "aliases": aliases}), encoding="utf-8"
    )


def test_no_alias_file_is_noop(tmp_path):
    G = build_from_json(_base_extraction(), directed=True, root=tmp_path)
    assert "readme_user" in G and "models_customer" in G
    assert not any(d.get("relation") == "same_as" for _, _, d in G.edges(data=True))


def test_same_as_adds_extracted_edge_and_keeps_both_nodes(tmp_path):
    _write_aliases(tmp_path, [{"from": "User", "to": "Customer", "mode": "same_as", "reason": "wiki User == code Customer"}])
    G = build_from_json(_base_extraction(), directed=True, root=tmp_path)

    assert "readme_user" in G and "models_customer" in G
    assert G.has_edge("readme_user", "models_customer")
    d = G.get_edge_data("readme_user", "models_customer")
    assert d["relation"] == "same_as"
    assert d["confidence"] == "EXTRACTED"
    assert d["confidence_score"] == 1.0


def test_merge_removes_from_node_and_repoints_edges(tmp_path):
    _write_aliases(tmp_path, [{"from": "User", "to": "Customer", "mode": "merge"}])
    G = build_from_json(_base_extraction(), directed=True, root=tmp_path)

    # The "User" node is gone; its inbound edge now points at Customer.
    assert "readme_user" not in G
    assert "models_customer" in G
    assert G.has_edge("auth_login", "models_customer")


def test_ambiguous_ref_is_skipped_not_crashed(tmp_path):
    extraction = _base_extraction()
    # A second node also labelled "User" makes the ref ambiguous.
    extraction["nodes"].append(
        {"id": "docs_user", "label": "User", "file_type": "document", "source_file": "docs/user.md"}
    )
    _write_aliases(tmp_path, [{"from": "User", "to": "Customer", "mode": "same_as"}])
    G = build_from_json(extraction, directed=True, root=tmp_path)

    # Both "User" nodes survive; no same_as edge was created.
    assert "readme_user" in G and "docs_user" in G
    assert not any(d.get("relation") == "same_as" for _, _, d in G.edges(data=True))


def test_match_by_exact_node_id(tmp_path):
    _write_aliases(tmp_path, [{"from": "readme_user", "to": "models_customer", "mode": "same_as"}])
    G = build_from_json(_base_extraction(), directed=True, root=tmp_path)
    assert G.has_edge("readme_user", "models_customer")


def test_append_alias_creates_and_dedups(tmp_path):
    append_alias(tmp_path, "User", "Customer", reason="first")
    append_alias(tmp_path, "User", "Customer", reason="second")  # same identity -> overwrite
    append_alias(tmp_path, "Order", "Purchase", mode="merge")

    entries = load_aliases(tmp_path)
    assert len(entries) == 2
    user = next(e for e in entries if e["from"] == "User")
    assert user["reason"] == "second"
    assert user["mode"] == "same_as"
    order = next(e for e in entries if e["from"] == "Order")
    assert order["mode"] == "merge"


def test_load_aliases_missing_root_or_file(tmp_path):
    assert load_aliases(None) == []
    assert load_aliases(tmp_path) == []  # no file yet


def test_invalid_mode_falls_back_to_same_as(tmp_path):
    _write_aliases(tmp_path, [{"from": "User", "to": "Customer", "mode": "bogus"}])
    entries = load_aliases(tmp_path)
    assert entries[0]["mode"] == "same_as"
