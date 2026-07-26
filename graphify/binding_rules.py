"""binding_rules.py — declarative, pluggable detection of framework constructs
that bind a handler to a trigger/source (schedulers, event listeners, …).

The problem this solves: cross-service, scheduler, and event detection are the
SAME shape — *a decorator/annotation binds the function below it to some
construct, and we want a typed edge from a synthetic construct node to that
handler*. Rather than a bespoke module + hardcoded regexes per concern, a
construct is a **data row** (a ``BindingRule``) and one deterministic engine
(``run_bindings``) executes every rule. Adding a framework is a rule, not code.

Design guarantees (why this stays the EXTRACTED, deterministic tier):
- **Deterministic**: pure regex over source lines; same input → same edges,
  every run. No LLM at run time.
- **Pluggable**: built-in rules + user/LLM-authored rules loaded from
  ``.graphify_binding_rules.json`` at the scan root. A new framework needs no
  code change.
- **Uniform output**: emits the standard graphify ``{nodes, edges, stats}``;
  handler function nodes reuse the ``svc_*_fn_*`` id scheme so the generic
  ``reconcile_contract`` folds them onto the tree-sitter AST, and ``blast_radius``
  traverses the emitted relation once it is in ``DEFAULT_AFFECTED_RELATIONS``.

Resolution modes:
- ``next_def`` (implemented): the construct annotates the method/function on the
  next line — schedulers, event listeners, message handlers. The bound handler
  is that def; the edge is EXTRACTED (the binding is syntactic).

Deliberately NOT here yet: dependency-injection ("inject" resolution — an
annotated field/param → its *type*), because resolving the target type
deterministically is SCIP's job (name matching alone is low precision). The
``resolution`` field reserves that as a future rule family rather than shipping a
low-confidence regex version. See docs/scheduler-detection-design.md.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

# ── shared source helpers (the single canonical home; other introspectors import
#    these instead of re-declaring them) ───────────────────────────────────────

_PY_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(")
_TS_METHOD = re.compile(r"^\s*(?:public |private |protected )?(?:static )?(?:async )?(\w+)\s*\(")
_JAVA_METHOD = re.compile(r"^\s*(?:public|private|protected)\s+[\w<>\[\], ?]+\s+(\w+)\s*\(")
_NON_FN = {"if", "for", "while", "switch", "catch", "return", "constructor", "class", "new"}
_DEFS = {"python": _PY_DEF, "ts": _TS_METHOD, "java": _JAVA_METHOD}

_LANG_BY_SUFFIX = {".py": "python", ".java": "java", ".ts": "ts", ".tsx": "ts"}
_PAREN = re.compile(r"\(([^)]*)\)")

VALID_LANGS = frozenset(_DEFS)
VALID_RESOLUTIONS = frozenset({"next_def", "inject"})
VALID_CONFIDENCE = frozenset({"EXTRACTED", "INFERRED", "AMBIGUOUS"})

# Enclosing type declaration, for the 'inject' resolution (DI: the annotated
# member belongs to a class, and the edge runs from that class to the injected
# type).
_CLASS_DECL = {
    "java": re.compile(r"^\s*(?:public\s+|abstract\s+|final\s+|static\s+)*(?:class|interface|enum)\s+(\w+)"),
    "ts": re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(\w+)"),
    "python": re.compile(r"^\s*class\s+(\w+)"),
}
# The injected type on a Java field/setter injection: capture the Capitalized
# type token of the declaration on (or just below) the annotation.
_JAVA_FIELD_TYPE = re.compile(
    r"(?:private|public|protected)?\s*(?:final\s+)?([A-Z]\w+)(?:<[^>]*>)?\s+\w+\s*[;=(]"
)

EXTERNAL_RULES_FILENAME = ".graphify_binding_rules.json"


def detect_lang(path: Path) -> str | None:
    return _LANG_BY_SUFFIX.get(path.suffix)


def next_def_after(lines: list[str], start: int, lang: str) -> tuple[str | None, int | None]:
    """Name + line index of the first function/method def at or after ``start``."""
    pat = _DEFS[lang]
    for j in range(start, len(lines)):
        m = pat.match(lines[j])
        if m and m.group(1) not in _NON_FN:
            return m.group(1), j
    return None, None


def enclosing_class(lines: list[str], idx: int, lang: str) -> tuple[str | None, int | None]:
    """Name + line index of the nearest class/interface/enum declaration at or
    above ``idx`` — the owner of an injected member."""
    pat = _CLASS_DECL.get(lang)
    if pat is None:
        return None, None
    for j in range(idx, -1, -1):
        m = pat.match(lines[j])
        if m:
            return m.group(1), j
    return None, None


def injected_type(lines: list[str], idx: int, lang: str) -> str | None:
    """The injected type of a Java field/setter injection — the Capitalized type
    token on the annotation line or the next couple of lines."""
    if lang != "java":
        return None
    for j in range(idx, min(idx + 3, len(lines))):
        m = _JAVA_FIELD_TYPE.search(lines[j])
        if m:
            return m.group(1)
    return None


def paren_content(line: str) -> str:
    """Best-effort construct argument: text inside the first ``(...)`` on the line
    (stops at the first close paren — nested calls truncate)."""
    m = _PAREN.search(line)
    return m.group(1).strip() if m else ""


def service_of(f: Path, root: Path) -> str:
    """Service = the immediate subdirectory of the scan root (monorepo-of-services
    layout); the root's own name when the file is top-level."""
    try:
        parts = f.relative_to(root).parts
    except ValueError:
        return root.name or "app"
    return parts[0] if len(parts) > 1 else (root.name or "app")


def _fn_id(service: str, fn: str) -> str:
    return f"svc_{service}_fn_{fn}".lower()


def _sanitize_id(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


# ── the rule ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BindingRule:
    """A declarative construct→handler binding. Built-in or loaded from JSON.

    ``pattern`` is a regex matched against each source line; anchor it to the line
    start (``^\\s*``) so a comment mentioning the annotation is not a false match.
    ``relation`` is the emitted edge type (add it to
    ``affected.DEFAULT_AFFECTED_RELATIONS`` for blast radius to traverse it).
    """

    id: str
    category: str                       # "scheduler" | "event" | ...
    provider: str                       # "spring" | "nestjs" | "kafka" | ...
    languages: tuple[str, ...]
    pattern: str
    relation: str
    node_kind: str
    resolution: str = "next_def"
    confidence: str = "EXTRACTED"
    description: str = ""
    origin: str = "builtin"             # "builtin" | "external" | "llm"
    extra_meta: dict[str, str] = field(default_factory=dict)

    @property
    def regex(self) -> re.Pattern:
        return _compile(self.pattern)


@lru_cache(maxsize=512)
def _compile(pattern: str) -> re.Pattern:
    return re.compile(pattern)


class RuleError(ValueError):
    """A rule definition is malformed."""


def validate_rule(d: dict[str, Any], *, origin: str = "external") -> BindingRule:
    """Validate an external/LLM rule dict and build a ``BindingRule``. Raises
    ``RuleError`` with a precise reason so a bad LLM proposal fails loud, not
    silently corrupts the graph."""
    required = ("id", "category", "provider", "languages", "pattern", "relation", "node_kind")
    for k in required:
        if not d.get(k):
            raise RuleError(f"rule missing required field {k!r}: {d!r}")
    langs = d["languages"]
    if isinstance(langs, str):
        langs = [langs]
    if not isinstance(langs, (list, tuple)) or not langs:
        raise RuleError(f"rule {d['id']!r}: 'languages' must be a non-empty list")
    bad = [x for x in langs if x not in VALID_LANGS]
    if bad:
        raise RuleError(f"rule {d['id']!r}: unsupported language(s) {bad} (valid: {sorted(VALID_LANGS)})")
    resolution = d.get("resolution", "next_def")
    if resolution not in VALID_RESOLUTIONS:
        raise RuleError(f"rule {d['id']!r}: unsupported resolution {resolution!r} (valid: {sorted(VALID_RESOLUTIONS)})")
    confidence = d.get("confidence", "EXTRACTED")
    if confidence not in VALID_CONFIDENCE:
        raise RuleError(f"rule {d['id']!r}: confidence must be one of {sorted(VALID_CONFIDENCE)}")
    for f_ in ("relation", "node_kind"):
        if not re.fullmatch(r"[a-z][a-z0-9_]*", str(d[f_])):
            raise RuleError(f"rule {d['id']!r}: {f_} must be snake_case, got {d[f_]!r}")
    try:
        re.compile(d["pattern"])
    except re.error as exc:
        raise RuleError(f"rule {d['id']!r}: invalid regex {d['pattern']!r}: {exc}") from None
    extra = d.get("extra_meta") or {}
    if not isinstance(extra, dict):
        raise RuleError(f"rule {d['id']!r}: extra_meta must be an object")
    return BindingRule(
        id=str(d["id"]), category=str(d["category"]), provider=str(d["provider"]),
        languages=tuple(langs), pattern=str(d["pattern"]), relation=str(d["relation"]),
        node_kind=str(d["node_kind"]), resolution=resolution, confidence=confidence,
        description=str(d.get("description", "")), origin=origin,
        extra_meta={str(k): str(v) for k, v in extra.items()},
    )


# ── built-in registry ─────────────────────────────────────────────────────────
# Add a same-shaped framework here (or, without touching code, in the external
# JSON rules file). Every rule below is the "annotation binds the next def" shape.

BUILTIN_RULES: tuple[BindingRule, ...] = (
    # schedulers → 'triggers'
    BindingRule("scheduler.spring.scheduled", "scheduler", "spring", ("java",),
                r"^\s*@Scheduled\b", "triggers", "schedule",
                description="Spring @Scheduled(cron/fixedRate/fixedDelay)"),
    BindingRule("scheduler.nestjs.cron", "scheduler", "nestjs", ("ts",),
                r"^\s*@Cron\b", "triggers", "schedule",
                description="NestJS @Cron", extra_meta={"trigger": "cron"}),
    BindingRule("scheduler.nestjs.interval", "scheduler", "nestjs", ("ts",),
                r"^\s*@Interval\b", "triggers", "schedule",
                description="NestJS @Interval", extra_meta={"trigger": "interval"}),
    BindingRule("scheduler.nestjs.timeout", "scheduler", "nestjs", ("ts",),
                r"^\s*@Timeout\b", "triggers", "schedule",
                description="NestJS @Timeout", extra_meta={"trigger": "timeout"}),
    BindingRule("scheduler.apscheduler.job", "scheduler", "apscheduler", ("python",),
                r"^\s*@(?:\w+\.)?scheduled_job\b", "triggers", "schedule",
                description="APScheduler @scheduled_job"),
    BindingRule("scheduler.celery.periodic", "scheduler", "celery", ("python",),
                r"^\s*@periodic_task\b", "triggers", "schedule",
                description="Celery @periodic_task"),
    # event / message listeners → 'consumes'
    BindingRule("event.spring.eventlistener", "event", "spring", ("java",),
                r"^\s*@EventListener\b", "consumes", "event",
                description="Spring @EventListener"),
    BindingRule("event.kafka.listener", "event", "kafka", ("java",),
                r"^\s*@KafkaListener\b", "consumes", "topic",
                description="Spring Kafka @KafkaListener(topics=...)"),
    BindingRule("event.nestjs.eventpattern", "event", "nestjs", ("ts",),
                r"^\s*@EventPattern\b", "consumes", "event",
                description="NestJS @EventPattern"),
    BindingRule("event.nestjs.messagepattern", "event", "nestjs", ("ts",),
                r"^\s*@MessagePattern\b", "consumes", "event",
                description="NestJS @MessagePattern"),
    # dependency injection → 'injects' (class → injected type). Name-resolved →
    # INFERRED; SCIP upgrades the target to a type-exact node when available.
    BindingRule("di.spring.autowired", "di", "spring", ("java",),
                r"^\s*@Autowired\b", "injects", "class",
                resolution="inject", confidence="INFERRED",
                description="Spring @Autowired field/setter injection"),
    BindingRule("di.jsr330.inject", "di", "jsr330", ("java",),
                r"^\s*@Inject\b", "injects", "class",
                resolution="inject", confidence="INFERRED",
                description="JSR-330 @Inject field injection"),
    BindingRule("di.jakarta.resource", "di", "jakarta", ("java",),
                r"^\s*@Resource\b", "injects", "class",
                resolution="inject", confidence="INFERRED",
                description="Jakarta @Resource injection"),
)


def load_rules(
    root: str | Path,
    *,
    categories: list[str] | None = None,
    include_external: bool = True,
) -> list[BindingRule]:
    """Built-in rules plus any valid rules from ``<root>/.graphify_binding_rules.json``
    (the user/LLM plug-in point). Invalid external rules are skipped with a stderr
    warning — one bad row never blocks the rest. Filter to ``categories`` if given."""
    rules = list(BUILTIN_RULES)
    if include_external:
        ext_path = Path(root) / EXTERNAL_RULES_FILENAME
        if ext_path.exists():
            rules.extend(_load_external_rules(ext_path))
    if categories is not None:
        wanted = set(categories)
        rules = [r for r in rules if r.category in wanted]
    return rules


def _load_external_rules(path: Path) -> list[BindingRule]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[graphify bindings] warning: could not read {path}: {exc}", file=sys.stderr)
        return []
    entries = raw.get("rules", raw) if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        print(f"[graphify bindings] warning: {path} must be a list of rules (or {{'rules': [...]}})",
              file=sys.stderr)
        return []
    out: list[BindingRule] = []
    seen = {r.id for r in BUILTIN_RULES}
    for d in entries:
        try:
            rule = validate_rule(d, origin="external")
        except RuleError as exc:
            print(f"[graphify bindings] warning: skipping invalid rule: {exc}", file=sys.stderr)
            continue
        if rule.id in seen:
            print(f"[graphify bindings] warning: duplicate rule id {rule.id!r} — skipping", file=sys.stderr)
            continue
        seen.add(rule.id)
        out.append(rule)
    return out


# ── the engine ────────────────────────────────────────────────────────────────


def _derive_trigger(provider: str, expr: str) -> str:
    low = expr.lower()
    if "cron" in low or "crontab" in low:
        return "cron"
    if provider == "apscheduler" and any(t in low for t in ("interval", "seconds=", "minutes=")):
        return "interval"
    if provider == "spring" and ("fixedrate" in low or "fixeddelay" in low):
        return "rate"
    if provider == "celery":
        return "cron" if "crontab" in low else "rate"
    return "schedule"


def run_bindings(
    root: str | Path,
    *,
    categories: list[str] | None = None,
    rules: list[BindingRule] | None = None,
) -> dict[str, Any]:
    """Deterministically scan ``root`` for every applicable binding rule and emit
    ``{nodes, edges, stats}`` in graphify schema: a synthetic construct node per
    match and an edge (``rule.relation``) to the handler it binds. Handler nodes
    reuse the ``svc_*_fn_*`` scheme so ``reconcile_contract`` folds them onto AST."""
    root = Path(root)
    if rules is None:
        rules = load_rules(root, categories=categories)
    elif categories is not None:
        wanted = set(categories)
        rules = [r for r in rules if r.category in wanted]

    by_lang: dict[str, list[BindingRule]] = {}
    for r in rules:
        for lang in r.languages:
            by_lang.setdefault(lang, []).append(r)

    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    stats: dict[str, Any] = {"bindings": 0, "by_category": {}, "by_provider": {}, "rules": len(rules)}

    for f in sorted(root.rglob("*")):
        if not f.is_file():
            continue
        lang = detect_lang(f)
        if lang is None or lang not in by_lang:
            continue
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        service = service_of(f, root)
        for i, line in enumerate(lines):
            for r in by_lang[lang]:
                if not r.regex.search(line):
                    continue
                if r.resolution == "inject":
                    emitted = _emit_inject(r, lines, i, f, service, nodes, edges)
                else:
                    emitted = _emit_next_def(r, lines, i, f, service, nodes, edges)
                if not emitted:
                    continue
                stats["bindings"] += 1
                stats["by_category"][r.category] = stats["by_category"].get(r.category, 0) + 1
                stats["by_provider"][r.provider] = stats["by_provider"].get(r.provider, 0) + 1

    return {"nodes": list(nodes.values()), "edges": edges, "stats": stats}


def _confidence_score(confidence: str) -> float:
    return 1.0 if confidence == "EXTRACTED" else 0.7


def _emit_next_def(r, lines, i, f, service, nodes, edges) -> bool:
    """Construct annotates the def below it (schedulers, event listeners): a
    synthetic construct node + an edge to that handler."""
    lang = detect_lang(f)
    handler, dline = next_def_after(lines, i, lang)
    if handler is None or dline is None:
        return False
    expr = paren_content(lines[i])
    meta: dict[str, Any] = {"provider": r.provider, "category": r.category,
                            "expr": expr, "service": service, "rule": r.id}
    meta.update(r.extra_meta)
    if r.category == "scheduler" and "trigger" not in meta:
        meta["trigger"] = _derive_trigger(r.provider, expr)
    sid = f"{r.node_kind}_{_sanitize_id(service)}_{_sanitize_id(handler)}_l{i + 1}"
    hid = _fn_id(service, handler)
    nodes[sid] = {
        "id": sid, "label": expr or f"{r.provider}:{r.node_kind}",
        "file_type": "code", "kind": r.node_kind,
        "source_file": str(f), "source_location": f"L{i + 1}",
        "metadata": meta,
    }
    nodes.setdefault(hid, {
        "id": hid, "label": f"{handler}()", "file_type": "code",
        "kind": "function", "source_file": str(f),
        "source_location": f"L{dline + 1}", "metadata": {"service": service},
    })
    edges.append({
        "source": sid, "target": hid, "relation": r.relation,
        "confidence": r.confidence, "confidence_score": _confidence_score(r.confidence),
        "source_file": str(f), "source_location": f"L{i + 1}",
        "context": r.category, "metadata": {"provider": r.provider, "expr": expr, "rule": r.id},
    })
    return True


def _emit_inject(r, lines, i, f, service, nodes, edges) -> bool:
    """Dependency injection: the annotated member's enclosing class depends on the
    injected type. Emits the class node (folds onto AST) + an 'injects' edge whose
    raw type-name target ``reconcile_contract`` resolves to the type's node."""
    lang = detect_lang(f)
    cls_name, cls_line = enclosing_class(lines, i, lang)
    inj_type = injected_type(lines, i, lang)
    if not cls_name or not inj_type:
        return False
    cid = f"svc_{_sanitize_id(service)}_cls_{_sanitize_id(cls_name)}"
    nodes.setdefault(cid, {
        "id": cid, "label": cls_name, "file_type": "code", "kind": "class",
        "source_file": str(f), "source_location": f"L{(cls_line or i) + 1}",
        "metadata": {"service": service},
    })
    edges.append({
        "source": cid, "target": inj_type, "relation": r.relation,
        "confidence": r.confidence, "confidence_score": _confidence_score(r.confidence),
        "source_file": str(f), "source_location": f"L{i + 1}",
        "context": r.category,
        "metadata": {"provider": r.provider, "injected_type": inj_type, "rule": r.id},
    })
    return True
