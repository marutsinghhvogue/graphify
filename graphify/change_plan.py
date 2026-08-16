"""change_plan.py — compose Stages 1→2→3 into one PRD→impact chain.

The end-to-end move of [[graphify-objective-prd-to-plan]], done *service-first*:

  1. rank the responsible SERVICES for the PRD prose  (service_profiles)
  2. discover seed symbols WITHIN those services      (semantic_index, scoped)
  3. blast radius from the top seeds                  (affected, over the graph)

Scoping Stage 2 to the Stage-1 services is the point: a PRD written in business
language selects the owning services first, so seed discovery searches only their
code instead of the whole estate. Stage 3 then crosses *back out* over
``calls_service`` / ``triggers`` / ``consumes`` edges, so the plan names both the
directly-responsible services (Stage 1) and the downstream-impacted ones (the
services whose code the blast radius reaches).

Operates on a networkx graph (Stage 3 needs it); Stage 1/2 read node dicts derived
from it. Impact rows are enriched at build time (label/service/location), so
``format_change_plan`` is pure over the ``ChangePlan`` and unit-testable without the
graph.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

import networkx as nx

from graphify.affected import DEFAULT_AFFECTED_RELATIONS, affected_nodes
from graphify.semantic_index import (
    EmbeddingProvider,
    SeedHit,
    chunk_nodes,
    retrieve_seeds,
)
from graphify.service_profiles import (
    ServiceMatch,
    Summarizer,
    build_profiles,
    rank_services,
    service_of,
)

# Relations that cross a service/process boundary — a hit reached via one of these
# means the blast radius left the seed's own service (a downstream-impacted service).
_CROSS_BOUNDARY_RELATIONS = {"calls_service", "triggers", "consumes"}


@dataclass(frozen=True)
class ImpactRow:
    """One node reached by the blast radius, enriched for display."""

    node_id: str
    label: str
    service: str | None
    depth: int
    via_relation: str
    confidence: str
    location: str
    from_seed: str          # the seed symbol whose radius first reached it
    cross_boundary: bool


@dataclass
class ChangePlan:
    prd: str
    services: list[ServiceMatch] = field(default_factory=list)       # Stage 1
    scoped_services: list[str] = field(default_factory=list)         # where Stage 2 searched
    scoped_to_all: bool = False                                      # Stage 1 found nothing → searched everything
    seeds: list[SeedHit] = field(default_factory=list)              # Stage 2
    seed_source: str = "retrieved"                                   # retrieved | service-entrypoints | none
    affected: list[ImpactRow] = field(default_factory=list)         # Stage 3 (deduped, closest depth)
    impacted_services: dict[str, int] = field(default_factory=dict)  # downstream service → affected count
    cross_boundary: int = 0


def graph_from_extraction(extraction: dict) -> nx.DiGraph:
    """Build a directed graph from a graphify ``{nodes, edges}`` extraction dict —
    the in-memory analog of ``affected.load_graph`` (which reads a path). Directed so
    the stored caller→callee / calls_service direction survives for blast radius."""
    G = nx.DiGraph()
    for n in extraction.get("nodes", []):
        nid = n.get("id")
        if nid is None:
            continue
        G.add_node(nid, **{k: v for k, v in n.items() if k != "id"})
    for e in extraction.get("edges", extraction.get("links", [])):
        s, t = e.get("source"), e.get("target")
        if s is None or t is None:
            continue
        G.add_edge(s, t, **{k: v for k, v in e.items() if k not in ("source", "target")})
    return G


def _nodes_from_graph(G: nx.Graph) -> list[dict]:
    return [
        {"id": n, "label": d.get("label", n), "kind": d.get("kind"),
         "source_file": d.get("source_file"), "source_location": d.get("source_location"),
         "metadata": d.get("metadata")}
        for n, d in G.nodes(data=True)
    ]


def _location(d: dict) -> str:
    sf = d.get("source_file") or "-"
    loc = d.get("source_location")
    return f"{sf}:{loc}" if loc else str(sf)


def plan_change(
    G: nx.Graph,
    prd: str,
    *,
    root: str | None = None,
    service_docs: dict[str, str] | None = None,
    top_services: int = 3,
    top_seeds: int = 5,
    depth: int = 2,
    embedder: EmbeddingProvider | None = None,
    summarizer: Summarizer | None = None,
) -> ChangePlan:
    """Run the service-first PRD→impact chain over ``G`` and return a ``ChangePlan``.

    ``top_services`` scopes Stage-2 seed discovery; ``top_seeds`` bounds how many
    seeds seed Stage-3 blast radius; ``depth`` is the reverse-reachability bound.
    If Stage 1 surfaces no service (no signal), Stage 2 falls back to the whole
    graph rather than returning nothing (``scoped_to_all=True``). ``summarizer``
    optionally enriches each service profile with an LLM capability sentence.
    """
    nodes = _nodes_from_graph(G)

    # Stage 1 — responsible services.
    profiles = build_profiles(nodes, root=root, service_docs=service_docs,
                              summarizer=summarizer)
    services = rank_services(prd, profiles, embedder=embedder, top_n=top_services)
    selected = {m.service for m in services}

    # Stage 2 — seeds, scoped to the responsible services (fall back to all).
    scoped_to_all = False
    if selected:
        scoped = [n for n in nodes if service_of(n, root=root) in selected]
        if not scoped:
            scoped, scoped_to_all = nodes, True
    else:
        scoped, scoped_to_all = nodes, True
    seeds = retrieve_seeds(prd, chunk_nodes(scoped), embedder=embedder, top_n=top_seeds)
    seed_source = "retrieved" if seeds else "none"

    # Entry-point fallback: the PRD selected services but shares no vocabulary with
    # any of their symbols (the Stage-2 weakness). Seed the blast radius from the
    # responsible services' handlers instead — their API surface is the natural
    # change point. Precise handlers (source of a `handles` edge) first; else any
    # function/class in scope.
    if not seeds and selected and not scoped_to_all:
        handler_ids = {
            u for u, _v, d in G.edges(data=True)
            if d.get("relation") == "handles"
            and (G.nodes[u].get("metadata") or {}).get("service") in selected
        }
        ep = [n for n in scoped if n["id"] in handler_ids]
        if not ep:
            ep = [n for n in scoped
                  if str(n.get("kind") or "").lower() in ("function", "class", "method")]
        seeds = [
            SeedHit(n["id"], str(n.get("label") or n["id"]), n.get("source_file") or "",
                    str(n.get("kind") or ""), 0.0, "entrypoint")
            for n in ep
        ][:top_seeds]
        seed_source = "service-entrypoints" if seeds else "none"

    # Stage 3 — blast radius from each seed, merged (keep the closest depth per node).
    merged: dict[str, ImpactRow] = {}
    for s in seeds:
        if s.symbol_id not in G:
            continue
        for hit in affected_nodes(G, s.symbol_id, relations=DEFAULT_AFFECTED_RELATIONS, depth=depth):
            d = G.nodes[hit.node_id]
            svc = (d.get("metadata") or {}).get("service")
            row = ImpactRow(
                node_id=hit.node_id,
                label=str(d.get("label") or hit.node_id),
                service=str(svc) if svc else None,
                depth=hit.depth,
                via_relation=hit.via_relation,
                confidence=hit.confidence or "EXTRACTED",
                location=_location(d),
                from_seed=s.name,
                cross_boundary=hit.via_relation in _CROSS_BOUNDARY_RELATIONS,
            )
            prev = merged.get(hit.node_id)
            if prev is None or row.depth < prev.depth:
                merged[hit.node_id] = row

    affected = sorted(merged.values(), key=lambda r: (r.depth, r.node_id))
    impacted_services = Counter(r.service for r in affected if r.service)
    cross_boundary = sum(1 for r in affected if r.cross_boundary)

    return ChangePlan(
        prd=prd,
        services=services,
        scoped_services=sorted(selected),
        scoped_to_all=scoped_to_all,
        seeds=seeds,
        seed_source=seed_source,
        affected=affected,
        impacted_services=dict(impacted_services.most_common()),
        cross_boundary=cross_boundary,
    )


def format_change_plan(
    plan: ChangePlan,
    *,
    max_rows: int = 25,
    sanitize: Callable[[str], str] | None = None,
) -> str:
    """Render a ``ChangePlan`` as the three-stage text artifact. Pure over the
    dataclass (rows are pre-enriched), so it is unit-testable without a graph.

    ``sanitize`` (e.g. ``serve.sanitize_label``) is applied to every graph-derived
    string — labels, paths, services, evidence — so the MCP transport can neutralise
    prompt-injection in untrusted node content; the CLI passes it plain."""
    sz = sanitize or (lambda s: s)
    lines = [f'Change plan for "{sz(plan.prd)}":', ""]

    # Stage 1
    lines.append("Responsible services (Stage 1):")
    if plan.services:
        for m in plan.services:
            ev = f"  <- {sz(m.evidence[0])}" if m.evidence else ""
            lines.append(f"  {m.score:.4f} [{sz(m.matched)}] {sz(m.service)}{ev}")
    else:
        lines.append("  (none matched — no service gave the requirement any signal)")

    # Stage 2
    scope = ("the whole graph (no service matched)" if plan.scoped_to_all
             else ", ".join(sz(s) for s in plan.scoped_services))
    via = (" via service handlers — no symbol matched the prose"
           if plan.seed_source == "service-entrypoints" else "")
    lines += ["", f"Seed symbols (Stage 2, scoped to {scope}{via}):"]
    if plan.seeds:
        for h in plan.seeds:
            kind = f" ({sz(h.kind)})" if h.kind else ""
            lines.append(f"  {h.score:.4f} [{sz(h.matched)}] {sz(h.name)}{kind}  {sz(h.path or '-')}")
    else:
        lines.append("  (no seed symbols found)")

    # Stage 3
    lines += ["", f"Blast radius (Stage 3, depth from top seeds) — "
                  f"{len(plan.affected)} affected node(s):"]
    if plan.impacted_services:
        downstream = ", ".join(f"{sz(s)}({n})" for s, n in plan.impacted_services.items())
        lines.append(f"  downstream services: {downstream}")
    if plan.cross_boundary:
        lines.append(f"  cross-boundary (service/schedule/event) hits: {plan.cross_boundary}")
    for r in plan.affected[:max_rows]:
        svc = f" {{{sz(r.service)}}}" if r.service else ""
        star = " *" if r.cross_boundary else ""
        lines.append(
            f"  d{r.depth} {sz(r.label)}{svc} [{sz(r.via_relation)}] [{sz(r.confidence)}] "
            f"{sz(r.location)}{star}"
        )
    if len(plan.affected) > max_rows:
        lines.append(f"  … and {len(plan.affected) - max_rows} more")
    if not plan.affected:
        lines.append("  (no downstream nodes — the seeds are leaves, or depth too shallow)")

    return "\n".join(lines)
