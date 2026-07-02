"""scip_ingest.py — SCIP JSON ingestion (simplified subset).

Reads a simplified SCIP-style JSON structure and converts it into
Graphify nodes and edges. NOT a full SCIP protobuf implementation —
this is a skeleton that consumes the simplified shape described below.

Not wired to the CLI in this phase.

Entry point:
  ingest_scip_json(doc: object, source_file: str = "",
      language: str = "python") -> dict[str, Any]

  Returns {"nodes": [...], "edges": [...]} compatible with Graphify's
  extraction result format. All edges emitted are endpoint-safe — the
  function builds a symbol → node_id index in a first pass and either
  resolves relationship targets via that index or creates a stub
  external node so `build_from_json()` will keep the edge.

Supported (simplified) JSON shape:
  documents[]: { relative_path, language, symbols[] }
  symbols[]:   { symbol, kind, display_name, documentation[],
                 relationships[], occurrences[] }
  relationships[]: { symbol, is_reference, is_implementation,
                     is_type_definition, is_definition }
  occurrences[]: { range[], symbol, symbol_roles }

This shape diverges from the official SCIP protobuf (where occurrences
live on the document, not on each symbol). We consume the simplified
shape that LLM-generated SCIP-style JSON commonly produces. Future
cycles may add document-level occurrence support.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from graphify.security import sanitize_metadata


def ingest_scip_json(
    doc: object,
    source_file: str = "",
    language: str = "python",
) -> dict[str, Any]:
    """Convert a SCIP-style JSON document into Graphify nodes and edges.

    Parameter ``doc`` is ``object`` (not ``dict[str, Any]``) because SCIP
    documents come from external tools — we may be handed arbitrary
    deserialized JSON. The first check rejects anything that isn't a dict
    and returns the empty result.

    Two-pass design:
      1. Build a ``symbol_str → node_id`` index across every valid symbol
         in every valid document, plus collect per-symbol metadata.
      2. Emit nodes for every indexed symbol and then emit relationship
         edges. Relationship targets are resolved via the index when
         present; otherwise a stub ``scip_external`` node is added so
         edges never dangle.
    """
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    seen_node_ids: set[str] = set()
    seen_edges: set[tuple[str, str, str, str | None]] = set()

    if not isinstance(doc, dict):
        return {"nodes": nodes, "edges": edges}

    documents = doc.get("documents", [])
    if not isinstance(documents, list):
        return {"nodes": nodes, "edges": edges}

    # ---- pass 1: build symbol → node_id indices -----------------------------
    # Two indices so relationship resolution can be document-aware:
    #   per_doc:  (symbol_id, doc_path) → node_id  (same-document precedence)
    #   global:   symbol_id              → list[node_id] (cross-document fallback,
    #                                                     used only when unambiguous)
    per_doc_index: dict[tuple[str, str], str] = {}
    global_index: dict[str, list[str]] = {}
    # Per-symbol metadata kept for pass-2 node emission (avoids re-walking
    # the document tree).
    symbol_records: list[dict[str, Any]] = []
    for document in documents:
        if not isinstance(document, dict):
            continue
        doc_path = _coerce_str(document.get("relative_path"), source_file)
        doc_language = _coerce_str(document.get("language"), language)
        symbols = document.get("symbols", [])
        if not isinstance(symbols, list):
            continue
        for symbol in symbols:
            if not isinstance(symbol, dict):
                continue
            symbol_id = _coerce_str(symbol.get("symbol"), "")
            if not symbol_id:
                continue
            node_id = _make_scip_node_id(symbol_id, doc_path)
            per_doc_index.setdefault((symbol_id, doc_path), node_id)
            # Dedupe node_ids in the global index — duplicate symbol records
            # within the SAME document produce identical node_ids, and we
            # don't want them to look like cross-document ambiguity.
            candidates = global_index.setdefault(symbol_id, [])
            if node_id not in candidates:
                candidates.append(node_id)
            symbol_records.append(
                {
                    "node_id": node_id,
                    "symbol_id": symbol_id,
                    "doc_path": doc_path,
                    "language": doc_language,
                    "raw": symbol,
                }
            )

    # ---- pass 2: emit nodes + relationship edges -----------------------------
    for record in symbol_records:
        _emit_symbol_node(record, nodes, seen_node_ids)
        _emit_relationships(
            record,
            per_doc_index,
            global_index,
            nodes,
            edges,
            seen_node_ids,
            seen_edges,
        )

    return {"nodes": nodes, "edges": edges}


def _emit_symbol_node(
    record: dict[str, Any],
    nodes: list[dict[str, Any]],
    seen_node_ids: set[str],
) -> None:
    """Append the canonical node for a SCIP symbol record."""
    node_id = record["node_id"]
    if node_id in seen_node_ids:
        return
    raw = record["raw"]
    symbol_id = record["symbol_id"]
    doc_path = record["doc_path"]
    kind = _coerce_str(raw.get("kind"), "unknown")
    display_name = _coerce_str(raw.get("display_name"), "")
    documentation = raw.get("documentation", [])
    description = ""
    if isinstance(documentation, list) and documentation:
        first = documentation[0]
        if isinstance(first, str):
            description = first
    occurrences = raw.get("occurrences", [])
    sourceline = _first_occurrence_line(occurrences)
    suffix = symbol_id.split("#")[-1] if "#" in symbol_id else symbol_id
    label = display_name or suffix or symbol_id
    seen_node_ids.add(node_id)  # label uses display_name or suffix (never empty for valid symbols)
    nodes.append(
        {
            "id": node_id,
            "label": label,
            "file_type": _scip_kind_to_file_type(kind),
            "source_file": doc_path,
            "source_location": f"L{sourceline}" if sourceline else "",
            "metadata": sanitize_metadata(_build_scip_metadata(symbol_id, kind, description)),
        }
    )


def _emit_relationships(
    record: dict[str, Any],
    per_doc_index: dict[tuple[str, str], str],
    global_index: dict[str, list[str]],
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    seen_node_ids: set[str],
    seen_edges: set[tuple[str, str, str, str | None]],
) -> None:
    """Append edges (and stub nodes when needed) for a symbol's relationships.

    Relationship target resolution order:
      1. Same-document `(target_symbol, doc_path)` — duplicate local symbol
         names across files route to THIS file's symbol, not another's.
      2. Unique cross-document match — when the symbol exists in exactly
         one document and that document is different from the source.
      3. Stub external node — for symbols not declared in any document
         OR ambiguous duplicates across multiple documents (refusing to
         guess silently).
    """
    raw = record["raw"]
    source_node_id = record["node_id"]
    doc_path = record["doc_path"]
    occurrences = raw.get("occurrences", [])
    sourceline = _first_occurrence_line(occurrences)
    relationships = raw.get("relationships")
    if not isinstance(relationships, list):
        return
    for rel in relationships:
        if not isinstance(rel, dict):
            continue
        target_symbol = _coerce_str(rel.get("symbol"), "")
        if not target_symbol:
            continue
        target_node_id = _resolve_relationship_target(
            target_symbol,
            doc_path,
            per_doc_index,
            global_index,
        )
        if target_node_id is None:
            # External relationship target: emit a stub node so the edge
            # is never dangling. The stub uses the source document's path
            # as its host context.
            target_node_id = _make_scip_node_id(target_symbol, doc_path)
            if target_node_id not in seen_node_ids:
                seen_node_ids.add(target_node_id)
                suffix = target_symbol.split("#")[-1] if "#" in target_symbol else target_symbol
                nodes.append(
                    {
                        "id": target_node_id,
                        "label": suffix or target_symbol,
                        "file_type": "code",
                        "source_file": doc_path,
                        "source_location": "",
                        "metadata": sanitize_metadata(
                            _build_scip_metadata(target_symbol, "external", "")
                        ),
                    }
                )
        relation = _scip_relation_for(rel)
        source_location = f"L{sourceline}" if sourceline else ""
        key = (source_node_id, target_node_id, relation, source_location)
        if key in seen_edges:
            continue
        seen_edges.add(key)
        edges.append(
            {
                "source": source_node_id,
                "target": target_node_id,
                "relation": relation,
                "confidence": "EXTRACTED",
                "confidence_score": 1.0,
                "source_file": doc_path,
                "source_location": source_location,
                "weight": 1.0,
                "context": "scip",
                "metadata": sanitize_metadata({"scip_relationship": rel}),
            }
        )


def _resolve_relationship_target(
    target_symbol: str,
    source_doc_path: str,
    per_doc_index: dict[tuple[str, str], str],
    global_index: dict[str, list[str]],
) -> str | None:
    """Resolve a SCIP relationship target to an emitted node id, or None.

    Resolution order:
      1. Same-document match — `(target_symbol, source_doc_path)`.
      2. Unique cross-document match — exactly one node id in the global
         index for this symbol AND it isn't the same document we already
         tried.
      3. None — symbol is either absent globally OR ambiguous (defined in
         multiple documents). The caller emits a stub external node.
    """
    same_doc = per_doc_index.get((target_symbol, source_doc_path))
    if same_doc is not None:
        return same_doc
    candidates = global_index.get(target_symbol, [])
    if len(candidates) == 1:
        return candidates[0]
    return None


def _is_true(value: object) -> bool:
    """Return True only when value is exactly the boolean True.

    Used for SCIP relationship flags. Truthy strings like ``"false"`` are
    common in untrusted external JSON and must NOT count as a set flag.
    """
    return value is True


def _scip_relation_for(rel: dict[str, Any]) -> str:
    """Pick the Graphify relation tag for a SCIP relationship dict.

    Flags are accepted only when the value is exactly ``True`` — protects
    against truthy-but-misleading values like ``"false"`` in external JSON.
    """
    if _is_true(rel.get("is_implementation")):
        return "scip_impl"
    if _is_true(rel.get("is_type_definition")):
        return "scip_typed"
    if _is_true(rel.get("is_definition")):
        return "scip_def"
    return "scip_ref"


def _first_occurrence_line(occurrences: object) -> int:
    """Read the 1-based line number from the first occurrence range, defensively.

    Note: ``bool`` is a subclass of ``int`` in Python — ``isinstance(True, int)``
    is True. We explicitly exclude booleans so a malformed ``range: [True, …]``
    cannot produce ``source_location = "LTrue"``.
    """
    if not isinstance(occurrences, list) or not occurrences:
        return 0
    first = occurrences[0]
    if not isinstance(first, dict):
        return 0
    rng = first.get("range", [])
    if not isinstance(rng, list) or len(rng) < 1:
        return 0
    line = rng[0]
    if isinstance(line, bool) or not isinstance(line, int) or line < 0:
        return 0
    return line


def _coerce_str(value: object, default: str) -> str:
    """Return ``value`` if it is a string, else the ``default`` (also a string)."""
    if isinstance(value, str):
        return value
    if isinstance(default, str):
        return default
    return ""


def _make_scip_node_id(symbol: str, source_file: str) -> str:
    """Derive a stable Graphify node ID from a SCIP symbol identifier.

    Uses SHA-1 truncated to 12 hex chars (48 bits). This is an identifier,
    not a security boundary — collision risk is acceptable at this scale
    given the per-document scoping prefix.
    """
    raw = f"{source_file}:{symbol}"
    h = hashlib.sha1(raw.encode(), usedforsecurity=False).hexdigest()[:12]
    parts = symbol.split("#")
    suffix = parts[-1] if parts else symbol
    suffix = re.sub(r"[^a-zA-Z0-9_]", "_", suffix).strip("_").lower()
    if suffix:
        return f"scip_{suffix}_{h}"
    return f"scip_{h}"


def _scip_kind_to_file_type(kind: str) -> str:
    """Map SCIP symbol kind to a Graphify file_type."""
    # All SCIP symbols are code entities (functions, methods, classes, …);
    # the `kind` is preserved in metadata for downstream consumers.
    _ = kind  # acknowledged but not currently used for file_type routing
    return "code"


def _build_scip_metadata(symbol_id: str, kind: str, description: str) -> dict[str, str]:
    """Build metadata for a SCIP node."""
    meta: dict[str, str] = {
        "scip_symbol": symbol_id,
        "scip_kind": kind,
    }
    if description:
        meta["scip_description"] = description
    return meta


# ── binary SCIP index reader (real protobuf) ────────────────────────────────
#
# ingest_scip_json() above consumes a simplified, LLM-shaped JSON. The reader
# below consumes a REAL ``index.scip`` (protobuf) emitted by scip-python /
# scip-typescript / rust-analyzer etc., and reconstructs type-resolved call and
# implements edges — the precision the heuristic tree-sitter path only
# approximates. Requires the 'scip' extra (protobuf); the import is lazy so
# this module stays importable without it.

_ROLE_DEFINITION = 0x1


def _scip_descriptor(symbol: str) -> str:
    """The stable SCIP descriptor: the tail after the four metadata fields
    ``<scheme> <manager> <package> <version>``. Locals pass through."""
    if symbol.startswith("local "):
        return symbol
    parts = symbol.split(" ")
    return " ".join(parts[4:]) if len(parts) > 4 else symbol


def _scip_label(descriptor: str) -> str:
    """Human label = the last identifier in the descriptor."""
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", descriptor)
    return tokens[-1] if tokens else descriptor


def _scip_kind_name(kind_value: int) -> str:
    """Lowercased SCIP ``SymbolInformation.Kind`` name (e.g. ``class``,
    ``method``, ``function``, ``interface``, ``typealias``, ``variable``).

    Returns "" for the unspecified/unknown kind so callers can skip tagging.
    This is the queryable distinction tree-sitter discards after parsing.
    """
    from graphify import scip_pb2  # lazy: needs protobuf ('scip' extra)
    try:
        name = scip_pb2.SymbolInformation.Kind.Name(kind_value)
    except (ValueError, KeyError):
        return ""
    return "" if name == "UnspecifiedKind" else name.lower()


def _scip_kind_from_descriptor(descriptor: str) -> str:
    """Coarse kind derived from the SCIP descriptor suffix — a reliable fallback
    when ``SymbolInformation.kind`` is Unspecified (scip-python / scip-typescript
    leave it 0).

    Suffix grammar: ``Type#`` = type (class/interface — indistinguishable here),
    ``Type#m().`` = method, ``ns/f().`` = function (no enclosing type), ``x.`` =
    term (variable/field/constant). ``SymbolInformation.kind`` still wins when set,
    so class-vs-interface precision is preserved wherever the indexer provides it.
    """
    if descriptor.endswith("()."):
        return "method" if "#" in descriptor[:-3] else "function"
    if descriptor.endswith("#"):
        return "type"
    if descriptor.endswith("."):
        return "term"
    return ""


def _scip_emittable(symbol: str) -> bool:
    """A real graph entity — not a local, parameter, or meta (``:``) symbol."""
    if not symbol or symbol.startswith("local "):
        return False
    desc = _scip_descriptor(symbol)
    if not desc or desc.endswith(":"):                      # meta, e.g. `m`/__init__:
        return False
    if desc.endswith(")") and not desc.endswith(")."):      # parameter, e.g. f().(x)
        return False
    return True


def _scip_range(values) -> tuple[int, int, int, int] | None:
    """Normalize a SCIP range: [l, c1, c2] (single line) or [l1, c1, l2, c2]."""
    vals = list(values)
    if len(vals) == 3:
        return (vals[0], vals[1], vals[0], vals[2])
    if len(vals) == 4:
        return (vals[0], vals[1], vals[2], vals[3])
    return None


def _scip_contains(outer: tuple, inner: tuple) -> bool:
    return (inner[0], inner[1]) >= (outer[0], outer[1]) and \
           (inner[2], inner[3]) <= (outer[2], outer[3])


def ingest_scip_index(path: str | Path) -> dict[str, Any]:
    """Read a binary ``index.scip`` and convert it into Graphify nodes/edges.

    Produces type-resolved, ``EXTRACTED`` edges:

      * **calls** — each non-definition occurrence of a callable symbol is
        attributed to the *innermost* definition whose ``enclosing_range``
        contains it (the caller). Exact: two same-named methods on different
        classes never cross-link.
      * **references** — same, for type/class symbols (instantiation, imports).
      * **implements** / **has_type** — from ``SymbolInformation.relationships``
        (``is_implementation`` / ``is_type_definition``).

    Requires the 'scip' extra (protobuf). Output is endpoint-safe and passes
    ``validate.validate_extraction``.
    """
    from graphify import scip_pb2  # lazy: needs protobuf ('scip' extra)

    index = scip_pb2.Index()
    index.ParseFromString(Path(path).read_bytes())

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    seen_nodes: set[str] = set()
    seen_edges: set[tuple[str, str, str]] = set()
    sym_to_node: dict[str, str] = {}

    # SymbolInformation carries the symbol KIND (Class/Method/Function/Interface
    # /TypeAlias/Variable/...); occurrences don't. Build a symbol -> kind map so
    # definition nodes can be tagged with a queryable kind — the distinction
    # tree-sitter drops after parsing, and what the merge stamps onto canonical
    # nodes.
    sym_kind: dict[str, str] = {}
    for document in index.documents:
        for si in document.symbols:
            k = _scip_kind_name(si.kind)
            if k:
                sym_kind[si.symbol] = k

    # pass 1: a node per emittable definition (Definition-role occurrence).
    for document in index.documents:
        for occ in document.occurrences:
            if not (occ.symbol_roles & _ROLE_DEFINITION):
                continue
            if not _scip_emittable(occ.symbol) or occ.symbol in sym_to_node:
                continue
            nr = _scip_range(occ.range)
            node_id = _make_scip_node_id(occ.symbol, document.relative_path)
            sym_to_node[occ.symbol] = node_id
            if node_id in seen_nodes:
                continue
            seen_nodes.add(node_id)
            meta = {"scip_symbol": occ.symbol}
            kind = sym_kind.get(occ.symbol) or _scip_kind_from_descriptor(
                _scip_descriptor(occ.symbol)
            )
            if kind:
                meta["scip_kind"] = kind
            nodes.append(
                {
                    "id": node_id,
                    "label": _scip_label(_scip_descriptor(occ.symbol)),
                    "file_type": "code",
                    "source_file": document.relative_path,
                    "source_location": f"L{nr[0]}" if nr else "",
                    "metadata": sanitize_metadata(meta),
                }
            )

    def _resolve_target(symbol: str, host_doc: str) -> str | None:
        if not _scip_emittable(symbol):
            return None
        node_id = sym_to_node.get(symbol)
        if node_id is not None:
            return node_id
        # External symbol (defined outside this index): stub a node so the edge
        # never dangles (mirrors build_from_json's expectations).
        node_id = _make_scip_node_id(symbol, host_doc)
        sym_to_node[symbol] = node_id
        if node_id not in seen_nodes:
            seen_nodes.add(node_id)
            nodes.append(
                {
                    "id": node_id,
                    "label": _scip_label(_scip_descriptor(symbol)),
                    "file_type": "code",
                    "source_file": host_doc,
                    "source_location": "",
                    "metadata": sanitize_metadata(
                        {"scip_symbol": symbol, "scip_external": "true"}
                    ),
                }
            )
        return node_id

    def _add_edge(src: str, tgt: str, relation: str, doc: str, line: int) -> None:
        key = (src, tgt, relation)
        if src == tgt or key in seen_edges:
            return
        seen_edges.add(key)
        edges.append(
            {
                "source": src,
                "target": tgt,
                "relation": relation,
                "confidence": "EXTRACTED",
                "confidence_score": 1.0,
                "source_file": doc,
                "source_location": f"L{line}" if line else "",
                "weight": 1.0,
                "context": "scip",
            }
        )

    # pass 2a: call / reference edges via occurrence containment.
    for document in index.documents:
        scopes = [
            (occ.symbol, enc)
            for occ in document.occurrences
            if (enc := _scip_range(occ.enclosing_range)) is not None
            and _scip_emittable(occ.symbol)
        ]
        for occ in document.occurrences:
            if occ.symbol_roles & _ROLE_DEFINITION or not _scip_emittable(occ.symbol):
                continue
            ref = _scip_range(occ.range)
            if ref is None:
                continue
            desc = _scip_descriptor(occ.symbol)
            if desc.endswith(")."):
                relation = "calls"
            elif desc.endswith("#"):
                relation = "references"
            else:
                continue
            caller = None
            for sym, enc in scopes:
                if sym == occ.symbol or not _scip_contains(enc, ref):
                    continue
                if caller is None or (enc[2] - enc[0]) < (caller[1][2] - caller[1][0]):
                    caller = (sym, enc)
            if caller is None:
                continue
            src = sym_to_node.get(caller[0])
            tgt = _resolve_target(occ.symbol, document.relative_path)
            if src is None or tgt is None:
                continue
            _add_edge(src, tgt, relation, document.relative_path, ref[0])

    # pass 2b: implements / type edges from symbol relationships.
    for document in index.documents:
        for sym_info in document.symbols:
            if not _scip_emittable(sym_info.symbol):
                continue
            src = _resolve_target(sym_info.symbol, document.relative_path)
            if src is None:
                continue
            for rel in sym_info.relationships:
                if rel.is_implementation:
                    relation = "implements"
                elif rel.is_type_definition:
                    relation = "has_type"
                else:
                    continue
                tgt = _resolve_target(rel.symbol, document.relative_path)
                if tgt is None:
                    continue
                _add_edge(src, tgt, relation, document.relative_path, 0)

    return {"nodes": nodes, "edges": edges}


# ── merge-on-identity reconciliation ────────────────────────────────────────
#
# Join a precise SCIP extraction onto a tree-sitter ("base") extraction so both
# describe the SAME nodes. Validated join key: (file, definition-line), where the
# SCIP line is 0-based and tree-sitter's is 1-based — hence SCIP_LINE_OFFSET = 1.
# The tree-sitter node stays canonical (it anchors containment / communities /
# docs); SCIP contributes precise edges + a queryable `kind`, and its edge
# endpoints are rewritten onto the canonical ids.

SCIP_LINE_OFFSET = 1


def _path_suffix_match(a: str, b: str) -> bool:
    """True if two paths share a trailing path-component suffix (either endswith
    the other), so ``pkg/models.py`` matches ``py_sample/pkg/models.py`` without
    the false positives a bare-basename compare would allow across directories."""
    pa = [p for p in a.replace("\\", "/").split("/") if p]
    pb = [p for p in b.replace("\\", "/").split("/") if p]
    n = min(len(pa), len(pb))
    return n > 0 and pa[-n:] == pb[-n:]


def _parse_line(source_location: object) -> int | None:
    if isinstance(source_location, str) and source_location.startswith("L"):
        try:
            return int(source_location[1:])
        except ValueError:
            return None
    return None


def reconcile_scip(
    base: dict[str, Any],
    scip: dict[str, Any],
    *,
    line_offset: int = SCIP_LINE_OFFSET,
) -> dict[str, Any]:
    """Merge a SCIP extraction into a tree-sitter extraction on node identity.

    Join key is ``(path-suffix, definition-line + line_offset)``. For each SCIP
    definition node that lands on exactly one base node, the base node is
    canonical: it is stamped with ``metadata.scip_symbol`` and a top-level
    ``kind``, and the SCIP node's id maps to it. SCIP nodes that match nothing
    (external symbols, or defs tree-sitter never emitted such as interface
    methods) are kept as new nodes; ambiguous matches are NOT guessed — they are
    also kept as new, never linked by name.

    Edges are merged with precedence: a SCIP edge (type-resolved, ``context:
    "scip"``) supersedes a base edge on the same ``(source, target, relation)``;
    base edges SCIP did not reproduce (e.g. dynamic calls) are preserved.

    Returns ``{"nodes", "edges", "reconciliation": {...stats}}``. ``base`` node
    dicts are mutated in place (kind/metadata stamped).
    """
    from collections import defaultdict

    base_nodes = list(base.get("nodes", []))
    base_edges = list(base.get("edges", base.get("links", [])))
    scip_nodes = list(scip.get("nodes", []))
    scip_edges = list(scip.get("edges", scip.get("links", [])))

    # Index base nodes by (basename, line); disambiguate collisions by path suffix.
    # Skip the file-container node itself (its label is the file basename and it
    # sits on the same line as the first definition — it must not shadow a real
    # symbol match).
    by_name_line: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for n in base_nodes:
        line = _parse_line(n.get("source_location"))
        if line is None:
            continue
        base_name = (n.get("source_file") or "").replace("\\", "/").rsplit("/", 1)[-1]
        if (n.get("label") or "") == base_name:
            continue  # file-container node, not a symbol definition
        by_name_line[(base_name, line)].append(n)

    id_map: dict[str, str] = {}
    scip_only: list[dict] = []
    matched = ambiguous = external = unmatched = 0

    for sn in scip_nodes:
        sid = sn["id"]
        meta = sn.get("metadata") or {}
        if meta.get("scip_external"):
            id_map[sid] = sid
            scip_only.append(sn)
            external += 1
            continue
        sfile = sn.get("source_file") or ""
        sline = _parse_line(sn.get("source_location"))
        cand: list[dict] = []
        if sline is not None:
            key = (sfile.replace("\\", "/").rsplit("/", 1)[-1], sline + line_offset)
            cand = [
                bn for bn in by_name_line.get(key, [])
                if _path_suffix_match(sfile, bn.get("source_file") or "")
            ]
        if len(cand) == 1:
            canon = cand[0]
            id_map[sid] = canon["id"]
            cmeta = canon.get("metadata")
            if not isinstance(cmeta, dict):
                cmeta = {}
            if meta.get("scip_symbol"):
                cmeta["scip_symbol"] = meta["scip_symbol"]
            canon["metadata"] = cmeta
            if meta.get("scip_kind") and not canon.get("kind"):
                canon["kind"] = meta["scip_kind"]
            matched += 1
        else:
            # 0 matches (SCIP-only def) or >1 (ambiguous) — keep as a new node,
            # never guess a name-based link.
            id_map[sid] = sid
            if meta.get("scip_kind") and not sn.get("kind"):
                sn["kind"] = meta["scip_kind"]
            scip_only.append(sn)
            if cand:
                ambiguous += 1
            else:
                unmatched += 1

    # Merge edges. SCIP (context="scip") supersedes base on identical endpoints.
    def _prec(e: dict) -> int:
        return 2 if e.get("context") == "scip" else 1

    merged: dict[tuple, dict] = {}
    for e in base_edges:
        merged[(e.get("source"), e.get("target"), e.get("relation"))] = e
    for e in scip_edges:
        e = dict(e)
        e["source"] = id_map.get(e.get("source"), e.get("source"))
        e["target"] = id_map.get(e.get("target"), e.get("target"))
        k = (e.get("source"), e.get("target"), e.get("relation"))
        cur = merged.get(k)
        if cur is None or _prec(e) >= _prec(cur):
            merged[k] = e

    return {
        "nodes": base_nodes + scip_only,
        "edges": list(merged.values()),
        "reconciliation": {
            "scip_nodes": len(scip_nodes),
            "matched": matched,
            "scip_only_new": unmatched,
            "ambiguous_kept_new": ambiguous,
            "external": external,
        },
    }
