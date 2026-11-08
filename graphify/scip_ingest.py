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
import html
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


# tree-sitter relations SCIP resolves precisely and can contradict (name-matched,
# collision-prone). Structural edges (contains/imports/definitions) are ~100% and
# never demoted.
_DEMOTABLE_RELATIONS = frozenset({"calls", "references"})

# How far above SCIP's definition line a tree-sitter node may start and still be
# the same definition. Tree-sitter's Java/TS/Python decorated nodes start at the
# first annotation/decorator; SCIP points at the name. OpenAPI-style annotation
# blocks can run a couple of dozen lines.
_MAX_ANNOTATION_SPAN = 40


def _bare_name(label: str) -> str:
    """``.getAccount()`` / ``getAccount`` / ``org.x.HttpStatus`` → the simple name."""
    s = (label or "").strip().lstrip(".")
    s = s.split("(", 1)[0]
    return s.rsplit(".", 1)[-1].strip()


def _scip_match_name(node: dict) -> str:
    """The name a SCIP definition carries in source. A constructor's descriptor is
    ``Type#`<init>`().`` (label ``init``); tree-sitter labels it with the type name."""
    sym = (node.get("metadata") or {}).get("scip_symbol") or ""
    # stored metadata is HTML-escaped by sanitize_metadata (`<init>` → `&lt;init&gt;`)
    desc = html.unescape(_scip_descriptor(sym)) if sym else ""
    if "<init>" in desc:
        owner = desc.split("<init>", 1)[0].rstrip("`#")
        return _scip_label(owner)
    return _bare_name(node.get("label") or "")


def _scip_fqn(symbol: str) -> str:
    """Dotted fully-qualified name of a SCIP *type* symbol, the spelling tree-sitter
    gives an unresolved type stub: ``org/springframework/http/HttpStatus#`` →
    ``org.springframework.http.HttpStatus``; nested ``a/B#C#`` → ``a.B.C``. ``""`` for
    non-type symbols."""
    desc = _scip_descriptor(symbol)
    if not desc.endswith("#") or "(" in desc:
        return ""
    return desc.rstrip("#").replace("`", "").replace("/", ".").replace("#", ".")


def reconcile_scip(
    base: dict[str, Any],
    scip: dict[str, Any],
    *,
    line_offset: int = SCIP_LINE_OFFSET,
    strict: bool = False,
) -> dict[str, Any]:
    """Merge a SCIP extraction into a tree-sitter extraction on node identity.

    Join key is ``(path-suffix, definition-line + line_offset)``. When no base node
    starts on that line, the same-named base node in the same file that starts
    nearest above it (within ``_MAX_ANNOTATION_SPAN`` lines) is taken: tree-sitter's
    node for an annotated or decorated definition starts at the first annotation,
    SCIP's at the name. Constructors (SCIP ``<init>``) match by their type's name.
    For each SCIP definition node that lands on exactly one base node, the base
    node is canonical: it is stamped with ``metadata.scip_symbol`` and a top-level
    ``kind``, and the SCIP node's id maps to it. SCIP nodes that match nothing
    (defs tree-sitter never emitted, such as fields or interface methods) are kept
    as new nodes; ambiguous matches are NOT guessed — they are also kept as new.
    An external SCIP type folds onto tree-sitter's unresolved stub with the same
    dotted FQN when exactly one exists; other externals are kept as new.

    Edges are merged with precedence: a SCIP edge (type-resolved, ``context:
    "scip"``) supersedes a base edge on the same ``(source, target, relation)``;
    base edges SCIP did not reproduce (e.g. dynamic calls) are preserved.

    **Per-caller demote (recall-safety).** When SCIP resolved a caller's
    calls/references, its tree-sitter ``calls``/``references`` edges that SCIP did
    NOT confirm are the collision-prone ones (e.g. name-matched ``User.save`` vs
    ``Logger.save``). By default they are **demoted** — retagged
    ``confidence="AMBIGUOUS"`` with ``metadata.demoted_by="scip"`` — never
    deleted: precision consumers filter to EXTRACTED, recall consumers (blast
    radius) keep them. ``strict=True`` hard-drops them instead. Base edges from
    callers SCIP said nothing about are left untouched.

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
    # (basename, simple name) → base definitions, for the annotation-offset fallback
    by_file_name: dict[tuple[str, str], list[dict]] = defaultdict(list)
    # dotted FQN → unresolved base type stubs (no source file), for external folding
    stubs_by_fqn: dict[str, list[dict]] = defaultdict(list)
    # SCIP symbol → base node already carrying it (a module merged earlier defines
    # it): an external reference to that symbol is the same entity, across modules.
    defined_symbol: dict[str, str] = {}
    for n in base_nodes:
        bmeta = n.get("metadata") if isinstance(n.get("metadata"), dict) else {}
        if bmeta.get("scip_symbol") and not bmeta.get("scip_external"):
            defined_symbol.setdefault(bmeta["scip_symbol"], n["id"])
        if not n.get("source_file"):
            stubs_by_fqn[(n.get("label") or "").strip()].append(n)
        line = _parse_line(n.get("source_location"))
        if line is None:
            continue
        base_name = (n.get("source_file") or "").replace("\\", "/").rsplit("/", 1)[-1]
        if (n.get("label") or "") == base_name:
            continue  # file-container node, not a symbol definition
        by_name_line[(base_name, line)].append(n)
        by_file_name[(base_name, _bare_name(n.get("label") or ""))].append(n)

    id_map: dict[str, str] = {}
    scip_only: list[dict] = []
    matched = ambiguous = external = unmatched = 0
    matched_by_name = external_folded = 0

    for sn in scip_nodes:
        sid = sn["id"]
        meta = sn.get("metadata") or {}
        if meta.get("scip_external"):
            # Fold an external type onto tree-sitter's stub for the same FQN, so
            # both graphs' references to it share one node (and SCIP's edge can
            # supersede, rather than "contradict", the stub edge).
            stub = stubs_by_fqn.get(_scip_fqn(meta.get("scip_symbol") or ""), [])
            if meta.get("scip_symbol") in defined_symbol:
                id_map[sid] = defined_symbol[meta["scip_symbol"]]
                external_folded += 1
            elif len(stub) == 1:
                id_map[sid] = stub[0]["id"]
                external_folded += 1
            else:
                id_map[sid] = sid
                scip_only.append(sn)
            external += 1
            continue
        sfile = sn.get("source_file") or ""
        sbase = sfile.replace("\\", "/").rsplit("/", 1)[-1]
        sline = _parse_line(sn.get("source_location"))
        cand: list[dict] = []
        if sline is not None:
            key = (sbase, sline + line_offset)
            cand = [
                bn for bn in by_name_line.get(key, [])
                if _path_suffix_match(sfile, bn.get("source_file") or "")
            ]
            if not cand:
                # Tree-sitter's node for an annotated/decorated definition starts at
                # the first annotation; SCIP points at the name. Take the same-named
                # node in the same file that starts nearest above, within the span.
                target = sline + line_offset
                above = [
                    (ln, bn) for bn in by_file_name.get((sbase, _scip_match_name(sn)), [])
                    if _path_suffix_match(sfile, bn.get("source_file") or "")
                    and (ln := _parse_line(bn.get("source_location"))) is not None
                    and 0 < target - ln <= _MAX_ANNOTATION_SPAN
                ]
                if above:
                    nearest = max(ln for ln, _ in above)
                    cand = [bn for ln, bn in above if ln == nearest]
                    if len(cand) == 1:
                        matched_by_name += 1
        if len(cand) == 1:
            canon = cand[0]
            id_map[sid] = canon["id"]
            cmeta = canon.get("metadata")
            if not isinstance(cmeta, dict):
                cmeta = {}
            # Several SCIP definitions can land on one node: a record's type and its
            # implicit canonical constructor share the declaration line. Keep the
            # owner's symbol, not one of its members' (Type# over Type#`<init>`().).
            prev = cmeta.get("scip_symbol")
            if meta.get("scip_symbol") and not (
                prev and _scip_descriptor(meta["scip_symbol"]).startswith(_scip_descriptor(prev))
            ):
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

    # What SCIP resolved, per caller: {canonical caller: {simple name: {targets}}}.
    # A reference is credited to its innermost SCIP definition AND to every
    # definition that owns it in the symbol path (`pkg/X#clock.` is owned by
    # `pkg/X#`), because tree-sitter attributes a field's type reference to the
    # class, a node granularity SCIP keeps separate.
    scip_confirmed: set = set()
    label_of = {n["id"]: n.get("label") or "" for n in base_nodes}
    label_of.update({n["id"]: n.get("label") or "" for n in scip_nodes if n["id"] not in label_of})
    desc_of: dict[str, str] = {}
    for sn in scip_nodes:
        sym = (sn.get("metadata") or {}).get("scip_symbol")
        if sym and not (sn.get("metadata") or {}).get("scip_external"):
            desc_of[sn["id"]] = _scip_descriptor(sym)
    canon_of_desc = {d: id_map.get(sid, sid) for sid, d in desc_of.items()}

    def _owners(desc: str) -> list[str]:
        cuts = [i + 1 for i, ch in enumerate(desc[:-1]) if ch == "#"]
        cuts += [m.end() for m in re.finditer(r"\)\.", desc[:-2])]
        return [desc[:c] for c in cuts]

    resolved: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    remapped_scip: list[dict] = []
    for e in scip_edges:
        orig_src = e.get("source")
        e = dict(e)
        e["source"] = id_map.get(orig_src, orig_src)
        e["target"] = id_map.get(e.get("target"), e.get("target"))
        remapped_scip.append(e)
        if e.get("relation") not in _DEMOTABLE_RELATIONS:
            continue
        scip_confirmed.add((e.get("source"), e.get("target"), e.get("relation")))
        name = _bare_name(label_of.get(e.get("target"), ""))
        callers = {e["source"]} | {
            canon_of_desc[o] for o in _owners(desc_of.get(orig_src, "")) if o in canon_of_desc
        }
        for c in callers:
            resolved[c][name].add(e["target"])
    stub_ids = {n["id"] for n in base_nodes if not n.get("source_file")}
    implemented: dict[str, set] = defaultdict(set)   # implementation → what it implements
    for e in base_edges + remapped_scip:
        if e.get("relation") in ("implements", "overrides"):
            implemented[e.get("source")].add(e.get("target"))
    symbol_of: dict[str, str] = {}
    file_of: dict[str, str] = {}
    for n in base_nodes + scip_only:
        nmeta = n.get("metadata") if isinstance(n.get("metadata"), dict) else {}
        if nmeta.get("scip_symbol"):
            symbol_of[n["id"]] = _scip_descriptor(nmeta["scip_symbol"])
        if n.get("source_file") and not nmeta.get("scip_external"):
            file_of[n["id"]] = n["source_file"]
    for e in remapped_scip:
        k = (e.get("source"), e.get("target"), e.get("relation"))
        cur = merged.get(k)
        if cur is None or _prec(e) >= _prec(cur):
            merged[k] = e

    def _contradicted(e: dict, k: tuple) -> bool:
        """SCIP resolved the SAME name from this caller (or a definition it owns) to
        a different target: the User.save / Logger.save collision. A name SCIP says
        nothing about is unconfirmed, not contradicted (SCIP is not complete: no
        enclosing definition for class annotations, dynamic calls), so it is kept.
        A same-named target confirms a tree-sitter edge to an unresolved stub."""
        if e.get("context") == "scip" or e.get("relation") not in _DEMOTABLE_RELATIONS:
            return False
        if k in scip_confirmed:
            return False
        targets = resolved.get(e.get("source"), {}).get(_bare_name(label_of.get(e.get("target"), "")))
        if not targets or e.get("target") in targets:
            return False
        if e.get("target") in stub_ids:
            return False
        # Same entity under another node: `new X()` is the class to tree-sitter and
        # X#`<init>`() to SCIP (symbol prefix, either way round), or SCIP's copy of a
        # definition that did not join sits in the same file under the same name.
        # Dynamic dispatch: SCIP resolves a call to the static type's method, while
        # tree-sitter bound the implementation that runs. Both describe the call.
        if implemented.get(e.get("target"), set()) & targets:
            return False
        tsym = symbol_of.get(e.get("target"), "")
        tfile = file_of.get(e.get("target"), "")
        for t in targets:
            osym = symbol_of.get(t, "")
            if tsym and osym and (osym.startswith(tsym) or tsym.startswith(osym)):
                return False
            if tfile and file_of.get(t) == tfile:
                return False
        return True

    # Per-caller demote: demote (keep, retag AMBIGUOUS) a contradicted base
    # call/reference by default; drop it only under strict.
    demoted = dropped = 0
    out_edges: list[dict] = []
    for k, e in merged.items():
        contradicted = _contradicted(e, k)
        if not contradicted:
            out_edges.append(e)
            continue
        if strict:
            dropped += 1
            continue
        e = dict(e)
        e["confidence"] = "AMBIGUOUS"
        meta = e.get("metadata")
        e["metadata"] = {**meta, "demoted_by": "scip"} if isinstance(meta, dict) else {"demoted_by": "scip"}
        out_edges.append(e)
        demoted += 1

    return {
        "nodes": base_nodes + scip_only,
        "edges": out_edges,
        "reconciliation": {
            "scip_nodes": len(scip_nodes),
            "matched": matched,
            "matched_by_name": matched_by_name,
            "external_folded": external_folded,
            "scip_only_new": unmatched,
            "ambiguous_kept_new": ambiguous,
            "external": external,
            "demoted": demoted,
            "dropped_strict": dropped,
        },
    }


# ── estate (multi-module) merge ─────────────────────────────────────────────
#
# A monorepo or a checkout of several services holds one index.scip per build
# module. Each index's paths are relative to its module, and each module sees the
# others only as external symbols. merge_scip_estate prefixes every index with
# its module path, merges providers before their consumers (so an external
# reference to a symbol another module defines folds onto that definition before
# demotion is decided), and finally joins any externals still pointing at symbols
# defined elsewhere (dependency cycles).

_ESTATE_SKIP_DIRS = frozenset({".git", "node_modules", "target", "build", "dist", ".venv", "venv",
                               "__pycache__", ".gradle", ".idea"})


def discover_scip_indexes(root: str | Path) -> list[Path]:
    """Every ``index.scip`` under ``root``, skipping build output and VCS dirs."""
    import os

    root = Path(root)
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _ESTATE_SKIP_DIRS)
        if "index.scip" in filenames:
            found.append(Path(dirpath) / "index.scip")
    return found


def scip_index_symbols(path: str | Path) -> tuple[set[str], set[str]]:
    """``(defined, referenced)`` emittable symbols of one index: a cheap pass that
    decides merge order without building nodes."""
    from graphify import scip_pb2  # lazy: needs protobuf ('scip' extra)

    index = scip_pb2.Index()
    index.ParseFromString(Path(path).read_bytes())
    defined: set[str] = set()
    referenced: set[str] = set()
    for document in index.documents:
        for occ in document.occurrences:
            if not _scip_emittable(occ.symbol):
                continue
            (defined if occ.symbol_roles & _ROLE_DEFINITION else referenced).add(occ.symbol)
    return defined, referenced - defined


def order_scip_modules(symbols: dict[Path, tuple[set[str], set[str]]]) -> list[Path]:
    """Providers first: module A precedes B when B references symbols A defines.
    Dependency cycles fall back to path order (the final symbol join covers them)."""
    defined_by: dict[str, Path] = {}
    for path in sorted(symbols):
        for sym in symbols[path][0]:
            defined_by.setdefault(sym, path)
    deps = {
        path: {defined_by[s] for s in referenced if s in defined_by and defined_by[s] != path}
        for path, (_, referenced) in symbols.items()
    }
    ordered: list[Path] = []
    placed: set[Path] = set()
    while len(ordered) < len(deps):
        ready = sorted(p for p in deps if p not in placed and deps[p] <= placed)
        if not ready:  # cycle: take the earliest remaining module
            ready = [sorted(p for p in deps if p not in placed)[0]]
        for p in ready:
            ordered.append(p)
            placed.add(p)
    return ordered


def merge_scip_estate(
    base: dict[str, Any],
    root: str | Path,
    *,
    strict: bool = False,
    progress: Any = None,
) -> dict[str, Any]:
    """Merge every module's ``index.scip`` under ``root`` onto ``base``, whose
    ``source_file`` paths are relative to ``root`` (``graphify extract <root>``).

    Returns ``{"nodes", "edges", "reconciliation": totals, "modules": [...]}``;
    ``modules`` lists each module's prefix and its own reconciliation stats in merge
    order. ``progress(prefix, stats)`` is called after each module when given.
    """
    root = Path(root)
    indexes = discover_scip_indexes(root)
    order = order_scip_modules({p: scip_index_symbols(p) for p in indexes})
    current = {"nodes": list(base.get("nodes", [])), "edges": list(base.get("edges", base.get("links", [])))}
    totals: dict[str, int] = {}
    modules: list[dict[str, Any]] = []
    for idx in order:
        prefix = idx.parent.relative_to(root).as_posix()
        scip = ingest_scip_index(idx)
        if prefix and prefix != ".":
            for item in scip["nodes"] + scip["edges"]:
                if item.get("source_file"):
                    item["source_file"] = f"{prefix}/{item['source_file']}"
        out = reconcile_scip(current, scip, strict=strict)
        current = {"nodes": out["nodes"], "edges": out["edges"]}
        for k, v in out["reconciliation"].items():
            totals[k] = totals.get(k, 0) + v
        modules.append({"module": prefix or ".", **out["reconciliation"]})
        if progress:
            progress(prefix or ".", out["reconciliation"])

    # Externals still pointing at a symbol some module defines (cycles): join them.
    defined: dict[str, str] = {}
    for n in current["nodes"]:
        m = n.get("metadata") if isinstance(n.get("metadata"), dict) else {}
        if m.get("scip_symbol") and not m.get("scip_external"):
            defined.setdefault(m["scip_symbol"], n["id"])
    remap = {}
    for n in current["nodes"]:
        m = n.get("metadata") if isinstance(n.get("metadata"), dict) else {}
        if m.get("scip_external") and m.get("scip_symbol") in defined:
            remap[n["id"]] = defined[m["scip_symbol"]]
    nodes = [n for n in current["nodes"] if n["id"] not in remap]
    seen: set[tuple] = set()
    edges: list[dict] = []
    for e in current["edges"]:
        e = {**e, "source": remap.get(e.get("source"), e.get("source")),
             "target": remap.get(e.get("target"), e.get("target"))}
        key = (e["source"], e["target"], e.get("relation"))
        if e["source"] == e["target"] or key in seen:
            continue
        seen.add(key)
        edges.append(e)
    totals["externals_joined_after"] = len(remap)
    totals["modules"] = len(order)
    return {"nodes": nodes, "edges": edges, "reconciliation": totals, "modules": modules}
