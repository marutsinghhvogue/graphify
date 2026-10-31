"""Tests for service_profiles — Stage 1: PRD prose → responsible services.

Exercises the responsibility layer end-to-end on the xservice fixture: build a
per-service profile from the cross-service contract graph + READMEs, then assert
that a PRD written in business language ranks the correct service on top — the
coarse "which services must change" cut that precedes symbol-level seed
discovery and blast radius.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from graphify.contract_introspect import cross_service_graph
from graphify.semantic_index import HashingEmbedder
from graphify.service_profiles import (
    LLMSummarizer,
    ServiceProfile,
    build_profiles,
    load_service_docs,
    rank_services,
    service_of,
)

FIXTURE = Path(__file__).parent / "fixtures" / "xservice"


@pytest.fixture(scope="module")
def profiles() -> list[ServiceProfile]:
    g = cross_service_graph(FIXTURE)
    docs = load_service_docs(FIXTURE)
    return build_profiles(g["nodes"], root=FIXTURE, service_docs=docs)


# --- profile assembly ---------------------------------------------------------

def test_one_profile_per_service(profiles):
    assert {p.service for p in profiles} == {"user_service", "order_service", "billing_service"}


def test_profile_carries_routes_entrypoints_and_doc(profiles):
    by = {p.service: p for p in profiles}
    user = by["user_service"]
    assert user.entrypoints == ["route"]
    assert "GET /users/{}" in user.routes and "POST /users" in user.routes
    assert user.doc  # README folded in
    # doc vocabulary reaches the retrieval text even when routes are terse
    assert "registration" in user.text


def test_load_service_docs_reads_every_service():
    docs = load_service_docs(FIXTURE)
    assert set(docs) == {"user_service", "order_service", "billing_service"}
    assert "invoice" in docs["billing_service"].lower()


# --- service identity resolution ----------------------------------------------

def test_service_of_prefers_metadata():
    node = {"id": "n", "source_file": "/anything/at/all/x.py",
            "metadata": {"service": "billing_service"}}
    assert service_of(node, root="/some/root") == "billing_service"


def test_service_of_path_fallback_immediate_subdir():
    node = {"id": "n", "source_file": "repos/user_service/app/main.py", "metadata": {}}
    assert service_of(node, root="repos") == "user_service"


def test_service_of_returns_none_for_file_directly_under_root():
    node = {"id": "n", "source_file": "repos/top.py", "metadata": {}}
    assert service_of(node, root="repos") is None


def test_service_of_falls_back_to_repo_metadata():
    # multi-repo / PG-exported graph: each repo is a service, no metadata.service
    node = {"id": "n", "source_file": "src/x.go", "metadata": {"repo": "payments-svc"}}
    assert service_of(node) == "payments-svc"


def test_service_metadata_wins_over_repo():
    node = {"id": "n", "metadata": {"service": "billing", "repo": "monorepo"}}
    assert service_of(node) == "billing"


def test_profiles_capture_broadened_symbol_kinds():
    # a full polyglot AST graph contributes structs/interfaces/traits, not just fns
    nodes = [
        {"id": "a", "label": "PaymentGateway", "kind": "interface",
         "source_file": "pay/gw.go", "metadata": {"repo": "pay"}},
        {"id": "b", "label": "LedgerEntry", "kind": "struct",
         "source_file": "pay/ledger.go", "metadata": {"repo": "pay"}},
    ]
    profiles = build_profiles(nodes)
    assert profiles[0].service == "pay"
    assert "PaymentGateway" in profiles[0].symbols and "LedgerEntry" in profiles[0].symbols
    ranked = rank_services("change the payment gateway ledger entry", profiles)
    assert ranked and ranked[0].service == "pay"


# --- PRD → service ranking ----------------------------------------------------

@pytest.mark.parametrize(
    "prd, expected",
    [
        ("add sales tax to the amount a customer is charged on their bill", "billing_service"),
        ("issue a refund to the customer", "billing_service"),
        ("send a reminder for overdue invoices", "billing_service"),
        ("let a customer sign up and edit their profile", "user_service"),
        ("deactivate a user account", "user_service"),
        ("allow cancelling an order during checkout", "order_service"),
        ("track an order through fulfilment", "order_service"),
    ],
)
def test_prd_ranks_correct_service_top(profiles, prd, expected):
    ranked = rank_services(prd, profiles, top_n=3)
    assert ranked, f"no service matched: {prd!r}"
    assert ranked[0].service == expected


def test_top_score_separates_from_runner_up(profiles):
    # A clearly billing-domain PRD must win decisively, not by a rounding margin —
    # guards the regression where RRF over a single ranking flattens all scores.
    ranked = rank_services(
        "add sales tax to the amount a customer is charged on their bill", profiles
    )
    assert ranked[0].service == "billing_service"
    assert ranked[0].score > 2 * ranked[1].score


def test_match_carries_explainable_evidence(profiles):
    ranked = rank_services("issue a refund to the customer", profiles)
    top = ranked[0]
    assert top.evidence, "expected a why-this-service trail"
    # the README responsibility language is surfaced, not just routes
    assert any(e.startswith("doc:") for e in top.evidence)


def test_unrelated_prd_matches_nothing(profiles):
    # No responsibility overlap → no guess (recall arm is the embedder, not noise).
    assert rank_services("provision kubernetes helm chart yaml manifests", profiles) == []


def test_hybrid_embedder_runs_and_tags_matches(profiles):
    ranked = rank_services(
        "charge the customer for their order", profiles,
        embedder=HashingEmbedder(), top_n=3,
    )
    assert ranked
    assert all(m.matched in ("lexical", "vector", "both") for m in ranked)


def test_empty_profiles_returns_empty():
    assert rank_services("anything", []) == []


# --- LLM summarizer (opt-in enrichment) ---------------------------------------

def _nodes_for(service: str) -> list[dict]:
    return [
        {"id": f"{service}_ep", "label": "GET /widgets/{}", "kind": "route",
         "source_file": f"{service}/w.py", "metadata": {"service": service}},
        {"id": f"{service}_fn", "label": "getWidget()", "kind": "function",
         "source_file": f"{service}/w.py", "metadata": {"service": service}},
    ]


def test_llm_summarizer_folds_capability_into_matching():
    # The symbols say only "widget"; the authored summary adds domain language the
    # PRD uses ("subscription billing"), so a paraphrased PRD now reaches the service.
    summ = LLMSummarizer(caller=lambda p: "Owns subscription billing and dunning for widgets.")
    profiles = build_profiles(_nodes_for("svc_a"), summarizer=summ)
    assert profiles[0].summary.startswith("Owns subscription billing")
    assert "subscription" in profiles[0].text
    ranked = rank_services("change how dunning works for a subscription", profiles)
    assert ranked and ranked[0].service == "svc_a"


def test_llm_summarizer_prompt_carries_service_signal():
    seen: dict[str, str] = {}

    def _caller(prompt: str) -> str:
        seen["prompt"] = prompt
        return "capability sentence"

    build_profiles(_nodes_for("svc_a"), summarizer=LLMSummarizer(caller=_caller))
    assert "svc_a" in seen["prompt"]
    assert "GET /widgets/{}" in seen["prompt"]


def test_summarizer_failure_degrades_gracefully():
    def _boom(_prompt: str) -> str:
        raise RuntimeError("backend down")

    profiles = build_profiles(_nodes_for("svc_a"), summarizer=LLMSummarizer(caller=_boom))
    # build_profiles swallows the failure — the deterministic profile still works
    assert profiles[0].summary == ""
    assert profiles[0].text
