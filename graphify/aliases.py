"""Human-supplied entity aliases: assert that two nodes are the same concept.

Graphify has no automatic synonym resolution — a doc that calls something
"User" and code that implements it as ``Customer`` produce two disconnected
nodes, and neither dedup (label-similarity) nor symbol resolution (exact-name)
will ever link them (see docs/how-it-works.md). This module lets a human close
that gap deterministically by declaring the equivalence in a version-controlled
``.graphify_aliases.json`` at the repo root.

Two modes:

* ``same_as`` (default) — keep both nodes, add a directed ``same_as`` edge with
  ``confidence: EXTRACTED`` (1.0). Non-destructive and reversible: delete the
  line and rebuild. Preserves the doc/code provenance of each node.
* ``merge`` — the ``from`` node is removed and every edge touching it is
  re-pointed onto the ``to`` node. Use only when the two names are genuinely one
  entity. This piggybacks on the same edge-repointing path ``build_from_json``
  already uses to fold LLM ghost-duplicate nodes into their AST twins.

Refs (``from``/``to``) are matched against node **labels** (case-insensitive)
first as an exact node ID, so humans type what they see ("User") rather than
internal IDs ("readme_user"). Resolution is conservative: an alias whose ref
matches zero or more-than-one node is skipped with a warning — mirroring the
"only act when exactly one candidate exists" rule used by symbol_resolution and
the ghost-merge collision guard in build.py.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .ids import normalize_id

__all__ = [
    "ALIAS_FILENAME",
    "default_aliases_path",
    "load_aliases",
    "apply_aliases",
    "append_alias",
]

ALIAS_FILENAME = ".graphify_aliases.json"

_VALID_MODES = ("same_as", "merge")


def default_aliases_path(root: str | Path | None) -> Path | None:
    """Path to the alias file for a repo root, or None when root is unknown."""
    if not root:
        return None
    return Path(root) / ALIAS_FILENAME


def load_aliases(root: str | Path | None) -> list[dict]:
    """Load and normalize alias entries from ``<root>/.graphify_aliases.json``.

    Returns a deterministically-ordered list of entries, each a dict with keys
    ``from``, ``to``, ``mode``, ``reason``. Missing file / unknown root yields an
    empty list. Malformed entries are skipped with a warning rather than raising,
    so one bad line never breaks a build.
    """
    path = default_aliases_path(root)
    if path is None or not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        _warn(f"could not read {ALIAS_FILENAME}: {exc}")
        return []
    raw = data.get("aliases", []) if isinstance(data, dict) else []
    entries: list[dict] = []
    for a in raw:
        if not isinstance(a, dict):
            continue
        frm, to = str(a.get("from", "")).strip(), str(a.get("to", "")).strip()
        if not frm or not to:
            _warn(f"alias with empty 'from'/'to' skipped: {a!r}")
            continue
        mode = a.get("mode", "same_as")
        if mode not in _VALID_MODES:
            _warn(f"alias {frm!r}->{to!r} has invalid mode {mode!r}; using 'same_as'")
            mode = "same_as"
        entries.append(
            {"from": frm, "to": to, "mode": mode, "reason": str(a.get("reason", ""))}
        )
    # Deterministic order so the resulting graph is byte-stable across rebuilds.
    entries.sort(key=lambda e: (e["from"].casefold(), e["to"].casefold(), e["mode"]))
    return entries


def _build_label_index(node_attrs) -> dict[str, list[str]]:
    """Map case-folded label -> [node ids]. ``node_attrs`` is G.nodes(data=True)."""
    idx: dict[str, list[str]] = {}
    for nid, d in node_attrs:
        lab = str(d.get("label", "")).strip().casefold()
        if lab:
            idx.setdefault(lab, []).append(nid)
    return idx


def _resolve_ref(ref: str, node_set: set, label_index: dict[str, list[str]]) -> list[str]:
    """Resolve a human ref to node IDs: exact node-ID first, then label match.

    Only nodes still present in ``node_set`` are returned, so a ref pointing at a
    node already merged away earlier in the pass resolves to nothing.
    """
    if ref in node_set:
        return [ref]
    return [n for n in label_index.get(ref.strip().casefold(), []) if n in node_set]


def apply_aliases(G, node_set: set, norm_to_id: dict, entries: list[dict]) -> list[dict]:
    """Apply alias entries against a partially-built graph.

    Mutates the graph in place for ``merge`` entries (removes the ``from`` node and
    routes its ID to the ``to`` node via ``norm_to_id``, so the existing edge loop
    re-points edges automatically). Returns a list of synthetic ``same_as`` edge
    dicts for the caller to feed through its normal edge-adding loop — that way
    alias edges inherit the same direction handling, source_file backfill, and
    dedup as every other edge.

    Called from ``build_from_json`` right after ``norm_to_id`` is built and before
    the edge loop runs.
    """
    if not entries:
        return []
    label_index = _build_label_index(G.nodes(data=True))
    edge_dicts: list[dict] = []
    for e in entries:
        from_ids = _resolve_ref(e["from"], node_set, label_index)
        to_ids = _resolve_ref(e["to"], node_set, label_index)
        if len(from_ids) != 1 or len(to_ids) != 1:
            _warn(
                f"alias {e['from']!r} -> {e['to']!r} skipped: resolved to "
                f"{len(from_ids)} and {len(to_ids)} node(s) (need exactly 1 each). "
                f"Use a more specific label or the exact node ID."
            )
            continue
        fid, tid = from_ids[0], to_ids[0]
        if fid == tid:
            continue  # self-alias is a no-op
        if e["mode"] == "merge":
            G.remove_node(fid)
            node_set.discard(fid)
            norm_to_id[normalize_id(fid)] = tid
            norm_to_id[fid] = tid
        else:  # same_as
            edge_dicts.append(
                {
                    "source": fid,
                    "target": tid,
                    "relation": "same_as",
                    "confidence": "EXTRACTED",
                    "confidence_score": 1.0,
                    "rationale": e["reason"],
                    "source_file": ALIAS_FILENAME,
                }
            )
    return edge_dicts


def append_alias(
    root: str | Path,
    frm: str,
    to: str,
    *,
    mode: str = "same_as",
    reason: str = "",
    contributor: str | None = None,
    date: str | None = None,
) -> dict:
    """Append (or update) an alias entry in ``<root>/.graphify_aliases.json``.

    Idempotent on the ``(from, to, mode)`` identity: re-asserting the same alias
    overwrites its reason/contributor/date rather than adding a duplicate. Creates
    the file with a ``version`` header when absent. Returns the stored entry.

    This is the write path shared by ``graphify save-result --alias`` and the web
    API's ``POST /api/aliases`` — it does NOT rebuild the graph; the alias takes
    effect on the next build.
    """
    if mode not in _VALID_MODES:
        raise ValueError(f"mode must be one of {_VALID_MODES}, got {mode!r}")
    path = default_aliases_path(root)
    if path is None:
        raise ValueError("root is required to write an alias file")

    data: dict = {"version": 1, "aliases": []}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
                data.setdefault("version", 1)
                data.setdefault("aliases", [])
        except (json.JSONDecodeError, OSError):
            pass  # start fresh rather than lose the new alias to a corrupt file

    entry = {
        "from": frm.strip(),
        "to": to.strip(),
        "mode": mode,
        "reason": reason,
    }
    if contributor:
        entry["contributor"] = contributor
    if date:
        entry["date"] = date

    aliases = [
        a
        for a in data.get("aliases", [])
        if not (
            isinstance(a, dict)
            and str(a.get("from", "")).strip() == entry["from"]
            and str(a.get("to", "")).strip() == entry["to"]
            and a.get("mode", "same_as") == mode
        )
    ]
    aliases.append(entry)
    aliases.sort(key=lambda a: (str(a.get("from", "")).casefold(), str(a.get("to", "")).casefold(), a.get("mode", "")))
    data["aliases"] = aliases
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return entry


def _warn(msg: str) -> None:
    print(f"[graphify] WARNING: {msg}", file=sys.stderr)
