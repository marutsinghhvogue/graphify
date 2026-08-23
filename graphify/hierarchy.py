"""Hierarchical wiki — recursive module-tree decomposition + navigable articles.

Upgrades the flat one-article-per-community wiki (:mod:`graphify.wiki`) to a
CodeWiki-style **multi-granularity tree**: an ``overview.md`` entry point,
top-level modules, and recursively-split sub-modules, each rendered as its own
article, plus a ``module_tree.json`` for programmatic navigation and a
``metadata.json`` (with the source commit) as the foundation for incremental
regeneration.

Reuses existing machinery — :func:`graphify.cluster._partition` for the
recursive split, :func:`graphify.wiki._safe_filename` for slugs, and
:func:`graphify.build.edge_data` for confidence tiers — so nothing about the
graph model changes. Deterministic and offline by default; an optional
``summarizer`` callable authors a one-line module blurb via an LLM.

See issue #9 (CodeWiki-parity) and ``docs/deployment-railway.md``.
"""
from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import networkx as nx

from graphify.build import edge_data
from graphify.cluster import cohesion_score
from graphify.wiki import _safe_filename

# A module summarizer takes (title, ordered top-node labels, source files) and
# returns a one-line blurb. Injectable so the wiring is unit-testable without an
# API key; any failure should return "" (the caller degrades gracefully).
Summarizer = Callable[[str, list[str], list[str]], str]

_DEFAULT_MAX_DEPTH = 2
_DEFAULT_MAX_NODES = 40   # split a module while it holds more than this many nodes
_DEFAULT_MIN_NODES = 5    # sub-groups smaller than this fold back into the parent


@dataclass
class Module:
    """One node of the decomposition tree."""
    module_id: str            # stable dotted path, e.g. "m0" or "m0.1"
    title: str
    level: int                # 1 = top-level
    node_ids: list[str]       # every node under this module (own + descendants)
    own_nodes: list[str]      # nodes rendered directly here (leaf residue)
    children: list[Module] = field(default_factory=list)
    cohesion: float = 0.0
    summary: str = ""

    @property
    def file(self) -> str:
        return f"{self.module_id.replace('.', '_')}-{_safe_filename(self.title)}.md"


# ---------------------------------------------------------------------------
# Decomposition
# ---------------------------------------------------------------------------

def _subpartition(G: nx.Graph, nodes: list[str]) -> list[list[str]]:
    """Split ``nodes`` into sub-groups with a second community-detection pass.

    Returns ``[]`` when no meaningful split exists (no edges, or one group).
    Isolates dropped by the partitioner are collected into a trailing group so
    no node is ever lost.
    """
    from graphify.cluster import _partition  # local import: heavy optional dep chain

    sub = G.subgraph(nodes)
    if sub.number_of_edges() == 0:
        return []
    try:
        part = _partition(sub)
    except Exception:
        return []
    groups: dict[int, list[str]] = {}
    for n, cid in part.items():
        groups.setdefault(cid, []).append(n)
    result = [sorted(v) for v in groups.values() if v]
    if len(result) <= 1:
        return []
    leftover = sorted(set(nodes) - set(part))
    if leftover:
        result.append(leftover)
    # Largest sub-module first for stable, readable ids.
    result.sort(key=lambda ns: (-len(ns), ns[0] if ns else ""))
    return result


def _module_title(G: nx.Graph, nodes: list[str], fallback: str) -> str:
    """Name a (sub-)module after its most-connected node, else ``fallback``."""
    if not nodes:
        return fallback
    top = max(nodes, key=lambda n: G.degree(n) if n in G else 0)
    label = G.nodes[top].get("label", top) if top in G else top
    label = str(label).strip()
    return label or fallback


def _build_subtree(
    G: nx.Graph,
    nodes: list[str],
    title: str,
    *,
    level: int,
    max_depth: int,
    budget: int,
    min_size: int,
    path: str,
) -> Module:
    node_ids = sorted(nodes)
    children: list[Module] = []
    own = node_ids
    if len(node_ids) > budget and level < max_depth:
        # Keep only sub-groups worth their own article; fold the small tail back
        # into the parent so the tree isn't cluttered with 2-node "modules".
        big = [sub for sub in _subpartition(G, node_ids) if len(sub) >= min_size]
        if len(big) >= 2:
            assigned: set[str] = set()
            for j, sub in enumerate(big):
                child_title = _module_title(G, sub, f"{title} · part {j + 1}")
                children.append(
                    _build_subtree(
                        G, sub, child_title,
                        level=level + 1, max_depth=max_depth, budget=budget,
                        min_size=min_size, path=f"{path}.{j}",
                    )
                )
                assigned.update(sub)
            # Anything not placed in a child stays rendered on the parent.
            own = [n for n in node_ids if n not in assigned]
    return Module(
        module_id=path,
        title=title,
        level=level,
        node_ids=node_ids,
        own_nodes=own,
        children=children,
        cohesion=cohesion_score(G, node_ids),
    )


def build_module_tree(
    G: nx.Graph,
    communities: dict[int, list[str]],
    *,
    community_labels: dict[int, str] | None = None,
    max_depth: int = _DEFAULT_MAX_DEPTH,
    max_nodes_per_module: int = _DEFAULT_MAX_NODES,
    min_module_size: int = _DEFAULT_MIN_NODES,
) -> list[Module]:
    """Decompose ``communities`` into a hierarchical module tree.

    Top-level modules are the communities (largest first); any module over
    ``max_nodes_per_module`` is recursively sub-partitioned up to ``max_depth``
    levels. Sub-groups smaller than ``min_module_size`` fold back into the
    parent rather than becoming their own article. Stale node IDs (present in
    ``communities`` but not in ``G``) are dropped, mirroring
    :func:`graphify.wiki.to_wiki`.
    """
    labels = community_labels or {}
    g_nodes = set(G.nodes)
    clean = {
        cid: [n for n in nodes if n in g_nodes]
        for cid, nodes in communities.items()
    }
    clean = {cid: nodes for cid, nodes in clean.items() if nodes}
    ordered = sorted(clean.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    roots: list[Module] = []
    for i, (cid, nodes) in enumerate(ordered):
        title = labels.get(cid) or f"Module {i}"
        roots.append(
            _build_subtree(
                G, nodes, title,
                level=1, max_depth=max_depth, budget=max_nodes_per_module,
                min_size=min_module_size, path=f"m{i}",
            )
        )
    return roots


def _walk(roots: list[Module]):
    """Depth-first iterator over every module in the forest."""
    stack = list(roots)
    while stack:
        m = stack.pop()
        yield m
        stack.extend(reversed(m.children))


def _node_to_module(roots: list[Module]) -> dict[str, str]:
    """Map each node to the id of the module that renders it (own_nodes)."""
    owner: dict[str, str] = {}
    for m in _walk(roots):
        for n in m.own_nodes:
            owner[n] = m.module_id
    return owner


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _mermaid_id(module_id: str) -> str:
    return "n" + module_id.replace(".", "_")


def _overview_mermaid(roots: list[Module], *, max_nodes: int = 60) -> list[str]:
    """A top-down Mermaid tree of the module hierarchy (bounded for readability)."""
    lines = ["```mermaid", "graph TD"]
    count = 0
    for parent in _walk(roots):
        for child in parent.children:
            if count >= max_nodes:
                break
            ptxt = parent.title.replace('"', "'")[:40]
            ctxt = child.title.replace('"', "'")[:40]
            lines.append(f'  {_mermaid_id(parent.module_id)}["{ptxt}"] --> '
                         f'{_mermaid_id(child.module_id)}["{ctxt}"]')
            count += 1
    if count == 0:  # single-level: still show the roots as standalone nodes
        for m in roots[:max_nodes]:
            lines.append(f'  {_mermaid_id(m.module_id)}["{m.title.replace(chr(34), "")[:40]}"]')
    lines.append("```")
    return lines


def _top_nodes(G: nx.Graph, nodes: list[str], k: int = 20) -> list[str]:
    return sorted(nodes, key=lambda n: G.degree(n) if n in G else 0, reverse=True)[:k]


def _cross_module_links(
    G: nx.Graph, nodes: list[str], self_id: str,
    owner: dict[str, str], id_to_title: dict[str, str],
) -> list[tuple[str, int]]:
    counts: Counter = Counter()
    node_set = set(nodes)
    for nid in nodes:
        if nid not in G:
            continue
        for nb in G.neighbors(nid):
            if nb in node_set:
                continue
            mod = owner.get(nb)
            if mod and mod != self_id:
                counts[id_to_title.get(mod, mod)] += 1
    return sorted(counts.items(), key=lambda x: -x[1])


def _module_article(
    G: nx.Graph, m: Module, *,
    owner: dict[str, str], id_to_title: dict[str, str],
    parent: Module | None, roots_title: str,
) -> str:
    lines: list[str] = [f"# {m.title}", ""]
    meta = [f"module `{m.module_id}`", f"level {m.level}",
            f"{len(m.node_ids)} nodes", f"cohesion {m.cohesion:.2f}"]
    lines += [f"> {' · '.join(meta)}", ""]
    if m.summary:
        lines += [m.summary, ""]

    # Breadcrumb / navigation.
    crumb = "[Overview](overview.md)"
    if parent is not None:
        crumb += f" / [{parent.title}]({parent.file})"
    crumb += f" / **{m.title}**"
    lines += [crumb, ""]

    if m.children:
        lines += ["## Submodules", ""]
        for c in m.children:
            lines.append(f"- [{c.title}]({c.file}) — {len(c.node_ids)} nodes")
        lines.append("")

    render_nodes = m.own_nodes or m.node_ids
    lines += ["## Key Concepts", ""]
    for nid in _top_nodes(G, render_nodes):
        d = G.nodes[nid]
        src = d.get("source_file") or ""
        src_str = f" — `{src}`" if src else ""
        lines.append(f"- **{d.get('label', nid)}** ({G.degree(nid)} connections){src_str}")
    extra = len(render_nodes) - len(_top_nodes(G, render_nodes))
    if extra > 0:
        lines.append(f"- *... and {extra} more nodes in this module*")
    lines.append("")

    cross = _cross_module_links(G, m.node_ids, m.module_id, owner, id_to_title)
    lines += ["## Relationships", ""]
    if cross:
        for other, count in cross[:12]:
            lines.append(f"- **{other}** ({count} shared connections)")
    else:
        lines.append("- No strong cross-module connections detected")
    lines.append("")

    sources = sorted({G.nodes[n].get("source_file") or "" for n in m.node_ids} - {""})
    if sources:
        lines += ["## Source Files", ""]
        lines += [f"- `{s}`" for s in sources[:25]]
        lines.append("")

    conf: Counter = Counter()
    for nid in m.node_ids:
        if nid not in G:
            continue
        for nb in G.neighbors(nid):
            conf[edge_data(G, nid, nb).get("confidence", "EXTRACTED")] += 1
    total = sum(conf.values()) or 1
    lines += ["## Audit Trail", ""]
    for tier in ("EXTRACTED", "INFERRED", "AMBIGUOUS"):
        n = conf.get(tier, 0)
        lines.append(f"- {tier}: {n} ({round(n / total * 100)}%)")
    lines += ["", "---", "", "*Part of the graphify hierarchical wiki. See [Overview](overview.md).*"]
    return "\n".join(lines)


def _overview_md(roots: list[Module], total_nodes: int, total_edges: int, max_depth: int) -> str:
    module_count = sum(1 for _ in _walk(roots))
    lines = [
        "# Architecture Overview",
        "",
        "> Auto-generated by graphify. Start here, then drill into modules; each "
        "module links to its submodules and neighbours.",
        "",
        f"**{total_nodes} nodes · {total_edges} edges · {len(roots)} top-level "
        f"modules · {module_count} modules total · depth {max_depth}**",
        "",
        "---",
        "",
        "## Modules",
        "(largest first)",
        "",
    ]
    for m in roots:
        sub = f" · {len(m.children)} submodules" if m.children else ""
        blurb = f" — {m.summary}" if m.summary else ""
        lines.append(f"- [{m.title}]({m.file}) — {len(m.node_ids)} nodes{sub}{blurb}")
    lines += ["", "## Module Map", ""]
    lines += _overview_mermaid(roots)
    lines += ["", "---", "", "*Generated by [graphify](https://github.com/safishamsi/graphify)*"]
    return "\n".join(lines)


def _tree_json(roots: list[Module]) -> dict:
    def node(m: Module) -> dict:
        return {
            "module_id": m.module_id,
            "title": m.title,
            "level": m.level,
            "file": m.file,
            "node_count": len(m.node_ids),
            "cohesion": round(m.cohesion, 4),
            "children": [node(c) for c in m.children],
        }
    return {"modules": [node(m) for m in roots]}


def to_hierarchical_wiki(
    G: nx.Graph,
    communities: dict[int, list[str]],
    output_dir: str | Path,
    *,
    community_labels: dict[int, str] | None = None,
    max_depth: int = _DEFAULT_MAX_DEPTH,
    max_nodes_per_module: int = _DEFAULT_MAX_NODES,
    min_module_size: int = _DEFAULT_MIN_NODES,
    summarizer: Summarizer | None = None,
) -> int:
    """Generate a hierarchical wiki. Returns the number of module articles written.

    Writes ``overview.md`` (entry point), one ``<id>-<title>.md`` per module,
    ``module_tree.json`` (the tree), and ``metadata.json`` (commit + params).
    Owns ``output_dir`` — clears prior ``*.md`` / ``module_tree.json`` /
    ``metadata.json`` so re-runs don't leave orphans.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    if not communities:
        raise ValueError(
            "communities dict is empty — refusing to build hierarchical wiki. "
            "Run `graphify extract .` or `graphify cluster-only .` first."
        )

    roots = build_module_tree(
        G, communities,
        community_labels=community_labels,
        max_depth=max_depth,
        max_nodes_per_module=max_nodes_per_module,
        min_module_size=min_module_size,
    )
    if not roots:
        raise ValueError(
            "all community node IDs are stale — none exist in the graph. "
            "Re-run `graphify extract .` to regenerate community data."
        )

    for old in (*out.glob("*.md"), *out.glob("module_tree.json"), *out.glob("metadata.json")):
        old.unlink()

    owner = _node_to_module(roots)
    id_to_title = {m.module_id: m.title for m in _walk(roots)}
    parent_of: dict[str, Module] = {}
    for m in _walk(roots):
        for c in m.children:
            parent_of[c.module_id] = m

    if summarizer is not None:
        for m in _walk(roots):
            try:
                labels = [G.nodes[n].get("label", n) for n in _top_nodes(G, m.own_nodes or m.node_ids, 12)]
                srcs = sorted({G.nodes[n].get("source_file") or "" for n in m.node_ids} - {""})
                m.summary = (summarizer(m.title, labels, srcs[:15]) or "").strip()
            except Exception:
                m.summary = ""

    count = 0
    for m in _walk(roots):
        article = _module_article(
            G, m, owner=owner, id_to_title=id_to_title,
            parent=parent_of.get(m.module_id), roots_title="Overview",
        )
        (out / m.file).write_text(article, encoding="utf-8")
        count += 1

    (out / "overview.md").write_text(
        _overview_md(roots, G.number_of_nodes(), G.number_of_edges(), max_depth),
        encoding="utf-8",
    )
    (out / "module_tree.json").write_text(
        json.dumps(_tree_json(roots), indent=2, ensure_ascii=False), encoding="utf-8",
    )
    (out / "metadata.json").write_text(
        json.dumps({
            "generated_by": "graphify",
            "built_at_commit": G.graph.get("built_at_commit"),
            "total_nodes": G.number_of_nodes(),
            "total_edges": G.number_of_edges(),
            "top_level_modules": len(roots),
            "module_count": count,
            "max_depth": max_depth,
            "max_nodes_per_module": max_nodes_per_module,
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return count
