"""service_profiles.py — Stage 1 of PRD→plan: which SERVICES are responsible.

Before drilling down to symbols (``semantic_index.py``) or tracing edges
(``blast_radius``), answer the coarse question a PRD actually poses: *which
services must change?* A PRD is written in business-workflow language ("recompute
the payoff quote when a loan is settled early") and may share no identifiers with
the code. So symbol-first retrieval silently misses a service when the PRD's
vocabulary doesn't overlap its code.

This module builds a per-service **responsibility profile** — a capability
descriptor assembled from the service's API surface (routes), other entry points
(schedulers / event consumers), domain vocabulary (handler / class / function
names + file names), and its docs (README) — then ranks services against the PRD
prose. The output scopes the estate to the responsible services *before* the
symbol-level and blast-radius passes run inside them.

Same deterministic-core / pluggable-enrichment idiom as ``semantic_index.py``:
works offline today (BM25 over profile documents with identifier splitting), and
upgrades to hybrid with a pluggable ``EmbeddingProvider`` (RRF fusion) for
paraphrase recall. An optional ``Summarizer`` can author a one-line capability
sentence per service (LLM or derived); it only enriches the retrieval text and is
never required.

Service identity is derived the same way the rest of the estate derives it
(``cross_service_edges``): a node's ``metadata.service`` when the contract layer
stamped one, else the immediate sub-directory under ``root`` (each repo/service is
one immediate child of the corpus root). Pure and I/O-free except
``load_service_docs``, which is the one filesystem helper and is kept separate.
"""
from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from graphify.semantic_index import (
    BM25,
    Chunk,
    EmbeddingProvider,
    cosine,
    reciprocal_rank_fusion,
    tokenize,
)

# ── service identity ──────────────────────────────────────────────────────────

# Node kinds that describe a service's responsibility. Routes are the strongest
# capability signal (the API surface); schedulers/events are the non-HTTP entry
# points; the symbol kinds carry the domain vocabulary. The symbol set is broad so
# a full polyglot AST graph (not just the contract handler surface) contributes —
# Python/TS functions & classes, Java/C# methods & interfaces, Go structs, Rust
# traits/enums, plus module containers.
_ROUTE_KINDS = {"route", "endpoint"}
_ENTRYPOINT_KINDS = {"schedule", "event", "topic"}
_SYMBOL_KINDS = {
    "function", "class", "method", "interface", "struct", "enum",
    "trait", "module", "type", "protocol", "record",
}

# How each entry-point kind reads as prose, so a PRD that says "nightly job" or
# "when an order event arrives" can match a service that only exposes those.
_ENTRY_PROSE = {
    "route": "http api endpoint route request",
    "schedule": "scheduled job cron timer nightly batch",
    "event": "event consumer handler subscriber message",
    "topic": "message queue topic subscription stream",
}


def service_of(node: dict, *, root: str | Path | None = None) -> str | None:
    """Which service a graph node belongs to.

    Resolution order (most authoritative first, so every node in a service — not
    just its reconciled handlers — is attributed):
      1. ``metadata.service`` — stamped by the contract/reconciliation layer.
      2. ``metadata.repo`` — the per-node repo on a multi-repo/PG-exported graph,
         where each repository *is* a service (the "set of repositories" case).
      3. the immediate sub-directory of ``root`` containing ``source_file`` — the
         "each immediate child of root is a service" convention ``cross_service_graph``
         uses (only when ``root`` is given).
    Returns ``None`` when none resolve, so callers skip unattributable nodes rather
    than guess.
    """
    meta = node.get("metadata") or {}
    svc = meta.get("service") or meta.get("repo")
    if svc:
        return str(svc)
    sf = (node.get("source_file") or "").replace("\\", "/")
    if not sf:
        return None
    if root is not None:
        root_parts = [p for p in str(root).replace("\\", "/").split("/") if p]
        sf_parts = [p for p in sf.split("/") if p]
        # Locate root inside the (possibly absolute) source path; the segment
        # right after it is the service directory — but only when there is a
        # further segment beyond it (i.e. the service dir contains the file, so
        # a file sitting directly under root is not itself a service).
        for i in range(len(sf_parts) - len(root_parts) + 1):
            if sf_parts[i:i + len(root_parts)] == root_parts:
                nxt = i + len(root_parts)
                if nxt < len(sf_parts) - 1:
                    return sf_parts[nxt]
                return None
    return None


# ── profile ───────────────────────────────────────────────────────────────────


@dataclass
class ServiceProfile:
    """A service's responsibility descriptor, assembled from its graph nodes."""

    service: str
    routes: list[str] = field(default_factory=list)       # "GET /users/{}"
    entrypoints: list[str] = field(default_factory=list)  # kinds present: route/schedule/event
    symbols: list[str] = field(default_factory=list)      # handler/fn/class labels
    files: list[str] = field(default_factory=list)        # basenames (domain vocab)
    doc: str = ""                                          # README / doc text
    summary: str = ""                                     # optional one-line capability
    node_ids: list[str] = field(default_factory=list)
    text: str = ""                                        # assembled retrieval document
    tokens: list[str] = field(default_factory=list)


def _basename(source_file: str) -> str:
    return (source_file or "").replace("\\", "/").rsplit("/", 1)[-1]


def _profile_text(p: ServiceProfile) -> str:
    """Assemble the retrieval document for a profile: service name (split into
    words), entry-point prose, routes, symbols, file names, the optional summary,
    and the doc — ordered so the highest-signal terms lead."""
    parts: list[str] = [p.service.replace("_", " ").replace("-", " ")]
    parts += [_ENTRY_PROSE.get(k, k) for k in p.entrypoints]
    parts += p.routes
    parts += p.symbols
    parts += p.files
    if p.summary:
        parts.append(p.summary)
    if p.doc:
        parts.append(p.doc)
    return " ".join(x for x in parts if x).strip()


@runtime_checkable
class Summarizer(Protocol):
    """Optional one-line capability author. Given a service's raw signal, return a
    short business-language sentence ("owns invoice generation and billing"). Any
    callable of this shape works — an LLM wrapper, or a deterministic template."""

    def __call__(self, service: str, routes: list[str], symbols: list[str], doc: str) -> str: ...


def _summary_prompt(service: str, routes: list[str], symbols: list[str], doc: str) -> str:
    """Prompt an LLM for a one-line business-capability sentence from a service's
    raw signal. Doc is truncated so a long README can't blow the token budget."""
    lines = [
        "You are labelling the business responsibility of a software service.",
        "Given its API routes, symbol names, and README, reply with ONE short sentence "
        "(≤ 25 words) naming the business capability the service owns — the domain "
        "language a product manager would use, not implementation detail. No preamble, "
        "no markdown, just the sentence.",
        "",
        f"Service: {service}",
    ]
    if routes:
        lines.append("Routes: " + "; ".join(routes[:20]))
    if symbols:
        lines.append("Symbols: " + ", ".join(symbols[:30]))
    if doc:
        lines.append("README:\n" + doc[:1500])
    return "\n".join(lines)


class LLMSummarizer:
    """A ``Summarizer`` that asks an LLM for a one-line capability sentence per
    service. Opt-in enrichment — the offline default stays no summarizer. ``caller``
    is injectable (defaults to ``llm._call_llm``) so the wiring is unit-testable
    without an API key. Any failure returns "" (build_profiles also guards), so a
    flaky backend degrades to the deterministic profile rather than breaking."""

    def __init__(self, *, backend: str = "claude", model: str | None = None,
                 max_tokens: int = 120, caller: Callable[[str], str] | None = None):
        self.backend = backend
        self.model = model
        self.max_tokens = max_tokens
        self._caller = caller

    def _call(self, prompt: str) -> str:
        if self._caller is not None:
            return self._caller(prompt)
        from graphify.llm import _call_llm
        return _call_llm(prompt, backend=self.backend, model=self.model, max_tokens=self.max_tokens)

    def __call__(self, service: str, routes: list[str], symbols: list[str], doc: str) -> str:
        text = self._call(_summary_prompt(service, routes, symbols, doc)) or ""
        # collapse to one tidy line and bound the length that lands in the profile
        return " ".join(text.split())[:280]


def build_profiles(
    nodes: list[dict],
    *,
    root: str | Path | None = None,
    service_docs: dict[str, str] | None = None,
    summarizer: Summarizer | None = None,
) -> list[ServiceProfile]:
    """Group graph ``nodes`` into per-service responsibility profiles.

    ``service_docs`` maps a service name → its README/doc text (see
    ``load_service_docs``). ``summarizer`` optionally authors a one-line capability
    sentence per service that is folded into the retrieval text. Deterministic:
    profiles come back sorted by service name.
    """
    service_docs = service_docs or {}
    groups: dict[str, list[dict]] = defaultdict(list)
    for n in nodes:
        svc = service_of(n, root=root)
        if svc:
            groups[svc].append(n)

    profiles: list[ServiceProfile] = []
    for svc in sorted(groups):
        routes: list[str] = []
        symbols: list[str] = []
        files: set[str] = set()
        entry: set[str] = set()
        node_ids: list[str] = []
        for n in groups[svc]:
            node_ids.append(str(n.get("id")))
            kind = str(n.get("kind") or "").lower()
            label = (n.get("label") or "").strip()
            meta = n.get("metadata") or {}
            bn = _basename(n.get("source_file") or "")
            if bn:
                files.add(bn)
            if kind in _ROUTE_KINDS:
                entry.add("route")
                routes.append(label or f"{meta.get('method', '')} {meta.get('path', '')}".strip())
            elif kind in _ENTRYPOINT_KINDS:
                entry.add(kind)
                if label:
                    symbols.append(label)
            elif kind in _SYMBOL_KINDS:
                if label:
                    symbols.append(label)
        doc = service_docs.get(svc, "")
        summary = ""
        if summarizer is not None:
            try:
                summary = summarizer(svc, routes, symbols, doc) or ""
            except Exception:  # a summarizer must never break profile building
                summary = ""
        prof = ServiceProfile(
            service=svc,
            routes=sorted(dict.fromkeys(routes)),
            entrypoints=sorted(entry),
            symbols=sorted(dict.fromkeys(symbols)),
            files=sorted(files),
            doc=doc,
            summary=summary,
            node_ids=node_ids,
        )
        prof.text = _profile_text(prof)
        prof.tokens = tokenize(prof.text)
        profiles.append(prof)
    return profiles


# ── ranking (PRD prose → responsible services) ────────────────────────────────


@dataclass
class ServiceMatch:
    service: str
    score: float
    matched: str          # "lexical" | "vector" | "both"
    evidence: list[str]   # routes/symbols/doc-sentences the query hit


_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def _best_doc_sentence(doc: str, qset: set[str]) -> str | None:
    """The README sentence that shares the most distinct query tokens — the
    responsibility language that actually matched, so the "why" surfaces the doc
    even though routes/symbols didn't."""
    best, best_hits = None, 0
    for sent in _SENT_SPLIT.split(doc or ""):
        s = sent.strip()
        if not s:
            continue
        hits = len(qset & set(tokenize(s)))
        if hits > best_hits:
            best, best_hits = " ".join(s.split()), hits
    return best if best_hits else None


def _evidence(p: ServiceProfile, qset: set[str], *, limit: int = 5) -> list[str]:
    """The "why this service" trail: routes/symbols whose tokens the query hit,
    plus the single best-matching README sentence — so a rank is explainable
    rather than a black box."""
    ev: list[str] = []
    for item in [*p.routes, *p.symbols]:
        if item in ev:
            continue
        if qset & set(tokenize(item)):
            ev.append(item)
        if len(ev) >= limit:
            break
    sent = _best_doc_sentence(p.doc, qset)
    if sent and len(ev) < limit:
        ev.append(f"doc: {sent}")
    return ev


def rank_services(
    query: str,
    profiles: list[ServiceProfile],
    *,
    embedder: EmbeddingProvider | None = None,
    top_n: int = 10,
) -> list[ServiceMatch]:
    """Rank services by how likely a PRD ``query`` requires changing them.

    Lexical-only (BM25 over profile documents) when ``embedder`` is None —
    deterministic and offline; the reported score is the raw BM25 magnitude so
    the separation between services is meaningful. With an ``embedder`` a vector
    ranking of the same profile texts is fused in (RRF) for paraphrase recall and
    the score is the RRF score. Only services with a non-zero signal are returned
    (a service the PRD gives no evidence for is not guessed at).
    """
    if not profiles:
        return []
    qtok = tokenize(query)
    qset = set(qtok)

    chunks = [
        Chunk(symbol_id=p.service, name=p.service, path="", kind="service",
              text=p.text, tokens=p.tokens)
        for p in profiles
    ]
    lex_ranked = BM25(chunks).rank(qtok)
    lex_scores = {sid: sc for sc, sid in lex_ranked}
    lex_ids = [sid for _s, sid in lex_ranked]

    vec_ids: list[str] = []
    if embedder is not None:
        qv = embedder.embed([query])[0]
        cvs = embedder.embed([p.text for p in profiles])
        sims = sorted(
            ((cosine(qv, cvs[i]), profiles[i].service) for i in range(len(profiles))),
            key=lambda x: (-x[0], x[1]),
        )
        vec_ids = [svc for sc, svc in sims if sc > 0]
        # Fuse lexical + vector rankings; RRF is rank-based so the incomparable
        # BM25 and cosine scales don't need normalising.
        scored = reciprocal_rank_fusion([lex_ids, vec_ids])
    else:
        # Single ranking — RRF would flatten every service to ~1/(60+rank); report
        # the real BM25 magnitude instead so the margins are interpretable.
        scored = [(sid, lex_scores[sid]) for sid in lex_ids]

    by_svc = {p.service: p for p in profiles}
    lex_set, vec_set = set(lex_ids), set(vec_ids)
    out: list[ServiceMatch] = []
    for svc, score in scored:
        p = by_svc.get(svc)
        if p is None:
            continue
        matched = ("both" if svc in lex_set and svc in vec_set
                   else "vector" if svc in vec_set else "lexical")
        out.append(ServiceMatch(svc, round(score, 5), matched, _evidence(p, qset)))
        if len(out) >= top_n:
            break
    return out


# ── doc loading (the one I/O helper) ──────────────────────────────────────────

_README_NAMES = ("README.md", "README.rst", "README.txt", "README")


def load_service_docs(
    root: str | Path,
    *,
    filenames: tuple[str, ...] = _README_NAMES,
    max_chars: int = 20_000,
) -> dict[str, str]:
    """Read a README (first match of ``filenames``) from each immediate
    sub-directory of ``root``, keyed by that directory name — the doc arm of a
    service's responsibility profile. Missing/oversized files are skipped, not
    fatal; content is capped at ``max_chars`` to bound retrieval-text size."""
    root = Path(root)
    docs: dict[str, str] = {}
    if not root.is_dir():
        return docs
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        for fn in filenames:
            f = d / fn
            if f.is_file():
                try:
                    docs[d.name] = f.read_text(encoding="utf-8", errors="replace")[:max_chars]
                except OSError:
                    pass
                break
    return docs
