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


# ── inter-procedural taint (summary-based, call-graph fixpoint) ───────────────


@dataclass(frozen=True)
class FnSummary:
    reaches_sink: tuple        # ((param_index, vuln), ...) — a tainted arg here hits a sink
    reaches_return: frozenset  # param indices whose taint reaches the return
    returns_source: bool       # returns internally-sourced taint (e.g. a wrapper)

    @property
    def sink_map(self) -> dict:
        return dict(self.reaches_sink)


_EMPTY_SUMMARY = FnSummary((), frozenset(), False)
_MAX_ITERS = 12


def _build_fn_table(pdgs: list[dict]) -> dict[str, dict]:
    funcs: dict[str, dict] = {}
    for pdg in pdgs:
        groups: dict[str, list] = defaultdict(list)
        for s in pdg["statements"]:
            groups[s.func].append(s)
        for fn, stmts in groups.items():
            entry = next((s for s in stmts if s.node_type == "function_entry"), None)
            funcs[fn] = {"stmts": stmts, "params": entry.params if entry else [],
                         "entry": entry.id if entry else None}
    return funcs


def _analyze_fn(view: dict, summaries: dict, rules: list[TaintRule], collect: bool):
    """One taint pass over a function using callee ``summaries``. Returns
    ``(FnSummary, findings)``. Taint is a *set of tags* per variable — each tag is
    a param index (→ summary fact) or a real source (→ finding) — so a param use
    can't mask a real source at the same sink. ``stmt_prev`` (source-preferred)
    reconstructs the intra-function path."""
    stmts, params, entry = view["stmts"], view["params"], view["entry"]
    by_id = {s.id: s for s in stmts}
    var_tags: dict[str, set] = {}     # var -> {("param", i) | ("src", stmt_id, category)}
    var_stmt: dict[str, str] = {}     # var -> stmt that last tainted it
    stmt_prev: dict[str, str | None] = {}
    for i, p in enumerate(params):
        var_tags[p] = {("param", i)}
        var_stmt[p] = entry
    reaches_sink: dict[int, str] = {}
    reaches_return: set[int] = set()
    returns_source = False
    findings: list[Finding] = []

    def _view(sid: str) -> dict:
        st = by_id.get(sid)
        if st is None:
            return {"stmt_id": sid, "line": 0, "text": "", "func": "", "file": ""}
        return {"stmt_id": sid, "line": st.line, "file": st.file, "func": st.func,
                "text": st.text.splitlines()[0].strip()[:160] if st.text else ""}

    def _path(sink_id: str) -> list[dict]:
        chain, cur, seen = [], sink_id, set()
        while cur is not None and cur not in seen:
            seen.add(cur)
            chain.append(_view(cur))
            cur = stmt_prev.get(cur)
        return list(reversed(chain))

    def _emit_tag(tag, sink_stmt, vuln, callee, arg_prev):
        if tag[0] == "param":
            reaches_sink[tag[1]] = vuln          # summary fact, not a finding
            return
        src_view = _view(tag[1])
        sink_view = _view(sink_stmt.id)
        if callee:                                # tainted arg → sink inside callee
            path = ([src_view] + _path(arg_prev)[:-1] if arg_prev else [src_view]) + [
                {**sink_view, "callee": callee}]
            sink_view = {**sink_view, "callee": callee}
        else:
            path = _path(sink_stmt.id)
        findings.append(Finding(vuln=vuln, category=tag[2], source=src_view,
                                sink=sink_view, path=path))

    for s in stmts:
        if s.node_type == "function_entry":
            continue
        if _matches(rules, "sanitizer", s.text):
            for d in s.defs:
                var_tags.pop(d, None)
                var_stmt.pop(d, None)
            continue
        tags: set = set()
        prev = src_prev = None
        for u in s.uses:
            if u in var_tags:
                tags |= var_tags[u]
                if prev is None:
                    prev = var_stmt.get(u)
                if src_prev is None and any(t[0] == "src" for t in var_tags[u]):
                    src_prev = var_stmt.get(u)
        def _arg_tags(arg):
            """Taint tags carried by a call argument: a tainted variable, an inline
            source expression, or a call to a returns_source function."""
            if arg.var and arg.var in var_tags:
                return var_tags[arg.var], var_stmt.get(arg.var)
            sm = _matches(rules, "source", arg.text)
            if sm:
                return {("src", s.id, sm[0].category or "tainted")}, None
            if arg.callee:
                cs = summaries.get(arg.callee)
                if cs is not None and cs.returns_source:
                    return {("src", s.id, f"via:{arg.callee}")}, None
            return set(), None

        for call in s.calls:
            summ = summaries.get(call.callee)
            if summ is None:
                continue
            smap = summ.sink_map
            for idx, arg in enumerate(call.args):
                atags, aprev = _arg_tags(arg)
                if atags and idx in smap:
                    for tag in atags:
                        _emit_tag(tag, s, smap[idx], call.callee, aprev)
                if atags and idx in summ.reaches_return:
                    tags |= atags
                    if src_prev is None and any(t[0] == "src" for t in atags):
                        src_prev = aprev
            if summ.returns_source:
                tags.add(("src", s.id, f"via:{call.callee}"))
        srcs = _matches(rules, "source", s.text)
        if srcs:
            tags = {("src", s.id, srcs[0].category or "tainted")}  # untrusted input replaces
            prev = src_prev = None
        if tags:
            stmt_prev[s.id] = src_prev if src_prev is not None else prev
            sinks = _matches(rules, "sink", s.text)
            if sinks:
                seen_src: set = set()
                for tag in tags:
                    if tag[0] == "src" and tag[1] in seen_src:
                        continue
                    seen_src.add(tag[1] if tag[0] == "src" else None)
                    _emit_tag(tag, s, sinks[0].vuln, None, None)
            if s.node_type == "return_statement":
                for tag in tags:
                    if tag[0] == "param":
                        reaches_return.add(tag[1])
                    else:
                        returns_source = True
            for d in s.defs:
                var_tags[d], var_stmt[d] = set(tags), s.id

    summary = FnSummary(tuple(sorted(reaches_sink.items())), frozenset(reaches_return), returns_source)
    return summary, (findings if collect else [])


def analyze_taint_interproc(pdgs: list[dict], rules: list[TaintRule]) -> list[Finding]:
    """Inter-procedural taint over a set of PDGs: compute per-function summaries to
    a call-graph fixpoint, then collect source→sink findings (intra + cross-function).
    Callees are resolved by function name (approximate across files)."""
    funcs = _build_fn_table(pdgs)
    summaries: dict[str, FnSummary] = dict.fromkeys(funcs, _EMPTY_SUMMARY)
    for _ in range(_MAX_ITERS):
        changed = False
        for fn, view in funcs.items():
            summ, _ = _analyze_fn(view, summaries, rules, collect=False)
            if summ != summaries[fn]:
                summaries[fn], changed = summ, True
        if not changed:
            break
    findings: list[Finding] = []
    for view in funcs.values():
        _, fs = _analyze_fn(view, summaries, rules, collect=True)
        findings += fs
    return findings


def _clean_path(path: list[dict]) -> list[dict]:
    """Collapse consecutive steps that share a stmt_id (an inter-procedural
    path-construction artifact) so the rendered flow reads source→…→sink once."""
    out: list[dict] = []
    for step in path:
        if out and out[-1].get("stmt_id") == step.get("stmt_id"):
            continue
        out.append(step)
    return out


def _findings_edges(findings: list[Finding]) -> list[dict]:
    """A ``flows_to`` edge per finding (source stmt → sink stmt), carrying the whole
    finding in metadata so the graph/REST layer can serve it without re-analysis."""
    out = []
    for f in findings:
        out.append({
            "source": f.source["stmt_id"], "target": f.sink["stmt_id"], "relation": "flows_to",
            "confidence": f.confidence, "confidence_score": 0.8, "context": "taint",
            "source_file": f.source.get("file"),
            "source_location": f"L{f.source.get('line', 0)}",
            "metadata": {
                "vuln": f.vuln, "category": f.category, "confidence": f.confidence,
                "cross_function": bool(f.sink.get("callee")), "callee": f.sink.get("callee"),
                "source": f.source, "sink": f.sink, "path": _clean_path(f.path),
            },
        })
    return out


def findings_to_graph(findings: list[Finding]) -> dict[str, Any]:
    """Graphify ``{nodes, edges}`` for a list of findings: the statement nodes on
    each source→sink path + the enriched ``flows_to`` edges. Only the
    finding-relevant statements (not the whole PDG) — keeps graph.json lean."""
    nodes: dict[str, dict] = {}
    for f in findings:
        steps = list(f.path)
        # ensure source & sink are present even if the path collapsed them
        for v in (f.source, f.sink, *steps):
            sid = v.get("stmt_id")
            if not sid or sid in nodes:
                continue
            nodes[sid] = {
                "id": sid, "label": v.get("text") or "statement", "file_type": "code",
                "kind": "statement", "source_file": v.get("file"),
                "source_location": f"L{v.get('line', 0)}",
                "metadata": {"func": v.get("func"), "taint": True},
            }
    return {"nodes": list(nodes.values()), "edges": _findings_edges(findings)}


def taint_source(source: str, *, path: str = "", rules: list[TaintRule] | None = None,
                 ns: str = "m") -> dict[str, Any]:
    """PDG + inter-procedural taint for one Python source string. Returns
    ``{nodes, edges, findings}`` (PDG nodes/edges + taint ``flows_to`` edges)."""
    pdg = build_pdg(source, path=path, ns=ns)
    findings = analyze_taint_interproc([pdg], rules if rules is not None else list(BUILTIN_TAINT_RULES))
    return {"nodes": pdg["nodes"], "edges": pdg["edges"] + _findings_edges(findings),
            "findings": findings}


def taint_scan(root: str | Path) -> dict[str, Any]:
    """Scan every ``.py`` file under ``root`` for taint (inter-procedural across the
    whole tree). Returns ``{nodes, edges, findings, stats}`` in graphify schema."""
    root = Path(root)
    rules = load_taint_rules(root)
    pdgs: list[dict] = []
    nodes: list[dict] = []
    edges: list[dict] = []
    for f in sorted(root.rglob("*.py")):
        if not f.is_file():
            continue
        try:
            src = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        pdg = build_pdg(src, path=str(f), ns=f.stem)
        pdgs.append(pdg)
        nodes += pdg["nodes"]
        edges += pdg["edges"]
    findings = analyze_taint_interproc(pdgs, rules)
    edges += _findings_edges(findings)
    by_vuln: dict[str, int] = defaultdict(int)
    for fi in findings:
        by_vuln[fi.vuln] += 1
    return {"nodes": nodes, "edges": edges, "findings": findings,
            "stats": {"findings": len(findings), "by_vuln": dict(by_vuln)}}
