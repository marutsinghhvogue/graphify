"""Flask JSON API over a built graph — query + human-in-the-loop review.

A thin HTTP layer for a separate frontend. It does NOT reimplement graph logic:
query/callers/callees reuse :mod:`graphify.serve`, review questions reuse
:func:`graphify.analyze.suggest_questions`, and alias writes reuse
:mod:`graphify.aliases`. This runs alongside (not instead of) the MCP server in
``serve.py``; MCP stays the agent-facing protocol, this is the browser/REST face.

Endpoints (all JSON):
  GET  /health
  GET  /api/query?q=...&mode=bfs|dfs&depth=3
  GET  /api/nodes?label=...
  GET  /api/callers?label=...
  GET  /api/callees?label=...
  GET  /api/review/uncertain-edges?confidence=INFERRED,AMBIGUOUS
  GET  /api/review/questions
  GET  /api/aliases
  POST /api/aliases     body: {"from","to","mode","reason","contributor"}

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
        "source_file": d.get("source_file"),
        "source_location": d.get("source_location"),
    }


def create_app(graph_path: str, root: str | None = None):
    """Build the Flask app. ``root`` is the repo root used for the alias file."""
    try:
        from flask import Flask, jsonify, request
    except ImportError as e:  # pragma: no cover - exercised via CLI message
        raise ImportError(
            'web API needs the "web" extra (flask). Run: pip install "graphifyy[web]"'
        ) from e

    app = Flask(__name__)
    app.config["GRAPHIFY_GRAPH_PATH"] = graph_path
    app.config["GRAPHIFY_ROOT"] = root or "."

    def _graph():
        return _get_graph(app.config["GRAPHIFY_GRAPH_PATH"])

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

    return app


def run_web(graph_path: str, *, host: str = "127.0.0.1", port: int = 8756, root: str | None = None) -> None:
    """Create and serve the app (blocking). Used by ``graphify serve-web``."""
    app = create_app(graph_path, root=root)
    app.run(host=host, port=port)
