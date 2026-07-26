"""Tests for binding_rules — the declarative, pluggable framework-construct engine."""
from __future__ import annotations

import json

import pytest

from graphify.binding_rules import (
    BUILTIN_RULES,
    EXTERNAL_RULES_FILENAME,
    RuleError,
    load_rules,
    run_bindings,
    validate_rule,
)
from graphify.contract_introspect import reconcile_contract
from graphify.validate import validate_extraction


def _write(dirpath, name, text):
    (dirpath / name).write_text(text)


# --- built-in detection: schedulers + events, one engine ---

def test_builtin_detects_scheduler_and_event_categories(tmp_path):
    svc = tmp_path / "svc"
    svc.mkdir()
    _write(svc, "jobs.py",
           "@scheduler.scheduled_job('cron', hour=2)\n"
           "def rollup():\n    return 1\n")
    _write(svc, "Listener.java",
           "@KafkaListener(topics = \"orders\")\n"
           "public void onOrder(String p) {\n    }\n")

    g = run_bindings(tmp_path)
    validate_extraction({"nodes": g["nodes"], "edges": g["edges"]})
    assert g["stats"]["by_category"] == {"scheduler": 1, "event": 1}
    rels = {e["relation"] for e in g["edges"]}
    assert {"triggers", "consumes"} <= rels
    assert all(e["confidence"] == "EXTRACTED" for e in g["edges"])


def test_category_filter_runs_only_requested(tmp_path):
    svc = tmp_path / "svc"
    svc.mkdir()
    _write(svc, "Listener.java",
           "@KafkaListener(topics = \"orders\")\n"
           "public void onOrder(String p) {\n    }\n")
    # scheduler-only run must not pick up the Kafka listener
    g = run_bindings(tmp_path, categories=["scheduler"])
    assert g["stats"]["bindings"] == 0


# --- pluggability: a new framework via an external JSON rule, no code change ---

def test_external_rule_adds_new_framework_with_no_code(tmp_path):
    svc = tmp_path / "svc"
    svc.mkdir()
    # a framework graphify has no built-in rule for
    _write(svc, "tasks.py",
           "@hangfire_recurring('0 4 * * *')\n"
           "def nightly_sync():\n    return 1\n")

    # without a rule: nothing
    assert run_bindings(tmp_path)["stats"]["bindings"] == 0

    # drop a rule row into the plug-in file — no code change
    rule = {
        "id": "scheduler.hangfire.recurring", "category": "scheduler",
        "provider": "hangfire", "languages": ["python"],
        "pattern": r"^\s*@hangfire_recurring\b", "relation": "triggers",
        "node_kind": "schedule", "description": "custom",
    }
    (tmp_path / EXTERNAL_RULES_FILENAME).write_text(json.dumps({"rules": [rule]}))

    g = run_bindings(tmp_path)
    assert g["stats"]["by_category"] == {"scheduler": 1}
    assert g["stats"]["by_provider"] == {"hangfire": 1}
    trig = [e for e in g["edges"] if e["relation"] == "triggers"]
    assert len(trig) == 1
    # the loaded rule is tagged external provenance
    ext = [r for r in load_rules(tmp_path) if r.origin == "external"]
    assert [r.id for r in ext] == ["scheduler.hangfire.recurring"]


def test_external_rules_survive_a_bad_row(tmp_path):
    good = {"id": "x.good", "category": "scheduler", "provider": "p",
            "languages": ["python"], "pattern": r"^\s*@good\b",
            "relation": "triggers", "node_kind": "schedule"}
    bad = {"id": "x.bad", "category": "scheduler"}  # missing fields
    (tmp_path / EXTERNAL_RULES_FILENAME).write_text(json.dumps([good, bad]))
    ids = {r.id for r in load_rules(tmp_path)}
    assert "x.good" in ids          # the good one loads
    assert "x.bad" not in ids       # the bad one is skipped, not fatal


def test_external_rule_cannot_shadow_a_builtin_id(tmp_path):
    dupe = {"id": BUILTIN_RULES[0].id, "category": "scheduler", "provider": "evil",
            "languages": ["python"], "pattern": r"^\s*@evil\b",
            "relation": "triggers", "node_kind": "schedule"}
    (tmp_path / EXTERNAL_RULES_FILENAME).write_text(json.dumps([dupe]))
    providers = {r.provider for r in load_rules(tmp_path) if r.id == BUILTIN_RULES[0].id}
    assert "evil" not in providers  # builtin wins, dup skipped


# --- validation: bad LLM/user rules fail loud ---

@pytest.mark.parametrize("bad", [
    {"id": "a", "category": "c", "provider": "p", "languages": ["klingon"],
     "pattern": "@X", "relation": "does", "node_kind": "t"},                 # bad lang
    {"id": "a", "category": "c", "provider": "p", "languages": ["python"],
     "pattern": "(", "relation": "does", "node_kind": "t"},                  # bad regex
    {"id": "a", "category": "c", "provider": "p", "languages": ["python"],
     "pattern": "@X", "relation": "Does It", "node_kind": "t"},              # bad relation
    {"id": "a", "category": "c", "provider": "p", "languages": ["python"],
     "pattern": "@X", "relation": "does", "node_kind": "t",
     "resolution": "telepathy"},                                            # bad resolution
    {"id": "a", "category": "c", "provider": "p", "languages": ["python"],
     "pattern": "@X", "relation": "does", "node_kind": "t",
     "confidence": "MAYBE"},                                                # bad confidence
    {"category": "c", "provider": "p", "languages": ["python"],
     "pattern": "@X", "relation": "does", "node_kind": "t"},                 # missing id
])
def test_validate_rule_rejects(bad):
    with pytest.raises(RuleError):
        validate_rule(bad)


def test_validate_rule_accepts_minimal_valid():
    r = validate_rule({
        "id": "ok", "category": "event", "provider": "nats", "languages": "python",
        "pattern": r"^\s*@subscribe\b", "relation": "consumes", "node_kind": "topic",
    })
    assert r.languages == ("python",) and r.confidence == "EXTRACTED"


# --- reconciliation: event handlers fold onto AST like schedulers ---

def test_event_handlers_reconcile_onto_ast_nodes(tmp_path):
    svc = tmp_path / "svc"
    svc.mkdir()
    _write(svc, "Listener.java",
           "@KafkaListener(topics = \"orders\")\n"
           "public void onOrder(String p) {\n    }\n")
    raw = run_bindings(tmp_path)
    base = {"nodes": [
        {"id": "ast_on_order", "label": ".onOrder()",
         "source_file": str(svc / "Listener.java"), "source_location": "L2"},
    ], "edges": []}
    out = reconcile_contract(base, raw)
    consumes = [e for e in out["edges"] if e["relation"] == "consumes"]
    assert consumes and consumes[0]["target"] == "ast_on_order"
    # the topic node has no AST twin → kept as new
    assert any(n.get("kind") == "topic" for n in out["nodes"])
