"""Flask JSON API over a built graph — query + human-in-the-loop review.

A thin HTTP layer for a separate frontend. It does NOT reimplement graph logic:
query/callers/callees reuse :mod:`graphify.serve`, review questions reuse
:func:`graphify.analyze.suggest_questions`, and alias writes reuse
:mod:`graphify.aliases`. This runs alongside (not instead of) the MCP server in
``serve.py``; MCP stays the agent-facing protocol, this is the browser/REST face.

Legacy endpoints (text/JSON): /health, /api/query, /api/nodes, /api/callers,
/api/callees, /api/review/*, /api/aliases.

v1 REST API (structured JSON, built for an embeddable UI):
  GET  /api/v1/impact?label=...&depth=2     blast radius (affected symbols)
  GET  /api/v1/seeds?q=...&top=10           Stage 2: prose -> ranked seed symbols
  GET  /api/v1/subgraph?label=...&depth=1   nodes + edges for graph viz
  GET  /api/v1/stats                        node/edge/community/confidence counts
  GET  /api/v1/taint?vuln=...               taint findings (from `flows_to` edges)

Embedding controls (env): ``GRAPHIFY_API_KEY`` gates every endpoint but /health
(via ``X-API-Key`` or ``Authorization: Bearer``); ``GRAPHIFY_CORS_ORIGINS`` (comma
list or ``*``) sets the allowed origins for the UI's host app.

Flask is an optional dependency: ``pip install "graphifyy[web]"``.
"""
from __future__ import annotations

from pathlib import Path

from . import serve as _serve
from .aliases import append_alias, load_aliases

# (path, mtime) -> loaded graph. Reloads when graph.json changes on disk so a
# rebuild (e.g. after an alias is added) is reflected without a server restart.
_GRAPH_CACHE: dict[tuple[str, float], object] = {}


def _get_graph(graph_path: str):
    resolved = Path(graph_path).resolve()
    mtime = resolved.stat().st_mtime if resolved.exists() else 0.0
    key = (str(resolved), mtime)
    cached = _GRAPH_CACHE.get(key)
    if cached is None:
        cached = _serve._load_graph(str(resolved))
        _GRAPH_CACHE.clear()  # only keep the newest graph in memory
        _GRAPH_CACHE[key] = cached
    return cached


def _node_view(G, nid: str) -> dict:
    d = G.nodes[nid]
    return {
        "id": nid,
        "label": d.get("label", nid),
        "file_type": d.get("file_type"),
        "kind": d.get("kind"),
        "source_file": d.get("source_file"),
        "source_location": d.get("source_location"),
        "service": (d.get("metadata") or {}).get("service"),
    }


def _impact_view(G, hit) -> dict:
    """Serialize an affected.AffectedHit into a UI-friendly record."""
    view = _node_view(G, hit.node_id)
    view.update(depth=hit.depth, via_relation=hit.via_relation,
                confidence=hit.confidence or "EXTRACTED")
    return view


def _taint_findings(G, *, vuln: str = "") -> list[dict]:
    """Reconstruct taint findings from the graph's ``flows_to`` edges. Each such
    edge (emitted by ``graphify extract --taint``) carries the whole finding in
    its metadata, so this is a read — no re-analysis. Optionally filter by vuln."""
    out: list[dict] = []
    for u, v, d in G.edges(data=True):
        if d.get("relation") != "flows_to":
            continue
        meta = d.get("metadata") or {}
        if vuln and meta.get("vuln") != vuln:
            continue
        out.append({
            "vuln": meta.get("vuln"),
            "category": meta.get("category"),
            "confidence": meta.get("confidence") or d.get("confidence") or "INFERRED",
            "cross_function": bool(meta.get("cross_function")),
            "callee": meta.get("callee"),
            "source": meta.get("source") or {"stmt_id": u},
            "sink": meta.get("sink") or {"stmt_id": v},
            "path": meta.get("path") or [],
        })
    # Most severe / most-hops first is subjective; stable sort by vuln then source
    # line keeps the list deterministic for the UI.
    out.sort(key=lambda f: (f.get("vuln") or "", (f.get("source") or {}).get("line", 0)))
    return out


def _subgraph(G, seed: str, *, depth: int) -> dict:
    """Nodes + edges within ``depth`` hops of ``seed`` (both directions) — the
    render payload for a graph-viz UI."""
    seen = {seed}
    frontier = [seed]
    for _ in range(max(depth, 0)):
        nxt = []
        for n in frontier:
            for nb in list(G.successors(n)) + list(G.predecessors(n)):
                if nb not in seen:
                    seen.add(nb)
                    nxt.append(nb)
        frontier = nxt
    edges = [
        {"source": u, "target": v, "relation": d.get("relation"),
         "confidence": d.get("confidence"), "confidence_score": d.get("confidence_score")}
        for u, v, d in G.edges(data=True) if u in seen and v in seen
    ]
    return {"nodes": [_node_view(G, n) for n in seen], "edges": edges}


def create_app(graph_path: str, root: str | None = None):
    """Build the Flask app. ``root`` is the repo root used for the alias file."""
    try:
        from flask import Flask, jsonify, request
    except ImportError as e:  # pragma: no cover - exercised via CLI message
        raise ImportError(
            'web API needs the "web" extra (flask). Run: pip install "graphifyy[web]"'
        ) from e

    import os

    app = Flask(__name__)
    app.config["GRAPHIFY_GRAPH_PATH"] = graph_path
    app.config["GRAPHIFY_ROOT"] = root or "."
    # Embedding controls (env-overridable): an optional API key gates every
    # endpoint except /health, and CORS allows the UI's host origin(s).
    app.config.setdefault("GRAPHIFY_API_KEY", os.environ.get("GRAPHIFY_API_KEY", ""))
    app.config.setdefault("GRAPHIFY_CORS_ORIGINS",
                          os.environ.get("GRAPHIFY_CORS_ORIGINS", "*"))

    def _graph():
        return _get_graph(app.config["GRAPHIFY_GRAPH_PATH"])

    def _allowed_origin(origin: str) -> str:
        allowed = app.config["GRAPHIFY_CORS_ORIGINS"]
        if allowed == "*" or not origin:
            return "*"
        origins = {o.strip() for o in allowed.split(",") if o.strip()}
        return origin if origin in origins else ""

    @app.after_request
    def _cors(resp):
        origin = _allowed_origin(request.headers.get("Origin", ""))
        if origin:
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Vary"] = "Origin"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key, Authorization"
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return resp

    @app.before_request
    def _auth():
        if request.method == "OPTIONS" or request.path == "/health":
            return None
        key = app.config["GRAPHIFY_API_KEY"]
        if not key:
            return None  # auth disabled
        supplied = request.headers.get("X-API-Key") or ""
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = supplied or auth[7:].strip()
        if supplied != key:
            return jsonify({"error": "unauthorized"}), 401
        return None

    @app.get("/health")
    def health():
        return jsonify({"status": "ok", "graph": app.config["GRAPHIFY_GRAPH_PATH"]})

    @app.get("/api/query")
    def query():
        q = (request.args.get("q") or "").strip()
        if not q:
            return jsonify({"error": "missing required query param 'q'"}), 400
        mode = request.args.get("mode", "bfs")
        if mode not in ("bfs", "dfs"):
            return jsonify({"error": "mode must be 'bfs' or 'dfs'"}), 400
        try:
            depth = int(request.args.get("depth", 3))
        except ValueError:
            return jsonify({"error": "depth must be an integer"}), 400
        result = _serve._query_graph_text(_graph(), q, mode=mode, depth=depth)
        return jsonify({"question": q, "mode": mode, "depth": depth, "result": result})

    @app.get("/api/nodes")
    def nodes():
        label = (request.args.get("label") or "").strip()
        if not label:
            return jsonify({"error": "missing required query param 'label'"}), 400
        G = _graph()
        matches = _serve._find_node(G, label)
        return jsonify({"label": label, "matches": [_node_view(G, n) for n in matches]})

    @app.get("/api/callers")
    def callers():
        label = (request.args.get("label") or "").strip()
        if not label:
            return jsonify({"error": "missing required query param 'label'"}), 400
        return jsonify({"label": label, "result": _serve._format_call_edges(_graph(), label, incoming=True)})

    @app.get("/api/callees")
    def callees():
        label = (request.args.get("label") or "").strip()
        if not label:
            return jsonify({"error": "missing required query param 'label'"}), 400
        return jsonify({"label": label, "result": _serve._format_call_edges(_graph(), label, incoming=False)})

    @app.get("/api/review/uncertain-edges")
    def uncertain_edges():
        wanted = {
            c.strip().upper()
            for c in (request.args.get("confidence") or "INFERRED,AMBIGUOUS").split(",")
            if c.strip()
        }
        G = _graph()
        out = []
        for u, v, d in G.edges(data=True):
            conf = d.get("confidence", "EXTRACTED")
            if conf in wanted:
                out.append(
                    {
                        "source": _node_view(G, u),
                        "target": _node_view(G, v),
                        "relation": d.get("relation"),
                        "confidence": conf,
                        "confidence_score": d.get("confidence_score"),
                        "source_file": d.get("source_file"),
                    }
                )
        # Least-confident first, so a reviewer sees the riskiest links up top.
        out.sort(key=lambda e: (e.get("confidence_score") or 0.0))
        return jsonify({"confidence": sorted(wanted), "count": len(out), "edges": out})

    @app.get("/api/review/questions")
    def questions():
        from .analyze import suggest_questions

        G = _graph()
        communities = _serve._communities_from_graph(G)
        labels = {
            cid: G.nodes[members[0]].get("community_label", "")
            for cid, members in communities.items()
            if members
        }
        return jsonify({"questions": suggest_questions(G, communities, labels)})

    @app.get("/api/aliases")
    def get_aliases():
        return jsonify({"aliases": load_aliases(app.config["GRAPHIFY_ROOT"])})

    @app.post("/api/aliases")
    def post_alias():
        body = request.get_json(silent=True) or {}
        frm, to = str(body.get("from", "")).strip(), str(body.get("to", "")).strip()
        if not frm or not to:
            return jsonify({"error": "body must include non-empty 'from' and 'to'"}), 400
        mode = body.get("mode", "same_as")
        if mode not in ("same_as", "merge"):
            return jsonify({"error": "mode must be 'same_as' or 'merge'"}), 400
        from datetime import datetime, timezone

        entry = append_alias(
            app.config["GRAPHIFY_ROOT"],
            frm,
            to,
            mode=mode,
            reason=str(body.get("reason", "")),
            contributor=body.get("contributor"),
            date=datetime.now(timezone.utc).isoformat(),
        )
        # The alias is persisted but not yet in the graph; rebuild applies it.
        return jsonify({"alias": entry, "note": "recorded; run a graphify build to apply"}), 201

    # ── v1 REST API (structured JSON for an embeddable UI) ────────────────────

    @app.get("/api/v1/impact")
    def v1_impact():
        """Blast radius: symbols affected by changing `label` (reverse reachability,
        cross-boundary + confidence-tiered)."""
        from .affected import DEFAULT_AFFECTED_RELATIONS, affected_nodes, resolve_seed
        label = (request.args.get("label") or "").strip()
        if not label:
            return jsonify({"error": "missing required query param 'label'"}), 400
        try:
            depth = int(request.args.get("depth", 2))
        except ValueError:
            return jsonify({"error": "depth must be an integer"}), 400
        G = _graph()
        seed = resolve_seed(G, label)
        if seed is None:
            return jsonify({"error": f"no unique node for '{label}'"}), 404
        hits = affected_nodes(G, seed, relations=DEFAULT_AFFECTED_RELATIONS, depth=depth)
        return jsonify({"seed": _node_view(G, seed), "depth": depth,
                        "count": len(hits), "affected": [_impact_view(G, h) for h in hits]})

    @app.get("/api/v1/seeds")
    def v1_seeds():
        """Stage 2: prose requirement -> ranked code symbols it touches."""
        from .semantic_index import chunk_nodes, retrieve_seeds
        q = (request.args.get("q") or "").strip()
        if not q:
            return jsonify({"error": "missing required query param 'q'"}), 400
        try:
            top = int(request.args.get("top", 10))
        except ValueError:
            return jsonify({"error": "top must be an integer"}), 400
        G = _graph()
        nodes = [{"id": n, "label": d.get("label", n), "kind": d.get("kind"),
                  "source_file": d.get("source_file"), "metadata": d.get("metadata")}
                 for n, d in G.nodes(data=True)]
        hits = retrieve_seeds(q, chunk_nodes(nodes), top_n=top)
        return jsonify({"query": q, "seeds": [
            {"symbol_id": h.symbol_id, "name": h.name, "path": h.path, "kind": h.kind,
             "score": h.score, "matched": h.matched} for h in hits]})

    @app.get("/api/v1/subgraph")
    def v1_subgraph():
        """Nodes + edges around `label` for graph visualization."""
        from .affected import resolve_seed
        label = (request.args.get("label") or "").strip()
        if not label:
            return jsonify({"error": "missing required query param 'label'"}), 400
        try:
            depth = int(request.args.get("depth", 1))
        except ValueError:
            return jsonify({"error": "depth must be an integer"}), 400
        G = _graph()
        seed = resolve_seed(G, label)
        if seed is None:
            return jsonify({"error": f"no unique node for '{label}'"}), 404
        out = _subgraph(G, seed, depth=depth)
        return jsonify({"seed": seed, "depth": depth, **out})

    @app.get("/api/v1/stats")
    def v1_stats():
        G = _graph()
        confs: dict[str, int] = {}
        for _, _, d in G.edges(data=True):
            c = d.get("confidence", "EXTRACTED")
            confs[c] = confs.get(c, 0) + 1
        communities = _serve._communities_from_graph(G)
        return jsonify({"nodes": G.number_of_nodes(), "edges": G.number_of_edges(),
                        "communities": len(communities), "confidence": confs})

    @app.get("/api/v1/taint")
    def v1_taint():
        """Taint findings (source→sink flows) from the graph's `flows_to` edges.
        Present only when the graph was built with `graphify extract --taint`."""
        vuln = (request.args.get("vuln") or "").strip()
        G = _graph()
        findings = _taint_findings(G, vuln=vuln)
        by_vuln: dict[str, int] = {}
        for f in findings:
            by_vuln[f["vuln"]] = by_vuln.get(f["vuln"], 0) + 1
        return jsonify({"count": len(findings), "by_vuln": by_vuln, "findings": findings})

    return app


def run_web(graph_path: str, *, host: str = "127.0.0.1", port: int = 8756, root: str | None = None) -> None:
    """Create and serve the app (blocking). Used by ``graphify serve-web``."""
    app = create_app(graph_path, root=root)
    app.run(host=host, port=port)
