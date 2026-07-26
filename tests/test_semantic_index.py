"""Tests for semantic_index — Stage 2 prose→seed retrieval (pure, offline)."""
from __future__ import annotations

import pytest

from graphify.semantic_index import (
    BM25,
    HashingEmbedder,
    chunk_extraction,
    cosine,
    get_embedder,
    reciprocal_rank_fusion,
    retrieve_seeds,
    tokenize,
)

# --- tokenization: identifier splitting is what bridges prose to code ---

def test_tokenize_splits_camel_and_snake():
    toks = tokenize("getUserById purge_stale_orders")
    for expected in ("get", "user", "id", "purge", "stale", "orders"):
        assert expected in toks
    assert "getuserbyid" in toks       # whole identifier kept too
    assert "by" not in toks            # dropped as a stopword


def test_tokenize_drops_stopwords_and_singletons():
    toks = tokenize("the a of x report")
    assert "report" in toks
    assert "the" not in toks and "of" not in toks and "x" not in toks


# --- chunks ---

def test_chunk_extraction_builds_one_chunk_per_labelled_node():
    ext = {"nodes": [
        {"id": "n1", "label": "rollupDaily()", "kind": "function",
         "source_file": "billing/report.py", "metadata": {"service": "billing"}},
        {"id": "n2", "label": "", "source_file": "x.py"},   # no label → skipped
    ]}
    chunks = chunk_extraction(ext, repo="r")
    assert [c.symbol_id for c in chunks] == ["n1"]
    c = chunks[0]
    assert "billing" in c.tokens and "rollup" in c.tokens and "daily" in c.tokens


# --- BM25 ---

def test_bm25_ranks_the_matching_symbol_first():
    ext = {"nodes": [
        {"id": "pay", "label": "PaymentController", "source_file": "checkout/pay.py"},
        {"id": "auth", "label": "AuthService", "source_file": "auth/svc.py"},
        {"id": "mail", "label": "MailSender", "source_file": "notify/mail.py"},
    ]}
    ranked = BM25(chunk_extraction(ext)).rank(tokenize("payment controller"))
    assert ranked and ranked[0][1] == "pay"
    assert "auth" not in {sid for _s, sid in ranked}   # no term overlap → not returned


# --- hashing embedder + fusion ---

def test_hashing_embedder_is_deterministic_and_normalized():
    e = HashingEmbedder(dim=64)
    v1 = e.embed(["order service"])[0]
    v2 = e.embed(["order service"])[0]
    assert v1 == v2                                   # reproducible across calls
    assert abs(sum(x * x for x in v1) - 1.0) < 1e-9   # L2-normalized
    assert cosine(v1, e.embed(["order service"])[0]) > 0.99


def test_reciprocal_rank_fusion_rewards_agreement():
    fused = dict(reciprocal_rank_fusion([["a", "b", "c"], ["b", "a", "d"]]))
    # 'a' and 'b' rank high in both → outrank 'c'/'d' seen once
    assert fused["a"] > fused["c"]
    assert fused["b"] > fused["d"]


# --- the end-to-end query ---

_GRAPH = {"nodes": [
    {"id": "purge", "label": "purgeStaleOrders()", "kind": "function",
     "source_file": "orders/cleanup.ts"},
    {"id": "inv", "label": "InventoryClient", "kind": "class",
     "source_file": "orders/InventoryClient.java"},
    {"id": "rollup", "label": "rollup_daily()", "kind": "function",
     "source_file": "billing/report_job.py"},
    {"id": "note", "label": "Nightly billing rollup cron job",
     "source_file": "billing/report_job.py"},
]}


def test_retrieve_seeds_lexical_matches_paraphrase_via_subtokens():
    hits = retrieve_seeds("clean up stale orders", chunk_extraction(_GRAPH), top_n=2)
    ids = [h.symbol_id for h in hits]
    assert "purge" in ids                      # purgeStaleOrders reached from prose
    assert all(h.matched == "lexical" for h in hits)


def test_retrieve_seeds_hybrid_tags_both():
    hits = retrieve_seeds("nightly billing rollup", chunk_extraction(_GRAPH),
                          embedder=HashingEmbedder(), top_n=3)
    top_ids = {h.symbol_id for h in hits}
    assert {"rollup", "note"} & top_ids
    assert any(h.matched == "both" for h in hits)


def test_retrieve_seeds_empty_query_and_corpus():
    assert retrieve_seeds("anything", []) == []
    assert retrieve_seeds("zzzznomatch", chunk_extraction(_GRAPH)) == []


# --- embedder factory: default offline, real providers gated on credentials ---

def test_get_embedder_defaults_to_hashing():
    assert isinstance(get_embedder(), HashingEmbedder)
    assert isinstance(get_embedder("hashing"), HashingEmbedder)


def test_get_embedder_unknown_name_raises():
    with pytest.raises(ValueError):
        get_embedder("word2vec9000")


def test_get_embedder_openai_requires_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError):
        get_embedder("openai")


def test_get_embedder_gemini_requires_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(ValueError):
        get_embedder("gemini")
