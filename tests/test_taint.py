"""Tests for taint — source→sink analysis over the PDG."""
from __future__ import annotations

import json

import pytest

from graphify.taint import (
    TAINT_RULES_FILENAME,
    TaintRuleError,
    load_taint_rules,
    taint_scan,
    taint_source,
    validate_taint_rule,
)


def _f(src):
    return taint_source(src, path="v.py")["findings"]


def test_sql_injection_source_to_sink():
    fs = _f("def f(request, cursor):\n"
            "    uid = request.args.get('id')\n"
            "    q = 'SELECT ' + uid\n"
            "    cursor.execute(q)\n")
    assert len(fs) == 1
    assert fs[0].vuln == "sql_injection" and fs[0].category == "untrusted-input"
    assert fs[0].confidence == "INFERRED"
    lines = [p["line"] for p in fs[0].path]
    assert lines == [2, 3, 4]                       # source → intermediate → sink


def test_sanitizer_blocks_the_flow():
    # int() cleans the value even though the same stmt reads a source
    assert _f("def f(request, cursor):\n"
              "    uid = int(request.args.get('id'))\n"
              "    cursor.execute('SELECT ' + str(uid))\n") == []
    # escape() on the path blocks it too
    assert _f("def f(request, cursor):\n"
              "    uid = request.args.get('id')\n"
              "    safe = escape(uid)\n"
              "    cursor.execute('SELECT ' + safe)\n") == []


def test_no_finding_when_sink_does_not_use_tainted_value():
    assert _f("def f(request, cursor):\n"
              "    uid = request.args.get('id')\n"
              "    cursor.execute('SELECT 1')\n") == []


def test_command_injection():
    fs = _f("import os\n"
            "def f(request):\n"
            "    cmd = request.args.get('c')\n"
            "    os.system('ls ' + cmd)\n")
    assert len(fs) == 1 and fs[0].vuln == "command_injection"


def test_flows_to_edge_emitted():
    r = taint_source("def f(request, cursor):\n"
                     "    uid = request.args.get('id')\n"
                     "    cursor.execute(uid)\n", path="v.py")
    flows = [e for e in r["edges"] if e["relation"] == "flows_to"]
    assert len(flows) == 1
    assert flows[0]["metadata"]["vuln"] == "sql_injection"
    assert flows[0]["confidence"] == "INFERRED"


# --- rule catalog (rules-as-data) ---

def test_validate_taint_rule_rejects_bad():
    with pytest.raises(TaintRuleError):
        validate_taint_rule({"role": "wizard", "lang": "python", "pattern": "x"})
    with pytest.raises(TaintRuleError):
        validate_taint_rule({"role": "sink", "lang": "python", "pattern": "("})  # bad regex
    with pytest.raises(TaintRuleError):
        validate_taint_rule({"role": "sink", "lang": "python"})  # missing pattern


def test_external_rule_adds_a_sink(tmp_path):
    # a custom sink graphify has no builtin for
    (tmp_path / TAINT_RULES_FILENAME).write_text(json.dumps({"rules": [
        {"role": "sink", "lang": "python", "pattern": r"\brender_template_string\(",
         "vuln": "ssti"}
    ]}))
    (tmp_path / "app.py").write_text(
        "def f(request):\n"
        "    name = request.args.get('n')\n"
        "    render_template_string('Hi ' + name)\n"
    )
    scan = taint_scan(tmp_path)
    assert scan["stats"]["by_vuln"].get("ssti") == 1
    ext = [r for r in load_taint_rules(tmp_path) if r.origin == "external"]
    assert ext and ext[0].vuln == "ssti"


def test_taint_scan_stats(tmp_path):
    (tmp_path / "a.py").write_text(
        "def f(request, cursor):\n"
        "    x = request.args.get('id')\n"
        "    cursor.execute(x)\n"
    )
    scan = taint_scan(tmp_path)
    assert scan["stats"]["findings"] == 1
    assert scan["stats"]["by_vuln"] == {"sql_injection": 1}
    assert any(e["relation"] == "flows_to" for e in scan["edges"])
