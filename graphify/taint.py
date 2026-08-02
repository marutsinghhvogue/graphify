"""taint.py — source→sink taint analysis over the PDG (``pdg.py``).

Untrusted/sensitive data ("source") that reaches a dangerous operation ("sink")
without passing a "sanitizer" is a finding. Sources/sinks/sanitizers are a
**rule catalog** — the same rules-as-data discipline as ``binding_rules`` (built-in
+ ``.graphify_taint_rules.json``, validated, LLM-authorable), so a new
framework/vuln is a data row, not code.

Algorithm: match each PDG statement against the catalog; mark source statements
tainted; propagate taint transitively along ``data_dep`` edges, **stopping at
sanitizers**; any **sink** statement that is tainted is a finding (with the
source→…→sink path). Findings are INFERRED (v1 PDG is last-write, intra-procedural).
See docs/pdg-taint-design.md.
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from graphify.pdg import build_pdg

TAINT_RULES_FILENAME = ".graphify_taint_rules.json"
_VALID_ROLES = frozenset({"source", "sink", "sanitizer"})


@dataclass(frozen=True)
class TaintRule:
    role: str                 # source | sink | sanitizer
    lang: str                 # currently "python"
    pattern: str
    category: str = ""        # source: e.g. untrusted-input, pii
    vuln: str = ""            # sink: e.g. sql_injection, command_injection
    description: str = ""
    origin: str = "builtin"

    @property
    def regex(self) -> re.Pattern:
        return _compile(self.pattern)


def _compile(pattern: str) -> re.Pattern:
    return re.compile(pattern)


class TaintRuleError(ValueError):
    pass


def validate_taint_rule(d: dict[str, Any], *, origin: str = "external") -> TaintRule:
    for k in ("role", "lang", "pattern"):
        if not d.get(k):
            raise TaintRuleError(f"taint rule missing {k!r}: {d!r}")
    if d["role"] not in _VALID_ROLES:
        raise TaintRuleError(f"role must be one of {sorted(_VALID_ROLES)}, got {d['role']!r}")
    try:
        re.compile(d["pattern"])
    except re.error as exc:
        raise TaintRuleError(f"invalid regex {d['pattern']!r}: {exc}") from None
    return TaintRule(
        role=str(d["role"]), lang=str(d["lang"]), pattern=str(d["pattern"]),
        category=str(d.get("category", "")), vuln=str(d.get("vuln", "")),
        description=str(d.get("description", "")), origin=origin,
    )


# Built-in Python catalog (extend via .graphify_taint_rules.json / learn-bindings).
BUILTIN_TAINT_RULES: tuple[TaintRule, ...] = (
    # sources — untrusted input
    TaintRule("source", "python", r"request\.(args|form|values|json|data|cookies|headers)",
              category="untrusted-input", description="Flask/Django request input"),
    TaintRule("source", "python", r"\.get_json\(", category="untrusted-input"),
    TaintRule("source", "python", r"\binput\(", category="untrusted-input"),
    TaintRule("source", "python", r"os\.environ", category="config-input"),
    # sinks
    TaintRule("sink", "python", r"\.execute(many)?\(", vuln="sql_injection",
              description="DB cursor.execute (raw SQL)"),
    TaintRule("sink", "python", r"os\.system\(", vuln="command_injection"),
    TaintRule("sink", "python", r"subprocess\.(run|call|Popen|check_output)\(", vuln="command_injection"),
    TaintRule("sink", "python", r"\beval\(", vuln="code_injection"),
    TaintRule("sink", "python", r"\bexec\(", vuln="code_injection"),
    # sanitizers
    TaintRule("sanitizer", "python", r"\bint\(", description="numeric coercion"),
    TaintRule("sanitizer", "python", r"\b(escape|quote|sanitize|clean)\("),
)


def load_taint_rules(root: str | Path | None = None, *, include_external: bool = True) -> list[TaintRule]:
    rules = list(BUILTIN_TAINT_RULES)
    if include_external and root is not None:
        ext = Path(root) / TAINT_RULES_FILENAME
        if ext.exists():
            try:
                raw = json.loads(ext.read_text(encoding="utf-8"))
                entries = raw.get("rules", raw) if isinstance(raw, dict) else raw
            except (OSError, json.JSONDecodeError) as exc:
                print(f"[graphify taint] warning: could not read {ext}: {exc}", file=sys.stderr)
                entries = []
            for d in entries if isinstance(entries, list) else []:
                try:
                    rules.append(validate_taint_rule(d, origin="external"))
                except TaintRuleError as exc:
                    print(f"[graphify taint] warning: skipping invalid rule: {exc}", file=sys.stderr)
    return rules


@dataclass
class Finding:
    vuln: str
    category: str
    source: dict           # {stmt_id, line, text}
    sink: dict
    path: list[dict]       # source → … → sink statement views
    confidence: str = "INFERRED"


def _matches(rules: list[TaintRule], role: str, text: str) -> list[TaintRule]:
    return [r for r in rules if r.role == role and r.regex.search(text)]


def analyze_taint(pdg_result: dict[str, Any], rules: list[TaintRule]) -> dict[str, Any]:
    """Given a ``build_pdg`` result + rules, return ``{findings, edges}``: taint
    ``flows_to`` edges (source→sink) and structured findings."""
    stmts = pdg_result["statements"]
    by_id = {s.id: s for s in stmts}
    incoming: dict[str, list[str]] = defaultdict(list)
    for e in pdg_result["edges"]:
        if e.get("relation") == "data_dep":
            incoming[e["target"]].append(e["source"])

    # taint state: stmt_id -> {origin, category, prev}
    tainted: dict[str, dict] = {}
    for s in stmts:  # source order
        # Sanitizer takes precedence: a statement that cleans a value produces a
        # clean output even if it also reads a source (e.g. int(request.args.get)).
        if _matches(rules, "sanitizer", s.text):
            continue
        srcs = _matches(rules, "source", s.text)
        if srcs:
            tainted[s.id] = {"origin": s.id, "category": srcs[0].category or "tainted", "prev": None}
            continue
        for pid in incoming.get(s.id, []):
            if pid in tainted:
                tainted[s.id] = {"origin": tainted[pid]["origin"],
                                 "category": tainted[pid]["category"], "prev": pid}
                break

    def _view(sid: str) -> dict:
        st = by_id[sid]
        return {"stmt_id": sid, "line": st.line,
                "text": st.text.splitlines()[0].strip()[:160] if st.text else "",
                "func": st.func}

    def _path(sink_id: str) -> list[dict]:
        chain, cur = [], sink_id
        while cur is not None:
            chain.append(_view(cur))
            cur = tainted.get(cur, {}).get("prev")
        return list(reversed(chain))

    findings: list[Finding] = []
    edges: list[dict] = []
    for s in stmts:
        if s.id not in tainted:
            continue
        sink_rules = _matches(rules, "sink", s.text)
        if not sink_rules:
            continue
        t = tainted[s.id]
        findings.append(Finding(
            vuln=sink_rules[0].vuln or "taint", category=t["category"],
            source=_view(t["origin"]), sink=_view(s.id), path=_path(s.id),
        ))
        edges.append({
            "source": t["origin"], "target": s.id, "relation": "flows_to",
            "confidence": "INFERRED", "confidence_score": 0.8, "context": "taint",
            "metadata": {"vuln": sink_rules[0].vuln, "category": t["category"]},
        })
    return {"findings": findings, "edges": edges}


def taint_source(source: str, *, path: str = "", rules: list[TaintRule] | None = None,
                 ns: str = "m") -> dict[str, Any]:
    """PDG + taint for one Python source string. Returns ``{nodes, edges, findings}``
    where nodes/edges include the PDG plus taint ``flows_to`` edges."""
    pdg = build_pdg(source, path=path, ns=ns)
    result = analyze_taint(pdg, rules if rules is not None else list(BUILTIN_TAINT_RULES))
    return {"nodes": pdg["nodes"], "edges": pdg["edges"] + result["edges"],
            "findings": result["findings"]}


def taint_scan(root: str | Path) -> dict[str, Any]:
    """Scan every ``.py`` file under ``root`` for taint. Returns
    ``{nodes, edges, findings, stats}`` in graphify schema."""
    root = Path(root)
    rules = load_taint_rules(root)
    nodes: list[dict] = []
    edges: list[dict] = []
    findings: list[Finding] = []
    for f in sorted(root.rglob("*.py")):
        if not f.is_file():
            continue
        try:
            src = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        pdg = build_pdg(src, path=str(f), ns=f.stem)
        res = analyze_taint(pdg, rules)
        nodes += pdg["nodes"]
        edges += pdg["edges"] + res["edges"]
        findings += res["findings"]
    by_vuln: dict[str, int] = defaultdict(int)
    for fi in findings:
        by_vuln[fi.vuln] += 1
    return {"nodes": nodes, "edges": edges, "findings": findings,
            "stats": {"findings": len(findings), "by_vuln": dict(by_vuln)}}
