"""semantic_index.py — Stage 2: map PRD prose to the code symbols it touches.

The weakest link of the PRD→plan pipeline: turning a requirement written in
English into the *seed symbols* that must change. Exact-name resolution and
hand-written aliases don't reach it (a doc says "checkout", the code says
`PaymentController`); keyword BFS misses paraphrase. This module is the automatic
seed-discovery arm.

Design:
- **Works offline, today.** A BM25 lexical retriever over rich per-symbol chunks
  (name split into subtokens — ``getUserById`` → get/user/by/id — plus path,
  kind, service, doc sentences). No Postgres, no embedding API, deterministic.
  Already beats keyword BFS because it ranks and splits identifiers.
- **Upgrades to hybrid.** A pluggable ``EmbeddingProvider`` adds a vector arm for
  paraphrase; ``reciprocal_rank_fusion`` combines the lexical and vector rankings.
  The default ``HashingEmbedder`` is deterministic/offline (a weak semantic
  signal, good for tests and zero-cost); plug OpenAI/Gemini/Voyage for real
  paraphrase recall.

Pure and I/O-free — unit-testable end to end. The Postgres ``code_chunks`` table
(pgvector + FTS, keyed to ``symbols.symbol_id``) is the persistence/scale path
that stores these same chunks + embeddings; this module produces them.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# ── tokenization ──────────────────────────────────────────────────────────────

# Minimal stoplist — prose glue that would otherwise flood identifier matches.
_STOP = frozenset(
    "a an the of to for in on and or is are be as by with at from this that it "
    "we our you your i it its into via def function class self return".split()
)
_WORD = re.compile(r"[A-Za-z0-9]+")
_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Lowercased tokens with identifier splitting: each word contributes itself
    plus its camelCase / snake_case sub-parts, so prose ``get user`` matches code
    ``getUserById``. Stopwords dropped."""
    out: list[str] = []
    for word in _WORD.findall(text or ""):
        low = word.lower()
        if low not in _STOP and len(low) > 1:
            out.append(low)
        for part in _CAMEL.findall(word):
            pl = part.lower()
            if pl != low and pl not in _STOP and len(pl) > 1:
                out.append(pl)
    return out


# ── chunks ────────────────────────────────────────────────────────────────────


@dataclass
class Chunk:
    symbol_id: str
    name: str
    path: str
    kind: str
    text: str
    repo: str = ""
    tokens: list[str] = field(default_factory=list)


def _path_terms(source_file: str) -> str:
    parts = (source_file or "").replace("\\", "/").split("/")
    return " ".join(parts[-2:])  # file + parent dir


def chunk_nodes(nodes: list[dict], repo: str = "") -> list[Chunk]:
    """Build a retrieval chunk per graph node from its label, path, kind, service,
    and any expr/summary metadata. Nodes with an empty label are skipped."""
    chunks: list[Chunk] = []
    for n in nodes:
        label = (n.get("label") or "").strip()
        if not label:
            continue
        sf = n.get("source_file") or ""
        kind = str(n.get("kind") or "")
        meta = n.get("metadata") or {}
        extra = " ".join(
            str(meta.get(k) or "") for k in ("service", "expr", "summary", "provider")
        )
        text = " ".join(t for t in (label, _path_terms(sf), kind, extra) if t).strip()
        chunks.append(Chunk(
            symbol_id=str(n.get("id")), name=label, path=sf, kind=kind,
            text=text, repo=repo, tokens=tokenize(text),
        ))
    return chunks


def chunk_extraction(extraction: dict[str, Any], repo: str = "") -> list[Chunk]:
    return chunk_nodes(extraction.get("nodes", []), repo)


# ── lexical retrieval (BM25) ──────────────────────────────────────────────────


class BM25:
    """Okapi BM25 over chunk token lists — pure, in-memory, deterministic."""

    def __init__(self, chunks: list[Chunk], *, k1: float = 1.5, b: float = 0.75):
        self.chunks = chunks
        self.k1, self.b = k1, b
        self.df: Counter = Counter()
        self.doclen = [len(c.tokens) for c in chunks]
        for c in chunks:
            self.df.update(set(c.tokens))
        self.N = len(chunks) or 1
        self.avgdl = (sum(self.doclen) / self.N) if self.doclen else 1.0

    def _idf(self, term: str) -> float:
        n = self.df.get(term, 0)
        return math.log(1 + (self.N - n + 0.5) / (n + 0.5))

    def rank(self, query_tokens: list[str]) -> list[tuple[float, str]]:
        scored: list[tuple[float, str]] = []
        for i, c in enumerate(self.chunks):
            tf = Counter(c.tokens)
            dl = self.doclen[i] or 1
            s = 0.0
            for t in query_tokens:
                f = tf.get(t)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
                s += self._idf(t) * (f * (self.k1 + 1)) / denom
            if s > 0:
                scored.append((s, c.symbol_id))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return scored


# ── vector retrieval (pluggable) ──────────────────────────────────────────────


@runtime_checkable
class EmbeddingProvider(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    """Deterministic, offline embeddings via hashed-token bag-of-features, L2
    normalized. A weak semantic signal (mostly lexical) but reproducible and
    zero-cost — the default so hybrid retrieval works with no external API. Swap
    for a real provider (OpenAI/Gemini/Voyage) for true paraphrase recall."""

    def __init__(self, dim: int = 256):
        self.dim = dim

    def _hash(self, token: str) -> int:
        return int.from_bytes(hashlib.md5(token.encode("utf-8")).digest()[:4], "big")

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            v = [0.0] * self.dim
            for tok in tokenize(t):
                v[self._hash(tok) % self.dim] += 1.0
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / norm for x in v])
        return out


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))  # inputs are L2-normalized


class OpenAIEmbedder:
    """Real embeddings via the OpenAI API (default ``text-embedding-3-small``).
    Lazy client; batched. Needs ``OPENAI_API_KEY`` and the ``openai`` package."""

    def __init__(self, model: str = "text-embedding-3-small", *,
                 api_key: str | None = None, dim: int = 1536, batch: int = 256):
        self.model, self.dim, self.batch = model, dim, batch
        self._key = api_key or os.environ.get("OPENAI_API_KEY")
        if not self._key:
            raise ValueError("OPENAI_API_KEY is not set — required for OpenAI embeddings")

    def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise ImportError("the 'openai' package is required: pip install openai") from exc
        client = OpenAI(api_key=self._key)
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            resp = client.embeddings.create(model=self.model, input=texts[i:i + self.batch])
            out.extend(d.embedding for d in resp.data)
        return out


class GeminiEmbedder:
    """Real embeddings via the Google Gemini API. Needs ``GEMINI_API_KEY`` (or
    ``GOOGLE_API_KEY``) and the ``google-generativeai`` package."""

    def __init__(self, model: str = "models/text-embedding-004", *,
                 api_key: str | None = None, dim: int = 768):
        self.model, self.dim = model, dim
        self._key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not self._key:
            raise ValueError("GEMINI_API_KEY (or GOOGLE_API_KEY) is not set — required for Gemini embeddings")

    def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            import google.generativeai as genai
        except ImportError as exc:  # pragma: no cover
            raise ImportError("the 'google-generativeai' package is required") from exc
        genai.configure(api_key=self._key)
        out: list[list[float]] = []
        for t in texts:
            r = genai.embed_content(model=self.model, content=t)
            out.append(r["embedding"])
        return out


def get_embedder(name: str | None = None, **kwargs: Any) -> EmbeddingProvider:
    """Resolve an embedder by name. ``hashing`` (default, offline/deterministic),
    ``openai``, or ``gemini``. Raises ``ValueError`` for an unknown name or a
    provider whose credentials aren't configured — never silently degrades."""
    key = (name or "hashing").lower()
    if key in ("hashing", "local", "offline", "none"):
        return HashingEmbedder(**kwargs)
    if key == "openai":
        return OpenAIEmbedder(**kwargs)
    if key in ("gemini", "google"):
        return GeminiEmbedder(**kwargs)
    raise ValueError(f"unknown embedder {name!r}; choose one of: hashing, openai, gemini")


# ── fusion + the query ────────────────────────────────────────────────────────


def reciprocal_rank_fusion(rankings: list[list[str]], k: int = 60) -> list[tuple[str, float]]:
    """Combine ranked id lists (lexical, vector, …) by Reciprocal Rank Fusion —
    rank-based, so incomparable score scales don't need normalizing."""
    score: dict[str, float] = defaultdict(float)
    for ranking in rankings:
        for rank, sid in enumerate(ranking):
            score[sid] += 1.0 / (k + rank + 1)
    return sorted(score.items(), key=lambda x: (-x[1], x[0]))


@dataclass
class SeedHit:
    symbol_id: str
    name: str
    path: str
    kind: str
    score: float
    matched: str   # "lexical" | "vector" | "both"


def retrieve_seeds(
    query: str,
    chunks: list[Chunk],
    *,
    embedder: EmbeddingProvider | None = None,
    top_n: int = 10,
) -> list[SeedHit]:
    """Rank ``chunks`` against a prose ``query`` and return the top seed symbols.

    Lexical-only (BM25) when ``embedder`` is None — deterministic, offline, and
    already strong for identifier/prose overlap. With an ``embedder`` the vector
    ranking is fused in (RRF) for paraphrase recall.
    """
    if not chunks:
        return []
    qtok = tokenize(query)
    lex_ranked = BM25(chunks).rank(qtok)
    lex_ids = [sid for _s, sid in lex_ranked]
    rankings: list[list[str]] = [lex_ids]

    vec_ids: list[str] = []
    if embedder is not None:
        pool = max(top_n * 5, 50)
        qv = embedder.embed([query])[0]
        cvs = embedder.embed([c.text for c in chunks])
        sims = sorted(
            ((cosine(qv, cvs[i]), c.symbol_id) for i, c in enumerate(chunks)),
            key=lambda x: (-x[0], x[1]),
        )
        vec_ids = [sid for sc, sid in sims if sc > 0][:pool]
        rankings.append(vec_ids)

    by_id = {c.symbol_id: c for c in chunks}
    lex_set, vec_set = set(lex_ids), set(vec_ids)
    hits: list[SeedHit] = []
    for sid, score in reciprocal_rank_fusion(rankings):
        c = by_id.get(sid)
        if c is None:
            continue
        matched = ("both" if sid in lex_set and sid in vec_set
                   else "vector" if sid in vec_set else "lexical")
        hits.append(SeedHit(sid, c.name, c.path, c.kind, round(score, 5), matched))
        if len(hits) >= top_n:
            break
    return hits
