"""contract_introspect.py — cross-service edge inference via a contract pivot.

SPIKE. Proves that a consumer's HTTP calls can be matched to producer endpoints
across services (and languages) by ``(method, normalized-path)`` against a
GLOBAL endpoint catalog — WITHOUT resolving hosts. Service identity is derived
from where the controller lives (the service directory), matching the estate's
"derive from controllers" constraint (no gateway/service-registry assumed).

Producer frameworks (decorator/annotation based → route→handler binds locally
in source): FastAPI (Python), NestJS (TypeScript), Spring (Java). Consumer
calls: fetch / axios (JS/TS), requests / httpx (Python).

Static extraction only (tier 3). Regexes target the common shapes and are
spike-scoped, not a bulletproof parser. Emits the standard graphify
``{nodes, edges}``; cross-service edges are tagged INFERRED (unique match) or
AMBIGUOUS (path collision across services — kept, never dropped, per the
demote-not-delete recall policy). Unmatched calls are treated as external and
reported in ``stats``, not silently dropped.

Known spike limitations (deferred): env-var/gateway host resolution; FastAPI
``include_router(prefix=...)`` composition; non-GET fetch options; method
overloads.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ── data ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Endpoint:
    method: str        # GET/POST/...
    path: str          # normalized, e.g. "/users/{}"
    raw_path: str
    service: str
    handler: str
    source_file: str
    line: int


@dataclass(frozen=True)
class Call:
    method: str
    path: str          # normalized
    raw_url: str
    caller: str
    service: str
    source_file: str
    line: int


# ── path normalization (the validated core) ─────────────────────────────────

_SCHEME_HOST = re.compile(r"^[a-zA-Z][\w+.-]*://[^/]+")


def normalize_path(raw: str) -> str:
    """Canonicalize a route/URL to ``/seg/seg`` with every path parameter
    collapsed to ``{}``. Strips scheme+host, query, and fragment.

    Handles param shapes across frameworks: ``{id}`` (FastAPI/Spring),
    ``:id`` (Nest/Express), ``<id>`` (Flask), and ``${id}`` (JS template).
    """
    s = raw.strip().strip('`"\'')
    s = _SCHEME_HOST.sub("", s)                 # drop scheme://host
    s = s.split("?", 1)[0].split("#", 1)[0]     # drop query/fragment
    out: list[str] = []
    for seg in s.split("/"):
        if not seg:
            continue
        if (
            (seg.startswith("{") and seg.endswith("}"))
            or seg.startswith(":")
            or (seg.startswith("<") and seg.endswith(">"))
            or "${" in seg
        ):
            out.append("{}")
        else:
            out.append(seg)
    return "/" + "/".join(out)


def _join(prefix: str, path: str) -> str:
    a = (prefix or "").strip("/")
    b = (path or "").strip("/")
    joined = "/".join(p for p in (a, b) if p)
    return "/" + joined


# ── enclosing-function tracking ──────────────────────────────────────────────

_PY_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(")
_TS_METHOD = re.compile(r"^\s*(?:public |private |protected )?(?:static )?(?:async )?(\w+)\s*\(")
_JAVA_METHOD = re.compile(r"^\s*(?:public|private|protected)\s+[\w<>\[\], ?]+\s+(\w+)\s*\(")
_NON_FN = {"if", "for", "while", "switch", "catch", "return", "constructor", "class", "new"}


def _defs_for(lang: str) -> re.Pattern:
    return {"python": _PY_DEF, "ts": _TS_METHOD, "java": _JAVA_METHOD}[lang]


def _function_index(lines: list[str], lang: str) -> list[tuple[int, str]]:
    """Ordered (line_no, name) of function/method definitions in a file."""
    pat = _defs_for(lang)
    out: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = pat.match(line)
        if m and m.group(1) not in _NON_FN:
            out.append((i, m.group(1)))
    return out


def _enclosing(defs: list[tuple[int, str]], line_no: int) -> str:
    """Name of the nearest def at or above ``line_no``."""
    name = "<module>"
    for dl, dn in defs:
        if dl <= line_no:
            name = dn
        else:
            break
    return name


def _next_def_after(defs: list[tuple[int, str]], line_no: int) -> str:
    for dl, dn in defs:
        if dl >= line_no:
            return dn
    return "<unknown>"


# ── producer extraction ──────────────────────────────────────────────────────

_FASTAPI = re.compile(r'@\w+\.(get|post|put|patch|delete|head|options)\(\s*["\']([^"\']+)["\']')
_NEST_CTRL = re.compile(r'@Controller\(\s*["\']?([^"\')]*)["\']?\s*\)')
_NEST_ROUTE = re.compile(r'@(Get|Post|Put|Patch|Delete)\(\s*["\']?([^"\')]*)["\']?\s*\)')
_SPRING_CLASS = re.compile(r'@RequestMapping\(\s*(?:value\s*=\s*|path\s*=\s*)?["\']([^"\']*)["\']')
_SPRING_METHOD = re.compile(
    r'@(Get|Post|Put|Patch|Delete)Mapping\(\s*(?:value\s*=\s*|path\s*=\s*)?["\']?([^"\')]*)["\']?'
)


def _detect_lang(path: Path) -> str | None:
    return {".py": "python", ".ts": "ts", ".java": "java"}.get(path.suffix)


def _extract_endpoints(path: Path, service: str, lines: list[str], lang: str) -> list[Endpoint]:
    defs = _function_index(lines, lang)
    eps: list[Endpoint] = []
    if lang == "python":
        for i, line in enumerate(lines):
            m = _FASTAPI.search(line)
            if m:
                eps.append(Endpoint(m.group(1).upper(), normalize_path(m.group(2)), m.group(2),
                                    service, _next_def_after(defs, i), str(path), i + 1))
    elif lang == "ts":
        prefix = ""
        cm = next((_NEST_CTRL.search(ln) for ln in lines if _NEST_CTRL.search(ln)), None)
        if cm:
            prefix = cm.group(1)
        for i, line in enumerate(lines):
            m = _NEST_ROUTE.search(line)
            if m:
                full = _join(prefix, m.group(2))
                eps.append(Endpoint(m.group(1).upper(), normalize_path(full), full,
                                    service, _next_def_after(defs, i), str(path), i + 1))
    elif lang == "java":
        cprefix = ""
        # class-level @RequestMapping precedes any method mapping
        for line in lines:
            if "@RequestMapping" in line and _SPRING_CLASS.search(line):
                cprefix = _SPRING_CLASS.search(line).group(1)
                break
        for i, line in enumerate(lines):
            m = _SPRING_METHOD.search(line)
            if m:
                full = _join(cprefix, m.group(2))
                eps.append(Endpoint(m.group(1).upper(), normalize_path(full), full,
                                    service, _next_def_after(defs, i), str(path), i + 1))
    return eps


# ── consumer extraction ──────────────────────────────────────────────────────

_FETCH = re.compile(r'fetch\(\s*[`"\']([^`"\']+)[`"\']')
_AXIOS = re.compile(r'axios\.(get|post|put|patch|delete)\(\s*[`"\']([^`"\']+)[`"\']')
_PY_HTTP = re.compile(
    r'(?:requests|httpx|client|session|self\.\w+)\.(get|post|put|patch|delete)\(\s*f?["\']([^"\']+)["\']'
)


def _extract_calls(path: Path, service: str, lines: list[str], lang: str) -> list[Call]:
    defs = _function_index(lines, lang)
    calls: list[Call] = []
    for i, line in enumerate(lines):
        if lang in ("ts", "python"):
            for m in _FETCH.finditer(line):
                calls.append(Call("GET", normalize_path(m.group(1)), m.group(1),
                                  _enclosing(defs, i), service, str(path), i + 1))
            for m in _AXIOS.finditer(line):
                calls.append(Call(m.group(1).upper(), normalize_path(m.group(2)), m.group(2),
                                  _enclosing(defs, i), service, str(path), i + 1))
        if lang == "python":
            for m in _PY_HTTP.finditer(line):
                calls.append(Call(m.group(1).upper(), normalize_path(m.group(2)), m.group(2),
                                  _enclosing(defs, i), service, str(path), i + 1))
    return calls


# ── orchestration ────────────────────────────────────────────────────────────

def _ep_id(e: Endpoint) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", e.path.lower()).strip("_")
    return f"svc_{e.service}_ep_{e.method.lower()}_{slug}"


def _fn_id(service: str, fn: str) -> str:
    return f"svc_{service}_fn_{fn}".lower()


def scan_service(service_dir: Path, service: str) -> tuple[list[Endpoint], list[Call]]:
    endpoints: list[Endpoint] = []
    calls: list[Call] = []
    for f in sorted(service_dir.rglob("*")):
        if not f.is_file():
            continue
        lang = _detect_lang(f)
        if lang is None:
            continue
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        endpoints += _extract_endpoints(f, service, lines, lang)
        calls += _extract_calls(f, service, lines, lang)
    return endpoints, calls


def cross_service_graph(root: str | Path) -> dict[str, Any]:
    """Scan every immediate subdirectory of ``root`` as a service; build a global
    endpoint catalog and match consumer calls against it to emit cross-service
    edges. Returns ``{"nodes", "edges", "stats"}`` in graphify schema."""
    root = Path(root)
    all_eps: list[Endpoint] = []
    all_calls: list[Call] = []
    for svc_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        eps, calls = scan_service(svc_dir, svc_dir.name)
        all_eps += eps
        all_calls += calls

    # global catalog keyed by (method, normalized path)
    catalog: dict[tuple[str, str], list[Endpoint]] = {}
    for e in all_eps:
        catalog.setdefault((e.method, e.path), []).append(e)

    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def _add_node(nid: str, label: str, kind: str, sf: str, line: int, **meta):
        nodes.setdefault(nid, {
            "id": nid, "label": label, "file_type": "code", "kind": kind,
            "source_file": sf, "source_location": f"L{line}",
            "metadata": {k: v for k, v in meta.items() if v is not None},
        })

    # producer nodes: endpoint + handler, handler --handles--> endpoint
    for e in all_eps:
        eid, hid = _ep_id(e), _fn_id(e.service, e.handler)
        _add_node(eid, f"{e.method} {e.path}", "route", e.source_file, e.line,
                  service=e.service, method=e.method, path=e.path)
        _add_node(hid, f"{e.handler}()", "function", e.source_file, e.line, service=e.service)
        edges.append({"source": hid, "target": eid, "relation": "handles",
                      "confidence": "EXTRACTED", "confidence_score": 1.0,
                      "source_file": e.source_file, "source_location": f"L{e.line}",
                      "context": "contract"})

    stats = {"endpoints": len(all_eps), "calls": len(all_calls),
             "matched_unique": 0, "matched_ambiguous": 0, "external": 0}

    for c in all_calls:
        cands = [e for e in catalog.get((c.method, c.path), []) if e.service != c.service]
        cid = _fn_id(c.service, c.caller)
        if not cands:
            stats["external"] += 1
            continue
        _add_node(cid, f"{c.caller}()", "function", c.source_file, c.line, service=c.service)
        ambiguous = len(cands) > 1
        stats["matched_ambiguous" if ambiguous else "matched_unique"] += 1
        for e in cands:
            hid = _fn_id(e.service, e.handler)
            edges.append({
                "source": cid, "target": hid, "relation": "calls_service",
                "confidence": "AMBIGUOUS" if ambiguous else "INFERRED",
                "confidence_score": 0.5 if ambiguous else 0.9,
                "source_file": c.source_file, "source_location": f"L{c.line}",
                "context": "cross_service",
                "metadata": {"method": c.method, "path": c.path,
                             "to_service": e.service, "via_endpoint": _ep_id(e),
                             "ambiguous": ambiguous, "raw_url": c.raw_url},
            })

    return {"nodes": list(nodes.values()), "edges": edges, "stats": stats}


# ── reconciliation with the tree-sitter AST graph ─────────────────────────────
#
# cross_service_graph emits handler/consumer function nodes under a self-contained
# ``svc_*`` id scheme, so cross-service edges form a subgraph disconnected from the
# tree-sitter AST nodes for the SAME handlers. reconcile_contract joins them on
# identity so blast radius can traverse from a real AST function node into the
# cross-service edges — the contract analog of scip_ingest.reconcile_scip.
#
# Join key = (path-suffix, normalized-function-name). SCIP joins on a definition
# line; contract handler nodes carry the route-decorator line, not the def line,
# so the name (with the file) is the reliable key.


def _norm_fn_name(label: str) -> str:
    """Canonical function name from a node label: drop a leading ``.`` (method
    qualifier) and a trailing ``()`` call marker, case-fold. ``get_user()`` →
    ``get_user``; ``.getOrder()`` → ``getorder``."""
    s = (label or "").strip()
    if s.startswith("."):
        s = s[1:]
    if s.endswith("()"):
        s = s[:-2]
    return s.strip().lower()


def _path_suffix_match(a: str, b: str) -> bool:
    """True if two paths share a trailing path-component suffix (either endswith
    the other), so same-named handlers in different services don't cross-match."""
    pa = [p for p in a.replace("\\", "/").split("/") if p]
    pb = [p for p in b.replace("\\", "/").split("/") if p]
    n = min(len(pa), len(pb))
    return n > 0 and pa[-n:] == pb[-n:]


def reconcile_contract(base: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    """Merge contract cross-service nodes/edges onto the tree-sitter (``base``)
    graph on ``(path-suffix, function-name)`` identity.

    Each contract *function* node (``kind='function'``) that lands on exactly one
    base AST node is folded into it: the AST node stays canonical and is stamped
    ``metadata.service``; the contract id maps to it and its edges are rewritten
    onto it. Endpoint (``kind='route'``) nodes have no AST twin and are kept as
    new; contract function nodes matching 0 or >1 base nodes are also kept —
    never name-guessed — mirroring ``reconcile_scip``.

    Returns ``{"nodes": <contract nodes to ADD>, "edges": <endpoint-rewritten
    edges>, "reconciliation": {...}}``. ``base`` node dicts are mutated in place
    (service stamped) but NOT returned — the caller already holds them.
    """
    from collections import defaultdict

    base_nodes = list(base.get("nodes", []))
    c_nodes = list(contract.get("nodes", []))
    c_edges = list(contract.get("edges", contract.get("links", [])))

    by_name: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for n in base_nodes:
        base_name = (n.get("source_file") or "").replace("\\", "/").rsplit("/", 1)[-1]
        label = n.get("label") or ""
        if label == base_name:
            continue  # file-container node, not a symbol definition
        by_name[(base_name, _norm_fn_name(label))].append(n)

    id_map: dict[str, str] = {}
    keep: list[dict] = []
    matched = ambiguous = unmatched = 0

    for cn in c_nodes:
        cid = cn["id"]
        if cn.get("kind") != "function":
            id_map[cid] = cid            # endpoint/route node — no AST twin
            keep.append(cn)
            continue
        cfile = cn.get("source_file") or ""
        key = (cfile.replace("\\", "/").rsplit("/", 1)[-1], _norm_fn_name(cn.get("label") or ""))
        cand = [
            bn for bn in by_name.get(key, [])
            if _path_suffix_match(cfile, bn.get("source_file") or "")
        ]
        if len(cand) == 1:
            canon = cand[0]
            id_map[cid] = canon["id"]
            meta = canon.get("metadata")
            if not isinstance(meta, dict):
                meta = {}
            svc = (cn.get("metadata") or {}).get("service")
            if svc and not meta.get("service"):
                meta["service"] = svc
            canon["metadata"] = meta
            matched += 1
        else:
            id_map[cid] = cid            # 0 or >1 — keep as new, never guess
            keep.append(cn)
            if cand:
                ambiguous += 1
            else:
                unmatched += 1

    seen: set[tuple] = set()
    edges_out: list[dict] = []
    for e in c_edges:
        e = dict(e)
        e["source"] = id_map.get(e.get("source"), e.get("source"))
        e["target"] = id_map.get(e.get("target"), e.get("target"))
        k = (e.get("source"), e.get("target"), e.get("relation"))
        if k in seen:
            continue
        seen.add(k)
        edges_out.append(e)

    return {
        "nodes": keep,
        "edges": edges_out,
        "reconciliation": {
            "contract_fn_nodes": sum(1 for n in c_nodes if n.get("kind") == "function"),
            "matched": matched,
            "kept_new": unmatched,
            "ambiguous_kept_new": ambiguous,
        },
    }
