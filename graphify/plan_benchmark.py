"""plan_benchmark.py — quantify the PRD→services plan vs a grep/read agent.

CodeGraph markets "88% fewer tool calls / 62% fewer tokens" against agents that
grep and read files. This measures the analogous numbers for graphify's
``plan_change``: ONE graph-backed tool call that returns the responsible +
downstream services and the seed symbols, versus a naive agent that greps the
corpus for the PRD's keywords, reads every hit, and then follows each HTTP call by
hand to establish cross-service impact.

The baseline is **really executed** (grep + read over the actual files, real token
counts) — not fabricated. But it is a deliberately *simple* agent, so the
assumptions are explicit:
  • it greps each distinct PRD keyword (one "tool call" per keyword),
  • reads every file that matches any keyword (one call each, tokens counted),
  • for every outbound HTTP call it reads, it must grep the target route and read
    the producer file to learn which service it hits (the cross-service hop the
    graph gives for free) — detected with the same consumer patterns
    ``contract_introspect`` uses.
Treat the ratios as a corpus-relative signal, not an absolute; run it on your own
estate for real figures. Two caveats worth stating up front:
  • **Tool-call reduction is scale-robust** (~1 call vs many, on any corpus);
    **token reduction grows with file size** — on tiny fixtures where a sparse
    query reads only a few short files it can be ~0 or even negative, since the
    plan text has a fixed size. Report tool calls as the headline.
  • The graph is built **once** upfront by static parsing (the contract layer costs
    zero LLM tokens); that amortised cost is not charged per query here, exactly as
    CodeGraph amortises its index.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from graphify.contract_introspect import (
    _AXIOS,
    _FETCH,
    _PY_HTTP,
    _RESTTEMPLATE,
    _WEBCLIENT_URI,
    normalize_path,
)
from graphify.semantic_index import tokenize
from graphify.service_profiles import service_of

# Files a grep/read agent would actually open. Code + the docs an agent greps.
_CODE_EXTS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".go", ".rb", ".php", ".cs",
    ".rs", ".kt", ".scala", ".c", ".cpp", ".h", ".hpp", ".swift", ".md", ".rst", ".txt",
}
# A real agent tries a handful of search terms, not every token. Cap so the
# baseline isn't inflated in our favour (more greps = worse baseline).
_MAX_KEYWORDS = 8
_CHARS_PER_TOKEN = 4  # same approximation as benchmark.py, deterministic across envs

# Consumer call-site patterns → the URL/path an agent would see when reading a file.
_CALL_PATTERNS = (_FETCH, _AXIOS, _PY_HTTP, _RESTTEMPLATE, _WEBCLIENT_URI)


def _estimate_tokens(text: str) -> int:
    return max(0, len(text) // _CHARS_PER_TOKEN)


@dataclass
class Cost:
    tool_calls: int = 0
    context_tokens: int = 0
    files_read: int = 0


@dataclass
class PlanBenchmark:
    prd: str
    graphify: Cost
    baseline: Cost
    services_graphify: list[str]   # responsible ∪ downstream (precise)
    services_baseline: list[str]   # every service the agent had to read into
    tool_call_reduction: float     # 1 - g/b
    token_reduction: float


def _iter_source_files(root: Path) -> list[Path]:
    return [p for p in sorted(root.rglob("*"))
            if p.is_file() and p.suffix.lower() in _CODE_EXTS
            and ".git" not in p.parts and "node_modules" not in p.parts]


def _keywords(prd: str) -> list[str]:
    # distinct content tokens, longest-first (a real agent searches specific words)
    seen: dict[str, None] = {}
    for t in sorted(set(tokenize(prd)), key=lambda x: (-len(x), x)):
        if len(t) >= 4:
            seen.setdefault(t, None)
        if len(seen) >= _MAX_KEYWORDS:
            break
    return list(seen)


def _call_paths_in(text: str) -> set[str]:
    """Normalized target paths of outbound HTTP calls visible in a file — what an
    agent must chase to another service."""
    out: set[str] = set()
    for pat in _CALL_PATTERNS:
        for m in pat.finditer(text):
            url = m.group(m.lastindex) if m.lastindex else m.group(0)
            # skip clearly-external hosts (an agent wouldn't grep the local corpus for them)
            if re.match(r"^[a-z]+://(?:api\.)?(?:stripe|github|google|amazonaws)\b", url or ""):
                continue
            p = normalize_path(url)
            if p and p != "/":
                out.add(p)
    return out


def grep_read_baseline(root: Path, prd: str) -> tuple[Cost, set[str]]:
    """Deterministically execute the naive agent's retrieval and tally its cost.
    Returns the cost and the set of services it had to read into."""
    files = _iter_source_files(root)
    contents = {p: p.read_text(encoding="utf-8", errors="replace") for p in files}
    lowered = {p: c.lower() for p, c in contents.items()}

    cost = Cost()
    read: set[Path] = set()

    def _read(p: Path) -> None:
        if p in read:
            return
        read.add(p)
        cost.files_read += 1
        cost.tool_calls += 1
        cost.context_tokens += _estimate_tokens(contents.get(p, ""))

    # 1) keyword greps → read every hit
    for kw in _keywords(prd):
        cost.tool_calls += 1  # one grep per keyword
        for p in files:
            if kw in lowered[p]:
                _read(p)

    # 2) cross-service hops: for each outbound call in the files already read,
    #    grep the target route and read the producer file(s).
    resolved: set[str] = set()
    for p in list(read):
        for path in _call_paths_in(contents[p]):
            if path in resolved:
                continue
            resolved.add(path)
            cost.tool_calls += 1  # grep for the route across the corpus
            seg = next((s for s in reversed(path.split("/")) if s and s != "{}"), "")
            if not seg:
                continue
            for q in files:
                if q not in read and ("/" + seg).lower() in lowered[q]:
                    _read(q)

    services = {s for p in read if (s := service_of({"source_file": str(p)}, root=root))}
    return cost, services


def graphify_plan_cost(prd: str, plan) -> tuple[Cost, set[str]]:
    """Cost of answering via ``plan_change``: one tool call, context = the plan
    text the agent reads back; it opens zero files (works off the prebuilt graph)."""
    from graphify.change_plan import format_change_plan
    text = format_change_plan(plan)
    services = set(plan.scoped_services) | set(plan.impacted_services)
    return Cost(tool_calls=1, context_tokens=_estimate_tokens(text), files_read=0), services


def benchmark_plan(root: str | Path, prds: list[str], *, service_docs=None,
                   top_services: int = 3, top_seeds: int = 5, depth: int = 3) -> list[PlanBenchmark]:
    """Run the head-to-head for each PRD over the services under ``root``."""
    from graphify.change_plan import graph_from_extraction, plan_change
    from graphify.contract_introspect import cross_service_graph
    from graphify.service_profiles import load_service_docs

    root = Path(root)
    G = graph_from_extraction(cross_service_graph(root))
    docs = service_docs if service_docs is not None else load_service_docs(root)

    results: list[PlanBenchmark] = []
    for prd in prds:
        plan = plan_change(G, prd, root=root, service_docs=docs,
                           top_services=top_services, top_seeds=top_seeds, depth=depth)
        g_cost, g_svc = graphify_plan_cost(prd, plan)
        b_cost, b_svc = grep_read_baseline(root, prd)
        tc = 1 - (g_cost.tool_calls / b_cost.tool_calls) if b_cost.tool_calls else 0.0
        tk = 1 - (g_cost.context_tokens / b_cost.context_tokens) if b_cost.context_tokens else 0.0
        results.append(PlanBenchmark(
            prd=prd, graphify=g_cost, baseline=b_cost,
            services_graphify=sorted(g_svc), services_baseline=sorted(b_svc),
            tool_call_reduction=round(tc, 3), token_reduction=round(tk, 3),
        ))
    return results


def _pct(x: float) -> str:
    return f"{round(x * 100)}%"


def format_plan_benchmark(results: list[PlanBenchmark]) -> str:
    if not results:
        return "No PRDs to benchmark."
    lines = ["plan_change vs grep/read agent — per PRD:", ""]
    for r in results:
        lines += [
            f'  "{r.prd}"',
            f"    graphify : {r.graphify.tool_calls} tool call, "
            f"~{r.graphify.context_tokens} ctx tokens, {r.graphify.files_read} files "
            f"→ services {r.services_graphify or ['—']}",
            f"    grep/read: {r.baseline.tool_calls} tool calls, "
            f"~{r.baseline.context_tokens} ctx tokens, {r.baseline.files_read} files "
            f"→ services {r.services_baseline or ['—']}",
            f"    reduction: {_pct(r.tool_call_reduction)} fewer tool calls, "
            f"{_pct(r.token_reduction)} fewer context tokens",
            "",
        ]
    n = len(results)
    avg_tc = sum(r.tool_call_reduction for r in results) / n
    avg_tk = sum(r.token_reduction for r in results) / n
    lines += [
        "Aggregate (mean over PRDs):",
        f"  {_pct(avg_tc)} fewer tool calls (scale-robust headline)",
        f"  {_pct(avg_tk)} fewer context tokens (grows with file size — tiny "
        "fixtures understate this)",
        "  baseline is a simplified grep/read agent (see module docstring); the "
        "graph is built once upfront and amortised across all queries.",
    ]
    return "\n".join(lines)
