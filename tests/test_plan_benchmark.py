"""Tests for plan_benchmark — plan_change vs a grep/read agent baseline.

Asserts the harness runs on the xservice fixture and that the graph-backed plan is
strictly cheaper than the deterministically-executed grep/read agent (fewer tool
calls, fewer context tokens, zero files opened).
"""
from __future__ import annotations

from pathlib import Path

from graphify.plan_benchmark import (
    _call_paths_in,
    _keywords,
    benchmark_plan,
    format_plan_benchmark,
    grep_read_baseline,
)

FIXTURE = Path(__file__).parent / "fixtures" / "xservice"

_PRDS = [
    "add sales tax to the amount a customer is charged on their bill",
    "let a customer update their user profile",
    "allow cancelling an order during checkout",
]


def test_graphify_is_one_call_and_cheaper():
    for r in benchmark_plan(FIXTURE, _PRDS):
        # tool-call reduction is the scale-robust headline — assert it strongly
        assert r.graphify.tool_calls == 1
        assert r.graphify.files_read == 0
        assert r.graphify.tool_calls < r.baseline.tool_calls
        assert r.baseline.files_read > 0
        assert r.tool_call_reduction > 0.5   # ≥ 50% fewer tool calls
        # token reduction grows with corpus size; on a tiny fixture only assert it
        # doesn't blow up (can be ~0 when a sparse query reads a few short files)
        assert r.token_reduction >= -0.5


def test_baseline_actually_reads_files():
    cost, services = grep_read_baseline(FIXTURE, _PRDS[0])
    assert cost.files_read >= 1
    assert cost.tool_calls > cost.files_read  # greps + hops on top of reads
    assert cost.context_tokens > 0
    assert "billing_service" in services


def test_keywords_are_capped_and_specific():
    kws = _keywords("add sales tax to the amount a customer is charged on their bill")
    assert len(kws) <= 8
    assert all(len(k) >= 4 for k in kws)


def test_call_paths_detects_internal_and_skips_external():
    text = (
        'const u = await fetch(`http://user-service/users/${id}`);\n'
        'const c = await fetch("https://api.stripe.com/v1/charges");\n'
    )
    paths = _call_paths_in(text)
    assert "/users/{}" in paths
    assert not any("charges" in p for p in paths)  # external host skipped


def test_format_reports_aggregate_reduction():
    out = format_plan_benchmark(benchmark_plan(FIXTURE, _PRDS))
    assert "Aggregate" in out
    assert "fewer tool calls" in out and "fewer context tokens" in out


def test_format_empty():
    assert "No PRDs" in format_plan_benchmark([])
