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


@dataclass(frozen=True)
class ContractChange:
    """An API contract (endpoint) inside the change surface, plus who consumes it.

    ``consumers`` are the *other* services whose code calls this endpoint over the
    wire (``calls_service`` edges). A non-empty consumer set means changing this
    contract risks breaking them — the cross-service blast the graph makes explicit."""

    endpoint: str            # "POST /invoices" (from the route node)
    service: str | None      # the owning (producer) service
    handler: str             # the handler function label
    location: str
    consumers: list[str] = field(default_factory=list)   # consuming services (break risk)

    @property
    def breaking_risk(self) -> bool:
        return bool(self.consumers)


@dataclass(frozen=True)
class ExternalCall:
    """A third-party / outbound HTTP call in the impacted code (Stripe, GitHub, …)."""

    caller: str
    service: str | None
    host: str                # api.stripe.com
    method: str
    url: str
    location: str


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
    contracts_changed: list[ContractChange] = field(default_factory=list)  # endpoints in the change surface
    external_calls: list[ExternalCall] = field(default_factory=list)       # third-party calls impacted
    detailed_plan: str = ""                                          # optional LLM narrative (--detailed)


def _derive_contracts(G: nx.Graph, surface_ids: set[str]) -> list[ContractChange]:
    """Endpoints inside the change surface + the services that consume them.

    An endpoint is in the surface when its ``route`` node OR its handler (the source
    of a ``handles`` edge) is a seed or in the blast radius. Consumers come from the
    reverse ``calls_service`` edges into that handler. Purely graph-derived —
    deterministic, no source access — so it works on any cross-service graph."""
    directed = G.is_directed()

    def _in_edges(n):   # (u, n) regardless of graph directedness
        return G.in_edges(n, data=True) if directed else G.edges(n, data=True)

    def _out_edges(n):
        return G.out_edges(n, data=True) if directed else G.edges(n, data=True)

    # Map each handler → the route it handles (handler --handles--> route).
    handler_route: dict[str, str] = {}
    for u, v, d in G.edges(data=True):
        if d.get("relation") == "handles":
            handler_route[u] = v

    def _consumers(handler_id: str) -> list[str]:
        svcs: set[str] = set()
        for a, b, d in _in_edges(handler_id):
            if d.get("relation") != "calls_service":
                continue
            other = a if b == handler_id else b
            svc = ((G.nodes.get(other) or {}).get("metadata") or {}).get("service")
            svcs.add(str(svc) if svc else "unknown")
        return sorted(svcs)

    out: dict[str, ContractChange] = {}
    for nid in surface_ids:
        if nid not in G:
            continue
        d = G.nodes[nid]
        kind = str(d.get("kind") or "").lower()
        # Resolve (route_id, handler_id) from either a route node or a handler node.
        if kind == "route":
            route_id, handler_id = nid, None
            for a, b, ed in _in_edges(nid):
                if ed.get("relation") == "handles":
                    handler_id = a if b == nid else b
                    break
        elif nid in handler_route:
            handler_id, route_id = nid, handler_route[nid]
        else:
            continue
        rd = G.nodes.get(route_id) or {}
        endpoint = str(rd.get("label") or route_id)
        svc = ((rd.get("metadata") or {}).get("service")
               or (d.get("metadata") or {}).get("service"))
        handler_label = str((G.nodes.get(handler_id) or {}).get("label") or handler_id or "?")
        loc = f"{rd.get('source_file') or d.get('source_file') or ''}:{rd.get('source_location') or ''}".strip(":")
        consumers = _consumers(handler_id) if handler_id else []
        cc = ContractChange(endpoint=endpoint, service=str(svc) if svc else None,
                             handler=handler_label, location=loc, consumers=consumers)
        # Dedup by endpoint; keep the row that carries consumers.
        prev = out.get(endpoint)
        if prev is None or (cc.consumers and not prev.consumers):
            out[endpoint] = cc
    return sorted(out.values(), key=lambda c: (not c.breaking_risk, c.endpoint))


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

    # Contracts in the change surface (seeds + blast radius) + their consumers.
    surface_ids = {s.symbol_id for s in seeds} | {r.node_id for r in affected}
    contracts_changed = _derive_contracts(G, surface_ids)

    # Third-party / outbound calls in the impacted services (best-effort: needs the
    # source tree via ``root``; the graph doesn't carry external calls today).
    external: list[ExternalCall] = []
    if root is not None:
        touched = set(selected) | set(impacted_services) | {
            (n.get("metadata") or {}).get("service")
            for n in nodes if service_of(n, root=root)
        }
        touched.discard(None)
        try:
            from graphify.contract_introspect import external_calls as _ext
            from graphify.contract_introspect import host_of as _host
            for c in _ext(root, services=touched or None):
                external.append(ExternalCall(
                    caller=f"{c.caller}()", service=c.service or None,
                    host=_host(c.raw_url), method=c.method,
                    url=c.raw_url, location=f"{c.source_file}:L{c.line}",
                ))
        except Exception:
            external = []          # never let a source-scan hiccup break the plan

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
        contracts_changed=contracts_changed,
        external_calls=external,
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

    # Contracts changed
    lines += ["", f"Contracts changed — {len(plan.contracts_changed)} endpoint(s) in the change surface:"]
    if plan.contracts_changed:
        for c in plan.contracts_changed:
            svc = f" {{{sz(c.service)}}}" if c.service else ""
            risk = (f"  ! BREAKING risk — consumed by: {', '.join(sz(s) for s in c.consumers)}"
                    if c.breaking_risk else "  (no cross-service consumers detected)")
            loc = f"  {sz(c.location)}" if c.location else ""
            lines.append(f"  {sz(c.endpoint)}{svc}  ->  {sz(c.handler)}{loc}")
            lines.append(f"    {risk}")
    else:
        lines.append("  (no API endpoints in the change surface — internal-only change)")

    # Third-party calls
    lines += ["", f"Third-party calls in the impacted code — {len(plan.external_calls)} call(s):"]
    if plan.external_calls:
        for x in plan.external_calls:
            svc = f" {{{sz(x.service)}}}" if x.service else ""
            lines.append(f"  {sz(x.host)}  [{sz(x.method)}] {sz(x.url)}{svc}"
                         f"  <- {sz(x.caller)}  {sz(x.location)}")
    else:
        lines.append("  (none detected — or run with --root so the source can be scanned)")

    if plan.detailed_plan:
        lines += ["", "Detailed plan:", "", plan.detailed_plan]

    return "\n".join(lines)


def _detailed_prompt(plan: ChangePlan) -> str:
    """Assemble the grounded facts of a ``ChangePlan`` into a synthesis prompt. The
    LLM narrates over verified structure — it does not invent the impact set."""
    svc = ", ".join(f"{m.service} ({m.evidence[0]})" if m.evidence else m.service
                    for m in plan.services) or "none identified"
    contracts = "\n".join(
        f"  - {c.endpoint} [{c.service or '?'}] handler {c.handler}"
        + (f" — BREAKING risk, consumed by {', '.join(c.consumers)}" if c.breaking_risk else "")
        for c in plan.contracts_changed) or "  - none"
    ext = "\n".join(f"  - {x.host} [{x.method}] via {x.caller} ({x.service or '?'})"
                    for x in plan.external_calls) or "  - none"
    downstream = ", ".join(f"{s} ({n})" for s, n in plan.impacted_services.items()) or "none"
    seeds = ", ".join(f"{s.name} [{s.path}]" for s in plan.seeds[:10]) or "none"
    return (
        "You are a staff engineer writing an implementation plan for a change request. "
        "Use ONLY the grounded facts below (derived from a code graph); do not invent files, "
        "services, or endpoints. Produce a concise, ordered plan.\n\n"
        f"CHANGE REQUEST:\n{plan.prd}\n\n"
        f"RESPONSIBLE SERVICES (and why they matched):\n  {svc}\n\n"
        f"CHANGE-POINT SYMBOLS (seeds):\n  {seeds}\n\n"
        f"API CONTRACTS IN THE CHANGE SURFACE:\n{contracts}\n\n"
        f"THIRD-PARTY CALLS IN IMPACTED CODE:\n{ext}\n\n"
        f"DOWNSTREAM-IMPACTED SERVICES (blast radius):\n  {downstream}\n\n"
        "Write the plan as:\n"
        "1. Per responsible service: what to change and WHY (tie to the request).\n"
        "2. Contract changes: for each endpoint, the change + migration/versioning if it has consumers.\n"
        "3. Third-party integration work, if any.\n"
        "4. Suggested order of execution and the top risks."
    )


def synthesize_detailed_plan(
    plan: ChangePlan,
    *,
    backend: str = "claude",
    model: str | None = None,
    max_tokens: int = 900,
    caller: Callable[[str], str] | None = None,
) -> str:
    """Turn a grounded ``ChangePlan`` into a natural-language detailed plan via an
    LLM. ``caller`` is injectable (defaults to ``llm._call_llm``) so this is
    unit-testable without an API key. Any failure returns ``""`` — the structured
    plan already stands on its own, so the narrative is pure enrichment."""
    def _call(prompt: str) -> str:
        if caller is not None:
            return caller(prompt)
        from graphify.llm import _call_llm
        return _call_llm(prompt, backend=backend, model=model, max_tokens=max_tokens)

    try:
        return (_call(_detailed_prompt(plan)) or "").strip()
    except Exception:
        return ""
