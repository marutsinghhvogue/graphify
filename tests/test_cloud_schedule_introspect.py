"""Tests for cloud_schedule_introspect — Tier-B (cloud/IaC) scheduler detection."""
from __future__ import annotations

from pathlib import Path

from graphify.cloud_schedule_introspect import cloud_schedule_graph
from graphify.contract_introspect import reconcile_contract
from graphify.validate import validate_extraction

FIXTURE = Path(__file__).parent / "fixtures" / "cloudsched"


def test_detects_all_cloud_providers():
    g = cloud_schedule_graph(FIXTURE)
    assert g["stats"]["by_provider"] == {
        "aws": 1, "gcp": 1, "kubernetes": 1, "serverless": 1,
    }
    assert g["stats"]["schedules"] == 4


def test_schedule_nodes_are_inferred_with_cron_expr():
    g = cloud_schedule_graph(FIXTURE)
    scheds = {n["metadata"]["provider"]: n for n in g["nodes"] if n["kind"] == "schedule"}
    assert scheds["aws"]["metadata"]["expr"] == "cron(0 2 * * ? *)"
    assert scheds["gcp"]["metadata"]["expr"] == "0 8 * * *"
    assert scheds["kubernetes"]["metadata"]["expr"] == "0 5 * * *"
    # cloud config is less certain than an in-code decorator
    edges = g["edges"]
    assert all(e["confidence"] == "INFERRED" for e in edges)


def test_eventbridge_resolves_lambda_handler_via_reference_chain():
    """R1: rule → event_target → aws_lambda_function.handler is followed so the
    schedule's `triggers` edge targets the handler function (not a raw hint)."""
    g = cloud_schedule_graph(FIXTURE)
    aws = [e for e in g["edges"]
           if e["relation"] == "triggers" and e["metadata"]["provider"] == "aws"]
    assert len(aws) == 1
    assert aws[0]["target"] == "rollup_daily"                 # func from "jobs.rollup_daily"
    assert aws[0]["metadata"]["handler"] == "jobs.rollup_daily"
    assert aws[0]["metadata"]["resolved"] is True


def test_serverless_links_the_named_handler_function():
    g = cloud_schedule_graph(FIXTURE)
    sl = [e for e in g["edges"]
          if e["relation"] == "triggers" and e["metadata"]["provider"] == "serverless"]
    assert len(sl) == 1
    assert sl[0]["target"] == "rollup_daily"                  # bare func, resolvable
    assert sl[0]["metadata"]["handler"] == "src/jobs.rollup_daily"


def test_cloud_triggers_resolve_onto_ast_nodes():
    """R1 payoff: reconcile resolves the cloud handler targets onto real AST nodes,
    so blast_radius crosses infra→code."""
    g = cloud_schedule_graph(FIXTURE)
    base = {"nodes": [
        {"id": "ast_rollup_daily", "label": "rollup_daily()",
         "source_file": "infra/jobs.py", "source_location": "L1"},
    ], "edges": []}
    out = reconcile_contract(base, g)
    linked = [e for e in out["edges"]
              if e["relation"] == "triggers" and e["target"] == "ast_rollup_daily"]
    assert linked, "cloud schedule should link onto the AST handler node"
    assert out["reconciliation"]["type_targets_resolved"] >= 1


def test_output_is_valid_extraction():
    g = cloud_schedule_graph(FIXTURE)
    validate_extraction({"nodes": g["nodes"], "edges": g["edges"]})


def test_terraform_only_when_yaml_absent(tmp_path):
    infra = tmp_path / "infra"
    infra.mkdir()
    (infra / "s.tf").write_text(
        'resource "aws_scheduler_schedule" "x" {\n'
        '  schedule_expression = "rate(5 minutes)"\n'
        "}\n"
    )
    g = cloud_schedule_graph(tmp_path)
    assert g["stats"]["by_provider"] == {"aws": 1}
    assert g["nodes"][0]["metadata"]["expr"] == "rate(5 minutes)"
