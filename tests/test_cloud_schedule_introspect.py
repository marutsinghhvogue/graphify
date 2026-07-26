"""Tests for cloud_schedule_introspect — Tier-B (cloud/IaC) scheduler detection."""
from __future__ import annotations

from pathlib import Path

from graphify.cloud_schedule_introspect import cloud_schedule_graph
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


def test_serverless_links_the_named_handler():
    g = cloud_schedule_graph(FIXTURE)
    trig = [e for e in g["edges"] if e["relation"] == "triggers"]
    assert len(trig) == 1
    assert trig[0]["target"] == "src/jobs.rollup_daily"   # handler it names


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
