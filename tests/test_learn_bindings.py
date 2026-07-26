"""Tests for learn_bindings — LLM authors rules, engine executes them.

The LLM call is injected (``_caller=``) so these stay offline and deterministic;
what's under test is the grounding (harvest), the parse+validate gate, and
persistence — everything that must be correct regardless of the model.
"""
from __future__ import annotations

import json

from graphify.binding_rules import load_rules, run_bindings
from graphify.learn_bindings import (
    build_prompt,
    harvest_candidates,
    persist_rules,
    propose_rules,
)


def _write(d, name, text):
    (d / name).write_text(text)


# --- harvest: grounded in real, uncovered constructs ---

def test_harvest_finds_uncovered_annotations_only(tmp_path):
    svc = tmp_path / "svc"
    svc.mkdir()
    _write(svc, "a.py",
           "@scheduler.scheduled_job('cron', hour=1)\n"   # covered by a builtin
           "def covered():\n    return 1\n\n"
           "@sqs_listener('orders')\n"                     # NOT covered
           "def on_msg():\n    return 2\n\n"
           "@property\n"                                   # uncovered but no framework meaning
           "def value(self):\n    return 3\n")

    cands = {c.annotation for c in harvest_candidates(tmp_path, load_rules(tmp_path))}
    assert "sqs_listener" in cands            # surfaced for the LLM to judge
    assert "scheduled_job" not in cands       # already covered → not a candidate


def test_harvest_records_handler_and_samples(tmp_path):
    svc = tmp_path / "svc"
    svc.mkdir()
    _write(svc, "w.py", "@my_worker('q')\ndef do_work():\n    return 1\n")
    cands = harvest_candidates(tmp_path, load_rules(tmp_path))
    c = next(c for c in cands if c.annotation == "my_worker")
    assert c.samples[0]["handler"] == "do_work"
    assert c.lang == "python"


def test_build_prompt_includes_candidates_and_schema(tmp_path):
    svc = tmp_path / "svc"
    svc.mkdir()
    _write(svc, "w.py", "@my_worker('q')\ndef do_work():\n    return 1\n")
    cands = harvest_candidates(tmp_path, load_rules(tmp_path))
    prompt = build_prompt(cands)
    assert "@my_worker" in prompt and "do_work" in prompt
    assert "triggers" in prompt and "consumes" in prompt   # schema guidance


# --- propose: validate every LLM proposal; the good survive, the bad are dropped ---

def test_propose_accepts_valid_and_rejects_invalid(tmp_path):
    svc = tmp_path / "svc"
    svc.mkdir()
    _write(svc, "w.py", "@SqsListener('orders')\ndef on_msg():\n    return 1\n")
    cands = harvest_candidates(tmp_path, load_rules(tmp_path))

    fake_reply = "```json\n" + json.dumps([
        {   # valid
            "id": "event.sqs.listener", "category": "event", "provider": "sqs",
            "languages": ["python"], "pattern": r"^\s*@SqsListener\b",
            "relation": "consumes", "node_kind": "topic",
        },
        {   # invalid: unknown language
            "id": "bad.rule", "category": "event", "provider": "x",
            "languages": ["cobol"], "pattern": "@X", "relation": "consumes",
            "node_kind": "topic",
        },
    ]) + "\n```"

    accepted, rejected = propose_rules(cands, backend="claude", _caller=lambda _p: fake_reply)
    assert [r.id for r in accepted] == ["event.sqs.listener"]
    assert accepted[0].origin == "llm"
    assert len(rejected) == 1 and "cobol" in rejected[0][1]


def test_propose_handles_unparseable_reply(tmp_path):
    svc = tmp_path / "svc"
    svc.mkdir()
    _write(svc, "w.py", "@Foo()\ndef h():\n    return 1\n")
    cands = harvest_candidates(tmp_path, load_rules(tmp_path))
    accepted, rejected = propose_rules(cands, backend="claude", _caller=lambda _p: "sorry, no idea")
    assert accepted == []
    assert rejected and "parse" in rejected[0][1]


# --- persist → the engine picks it up (the full author→execute loop) ---

def test_persist_then_engine_executes_the_learned_rule(tmp_path):
    svc = tmp_path / "svc"
    svc.mkdir()
    _write(svc, "w.py", "@SqsListener('orders')\ndef on_msg():\n    return 1\n")
    cands = harvest_candidates(tmp_path, load_rules(tmp_path))
    fake_reply = json.dumps([{
        "id": "event.sqs.listener", "category": "event", "provider": "sqs",
        "languages": ["python"], "pattern": r"^\s*@SqsListener\b",
        "relation": "consumes", "node_kind": "topic",
    }])
    accepted, _ = propose_rules(cands, backend="claude", _caller=lambda _p: fake_reply)

    path = persist_rules(tmp_path, accepted)
    assert path.exists()
    on_disk = json.loads(path.read_text())["rules"]
    assert on_disk[0]["id"] == "event.sqs.listener"

    # the engine now detects the construct the LLM taught it — deterministically
    g = run_bindings(tmp_path)
    consumes = [e for e in g["edges"] if e["relation"] == "consumes"]
    assert len(consumes) == 1
    assert g["stats"]["by_provider"] == {"sqs": 1}


def test_persist_merges_and_dedups(tmp_path):
    from graphify.binding_rules import validate_rule
    r1 = validate_rule({"id": "a.one", "category": "event", "provider": "p",
                        "languages": ["python"], "pattern": r"^\s*@A\b",
                        "relation": "consumes", "node_kind": "event"}, origin="llm")
    r2 = validate_rule({"id": "a.one", "category": "event", "provider": "p2",
                        "languages": ["python"], "pattern": r"^\s*@A\b",
                        "relation": "consumes", "node_kind": "event"}, origin="llm")
    persist_rules(tmp_path, [r1])
    persist_rules(tmp_path, [r2])   # same id → replace, not duplicate
    rules = json.loads((tmp_path / ".graphify_binding_rules.json").read_text())["rules"]
    assert len(rules) == 1 and rules[0]["provider"] == "p2"
