"""learn_bindings.py — LLM authors binding rules; the engine executes them.

The reconciliation of "an LLM already knows every framework" with "blast radius
must be deterministic": use the LLM at *author time* to WRITE a binding rule, not
at run time to BE the extractor. The LLM proposes ``BindingRule`` rows for
framework constructs it recognizes; each is deterministically **validated**
(``binding_rules.validate_rule``) and, once a human ratifies, **persisted** to
``.graphify_binding_rules.json``. From then on ``run_bindings`` executes it
deterministically — reproducible, free, EXTRACTED — forever.

Flow:
  1. ``harvest_candidates`` (pure, deterministic) — find decorator/annotation
     lines that NO existing rule matches. This grounds the LLM in real code, so it
     proposes rules for constructs that actually exist, not hallucinated ones.
  2. ``propose_rules`` — ask the LLM to emit rule JSON for the scheduler/event
     constructs among the candidates; validate every proposal, drop the invalid.
  3. ``persist_rules`` — a human ratifies, then the rules are written to the
     plug-in file and run deterministically thereafter.

Only the authoring is non-deterministic; execution never touches an LLM.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from graphify.binding_rules import (
    BUILTIN_RULES,
    EXTERNAL_RULES_FILENAME,
    BindingRule,
    RuleError,
    detect_lang,
    next_def_after,
    validate_rule,
)

_DECORATOR = {
    "python": re.compile(r"^\s*@([A-Za-z_][\w.]*)"),
    "java": re.compile(r"^\s*@([A-Z][\w.]*)"),
    "ts": re.compile(r"^\s*@([A-Z][\w.]*)"),
}
_MAX_SAMPLES = 3


@dataclass
class Candidate:
    """An annotation/decorator no existing rule covers — a rule-authoring lead."""

    annotation: str
    lang: str
    count: int
    samples: list[dict]     # [{file, line, text, handler}]


def harvest_candidates(
    root: str | Path,
    rules: list[BindingRule] | None = None,
) -> list[Candidate]:
    """Deterministically find decorator/annotation lines under ``root`` that no
    rule in ``rules`` matches, grouped by annotation. This is what the LLM is
    asked to write rules for — grounded in code that actually exists."""
    root = Path(root)
    if rules is None:
        rules = list(BUILTIN_RULES)
    by_lang: dict[str, list[BindingRule]] = {}
    for r in rules:
        for lang in r.languages:
            by_lang.setdefault(lang, []).append(r)

    found: dict[tuple[str, str], Candidate] = {}
    for f in sorted(root.rglob("*")):
        if not f.is_file():
            continue
        lang = detect_lang(f)
        if lang is None:
            continue
        dec = _DECORATOR[lang]
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        existing = by_lang.get(lang, [])
        for i, line in enumerate(lines):
            m = dec.match(line)
            if not m:
                continue
            if any(r.regex.search(line) for r in existing):
                continue  # already covered by a rule
            annotation = m.group(1).split(".")[-1]
            handler, _ = next_def_after(lines, i, lang)
            if handler is None:
                continue  # not bound to a function — unlikely to be a construct
            key = (lang, annotation)
            cand = found.get(key)
            if cand is None:
                cand = Candidate(annotation=annotation, lang=lang, count=0, samples=[])
                found[key] = cand
            cand.count += 1
            if len(cand.samples) < _MAX_SAMPLES:
                cand.samples.append({
                    "file": str(f), "line": i + 1,
                    "text": line.strip()[:200], "handler": handler,
                })
    return sorted(found.values(), key=lambda c: (-c.count, c.annotation))


_SCHEMA_HINT = """\
Each rule is a JSON object with these fields:
  id          unique dotted id, e.g. "scheduler.quartz.job"
  category    "scheduler" (runs a handler on a timer) or "event" (invokes a
              handler on a message/event). Use ONLY these two.
  provider    framework name, lower-case, e.g. "quartz", "sqs"
  languages   list from ["python","java","ts"]
  pattern     a Python regex matching the decorator/annotation line; MUST anchor
              to line start with ^\\\\s* so comments don't match, e.g. "^\\\\s*@SqsListener\\\\b"
  relation    snake_case edge type: "triggers" for schedulers, "consumes" for events
  node_kind   snake_case: "schedule" for schedulers, "event" or "topic" for events
  description one short line
"""


def build_prompt(candidates: list[Candidate]) -> str:
    """Construct the rule-authoring prompt: schema, a couple of built-in examples
    as few-shot, and the grounded candidate list."""
    examples = [
        {"id": r.id, "category": r.category, "provider": r.provider,
         "languages": list(r.languages), "pattern": r.pattern,
         "relation": r.relation, "node_kind": r.node_kind, "description": r.description}
        for r in BUILTIN_RULES if r.id in
        ("scheduler.spring.scheduled", "event.kafka.listener")
    ]
    cand_lines = []
    for c in candidates:
        s = c.samples[0]
        cand_lines.append(
            f"- annotation @{c.annotation} ({c.lang}, {c.count}x). "
            f"example: `{s['text']}` binds handler `{s['handler']}`"
        )
    return (
        "You write detection rules for graphify. Given decorators/annotations that "
        "graphify has no rule for, output a JSON array of rules for the ones that are "
        "SCHEDULERS (run a handler on a timer) or EVENT/MESSAGE LISTENERS (invoke a "
        "handler when a message/event arrives).\n\n"
        "STRICT: only scheduler/event constructs. SKIP dependency injection, HTTP "
        "routing, ORM/entities, validation, serialization, and generic decorators "
        "(@property, @Component, @Injectable, @Override, etc.). If none qualify, "
        "output [].\n\n"
        f"{_SCHEMA_HINT}\n"
        f"Examples of valid rules:\n{json.dumps(examples, indent=2)}\n\n"
        f"Uncovered annotations found in this codebase:\n" + "\n".join(cand_lines) +
        "\n\nOutput ONLY the JSON array of rules, no prose."
    )


def _extract_json_array(text: str) -> list:
    """Pull a JSON array out of an LLM reply (tolerating ``` fences / prose)."""
    t = text.strip()
    if "```" in t:
        # take the content of the first fenced block
        parts = t.split("```")
        for chunk in parts:
            chunk = chunk.removeprefix("json").strip()
            if chunk.startswith("["):
                t = chunk
                break
    start, end = t.find("["), t.rfind("]")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON array found in LLM reply")
    return json.loads(t[start:end + 1])


def propose_rules(
    candidates: list[Candidate],
    *,
    backend: str,
    model: str | None = None,
    _caller=None,
) -> tuple[list[BindingRule], list[tuple[dict, str]]]:
    """Ask the LLM to author rules for ``candidates``; validate every proposal.

    Returns ``(accepted, rejected)`` where accepted are validated ``BindingRule``s
    (origin='llm') and rejected are ``(raw_dict, reason)``. ``_caller`` injects the
    LLM call for testing; by default it dispatches through ``llm._call_llm`` and so
    honours the configured backend.
    """
    if not candidates:
        return [], []
    prompt = build_prompt(candidates)
    if _caller is None:
        from graphify.llm import _call_llm
        def _caller(p):  # noqa: E731 - tiny adapter
            return _call_llm(p, backend=backend, model=model, max_tokens=2000)
    reply = _caller(prompt)
    try:
        proposals = _extract_json_array(reply)
    except (ValueError, json.JSONDecodeError) as exc:
        return [], [({}, f"could not parse LLM reply as a JSON array: {exc}")]

    accepted: list[BindingRule] = []
    rejected: list[tuple[dict, str]] = []
    seen = {r.id for r in BUILTIN_RULES}
    for d in proposals:
        if not isinstance(d, dict):
            rejected.append(({"raw": d}, "not a JSON object"))
            continue
        try:
            rule = validate_rule(d, origin="llm")
        except RuleError as exc:
            rejected.append((d, str(exc)))
            continue
        if rule.id in seen:
            rejected.append((d, f"duplicate id {rule.id!r}"))
            continue
        seen.add(rule.id)
        accepted.append(rule)
    return accepted, rejected


def _rule_to_dict(r: BindingRule) -> dict[str, Any]:
    d = {
        "id": r.id, "category": r.category, "provider": r.provider,
        "languages": list(r.languages), "pattern": r.pattern,
        "relation": r.relation, "node_kind": r.node_kind,
        "resolution": r.resolution, "confidence": r.confidence,
        "description": r.description,
    }
    if r.extra_meta:
        d["extra_meta"] = dict(r.extra_meta)
    return d


def persist_rules(root: str | Path, rules: list[BindingRule]) -> Path:
    """Merge ``rules`` into ``<root>/.graphify_binding_rules.json`` (dedup by id),
    creating it if absent. Returns the path written."""
    path = Path(root) / EXTERNAL_RULES_FILENAME
    existing: list[dict] = []
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            existing = raw.get("rules", raw) if isinstance(raw, dict) else raw
            if not isinstance(existing, list):
                existing = []
        except (OSError, json.JSONDecodeError):
            existing = []
    by_id = {d.get("id"): d for d in existing if isinstance(d, dict)}
    for r in rules:
        by_id[r.id] = _rule_to_dict(r)
    path.write_text(json.dumps({"rules": list(by_id.values())}, indent=2) + "\n",
                    encoding="utf-8")
    return path
