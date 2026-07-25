"""Tests for schedule_introspect — Tier-A (in-code) scheduler detection."""
from __future__ import annotations

from pathlib import Path

from graphify.contract_introspect import reconcile_contract
from graphify.schedule_introspect import schedule_graph
from graphify.validate import validate_extraction

FIXTURE = Path(__file__).parent / "fixtures" / "schedulers"


def test_fixture_detects_all_three_frameworks():
    g = schedule_graph(FIXTURE)
    assert g["stats"]["by_provider"] == {"apscheduler": 1, "nestjs": 2, "spring": 1}
    # every schedule binds a handler via an EXTRACTED 'triggers' edge
    trig = [e for e in g["edges"] if e["relation"] == "triggers"]
    assert len(trig) == g["stats"]["schedules"] == 4
    assert all(e["confidence"] == "EXTRACTED" for e in trig)


def test_schedule_nodes_carry_provider_and_expr():
    g = schedule_graph(FIXTURE)
    scheds = {n["id"]: n for n in g["nodes"] if n["kind"] == "schedule"}
    roll = next(n for n in scheds.values() if n["metadata"]["service"] == "billing")
    assert roll["metadata"]["provider"] == "apscheduler"
    assert roll["metadata"]["trigger"] == "cron"
    assert "hour=2" in roll["metadata"]["expr"]


def test_comment_mentioning_annotation_is_not_a_schedule(tmp_path):
    """A comment containing '@Cron'/'@Scheduled' must not create a false schedule —
    only a real line-leading decorator/annotation counts."""
    svc = tmp_path / "svc"
    svc.mkdir()
    (svc / "noise.py").write_text(
        "# this function is NOT scheduled — do not confuse @scheduled_job here\n"
        "def not_a_job():\n    return 1\n"
    )
    g = schedule_graph(tmp_path)
    assert g["stats"]["schedules"] == 0
    assert not any(e["relation"] == "triggers" for e in g["edges"])


def test_output_is_valid_extraction():
    g = schedule_graph(FIXTURE)
    # emits standard graphify {nodes, edges}
    validate_extraction({"nodes": g["nodes"], "edges": g["edges"]})


def test_reconcile_folds_schedule_handlers_onto_ast_nodes():
    """reconcile_contract is generic: it folds the schedule handler fn nodes onto
    AST nodes, keeps the 'schedule' nodes as new, and repoints 'triggers' edges."""
    sched = schedule_graph(FIXTURE)
    base = {"nodes": [
        {"id": "ast_rollup", "label": "rollup_daily()",
         "source_file": str(FIXTURE / "billing" / "report_job.py"),
         "source_location": "L7"},
    ], "edges": []}

    out = reconcile_contract(base, sched)
    # the billing handler folded onto the AST node; service stamped
    assert (base["nodes"][0].get("metadata") or {}).get("service") == "billing"
    # its 'triggers' edge now targets the AST id, not a svc_* island
    trig_to_ast = [
        e for e in out["edges"]
        if e["relation"] == "triggers" and e["target"] == "ast_rollup"
    ]
    assert len(trig_to_ast) == 1
    # schedule nodes are kept as new (kind != function)
    assert any(n.get("kind") == "schedule" for n in out["nodes"])
