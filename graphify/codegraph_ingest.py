"""codegraph_ingest.py — enhance a CodeGraph index with our multi-repo layer.

CodeGraph (github.com/colbymchenry/codegraph) is a fast Rust/tree-sitter indexer
that writes a per-project SQLite graph (``.codegraph/codegraph.db``) — but it stops
at the repo boundary: cross-service HTTP calls land in its ``unresolved_refs`` table,
so a set of repos comes out as N disconnected graphs. This adapter reads that SQLite
into graphify's ``{nodes, edges}`` shape so our cross-service layer
(``contract_introspect`` + ``reconcile_contract``) can stitch the repos together and
``plan_change`` can compute a real cross-service blast radius on top of CodeGraph's
fast within-repo graph.

Zero coupling to CodeGraph internals or a running process: we read the SQLite file it
already produced (like we read our own ``graph.json``). Each repo's db is tagged with
``metadata.repo`` and its node ids are namespaced, so a multi-repo estate merges
without collisions and every node is attributable to a service.

Mapping (CodeGraph → graphify):
  nodes(id, kind, name, qualified_name, file_path, start_line, signature, docstring,
        language)  →  {id, label=name, kind, source_file, source_location, metadata}
  edges(source, target, kind, metadata, line)  →  {source, target, relation=kind, …}
CodeGraph edge kinds (calls/references/imports/extends/implements/inherits/contains)
are graphify relations already, so blast radius traverses them directly.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

# CodeGraph edge-kind → graphify relation. Pass-through for the ones we already
# traverse; a couple of synonyms normalised. Unknown kinds are kept verbatim
# (harmless — blast radius simply ignores relations it doesn't know).
_RELATION_SYNONYMS = {
    "extend": "extends",
    "implement": "implements",
    "inherit": "inherits",
    "call": "calls",
    "reference": "references",
    "import": "imports",
}

_NODE_SQL = (
    "SELECT id, kind, name, qualified_name, file_path, start_line, "
    "signature, docstring, language FROM nodes"
)
_EDGE_SQL = "SELECT source, target, kind, metadata, line FROM edges"


def _prefix(repo: str | None, node_id: str) -> str:
    return f"{repo}::{node_id}" if repo else node_id


def ingest_db(db_path: str | Path, *, repo: str | None = None) -> dict[str, Any]:
    """Read one CodeGraph SQLite graph into graphify ``{nodes, edges}``.

    ``repo`` (when given) tags every node's ``metadata.repo`` and namespaces its id,
    so a node is attributable to its service and ids don't collide across repos.
    """
    db_path = Path(db_path)
    if not db_path.is_file():
        raise FileNotFoundError(f"no CodeGraph db at {db_path}")
    # read-only open so we never touch CodeGraph's file
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        nodes: list[dict] = []
        for r in conn.execute(_NODE_SQL):
            meta = {
                "qualified_name": r["qualified_name"],
                "signature": r["signature"],
                "language": r["language"],
                "repo": repo,
            }
            nodes.append({
                "id": _prefix(repo, r["id"]),
                "label": r["name"],
                "kind": r["kind"],
                "file_type": "code",
                "source_file": r["file_path"],
                "source_location": f"L{r['start_line']}" if r["start_line"] else None,
                "metadata": {k: v for k, v in meta.items() if v},
            })
        edges: list[dict] = []
        for r in conn.execute(_EDGE_SQL):
            rel = _RELATION_SYNONYMS.get(r["kind"], r["kind"])
            try:
                emeta = json.loads(r["metadata"]) if r["metadata"] else {}
            except (json.JSONDecodeError, TypeError):
                emeta = {}
            edges.append({
                "source": _prefix(repo, r["source"]),
                "target": _prefix(repo, r["target"]),
                "relation": rel,
                "confidence": "EXTRACTED",
                "confidence_score": 1.0,
                "source_location": f"L{r['line']}" if r["line"] else None,
                "context": "codegraph",
                "metadata": emeta,
            })
    finally:
        conn.close()
    return {"nodes": nodes, "edges": edges}


def discover_repo_dbs(root: str | Path) -> dict[str, Path]:
    """Map service/repo name → its ``.codegraph/codegraph.db``.

    Looks at ``root/.codegraph`` (a single index over the whole tree, keyed by the
    root's own name) and at each immediate sub-directory (the multi-repo case, each
    repo indexed separately). Sub-directory indexes win — they're the "set of
    repositories" model this whole layer targets."""
    root = Path(root)
    dbs: dict[str, Path] = {}
    root_db = root / ".codegraph" / "codegraph.db"
    if root_db.is_file():
        dbs[root.name] = root_db
    for sub in sorted(p for p in root.iterdir() if p.is_dir()):
        sub_db = sub / ".codegraph" / "codegraph.db"
        if sub_db.is_file():
            dbs.pop(root.name, None)  # a per-repo index supersedes the whole-tree one
            dbs[sub.name] = sub_db
    return dbs


def ingest_estate(root: str | Path) -> dict[str, Any]:
    """Ingest every CodeGraph db under ``root`` into one merged ``{nodes, edges}``,
    each repo tagged + namespaced. Raises if no CodeGraph index is found."""
    dbs = discover_repo_dbs(root)
    if not dbs:
        raise FileNotFoundError(
            f"no CodeGraph index under {root} — run `codegraph init` in each service "
            "(or the root), then retry."
        )
    nodes: list[dict] = []
    edges: list[dict] = []
    # when a single whole-tree index is used, don't force a repo tag (service falls
    # out of the path); with per-repo dbs, tag each with its repo name.
    single = len(dbs) == 1 and Path(root).name in dbs
    for repo, db in dbs.items():
        got = ingest_db(db, repo=None if single else repo)
        nodes += got["nodes"]
        edges += got["edges"]
    return {"nodes": nodes, "edges": edges, "repos": sorted(dbs)}


def cross_service_extraction_from_codegraph(root: str | Path) -> dict[str, Any]:
    """The full enhancement: CodeGraph's within-repo graph + our cross-service edges.

    Ingest the CodeGraph index(es) under ``root``, then run our contract pivot over
    the same source tree and reconcile the ``calls_service`` edges onto the ingested
    nodes by ``(file, symbol-name)`` identity — exactly as we fold them onto our own
    tree-sitter nodes. The result is one connected estate graph: CodeGraph's fast
    within-repo edges plus the cross-repo edges it could not resolve.
    """
    from graphify.contract_introspect import cross_service_graph, reconcile_contract

    base = ingest_estate(root)
    contract = cross_service_graph(root)
    rec = reconcile_contract(base, contract)  # stamps service on matched base nodes
    return {
        "nodes": base["nodes"] + rec["nodes"],
        "edges": base["edges"] + rec["edges"],
        "repos": base["repos"],
        "reconciliation": rec["reconciliation"],
    }
