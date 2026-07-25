"""schedule_introspect.py — Tier-A (in-code) scheduler/job detection.

Finds timed entry points whose trigger binds the handler *locally in source* via
a decorator/annotation, and emits a ``schedule`` node + a ``triggers`` edge
(schedule → handler) so blast radius reaches "what runs this on a timer" — the
sibling of the cross-service ``handles``/``calls_service`` entry-point edges (see
docs/scheduler-detection-design.md).

Tier A only — the deterministic, always-fresh case where the schedule lives in
the code:

  Python  APScheduler ``@scheduler.scheduled_job(...)`` ; Celery ``@periodic_task(...)``
  Java    Spring ``@Scheduled(cron=... / fixedRate=...)``
  TS      NestJS ``@Cron(...)`` / ``@Interval(...)`` / ``@Timeout(...)``

NOT Tier B (cloud/IaC — EventBridge/Cloud Scheduler/k8s CronJob/function.json):
that trigger lives outside the code and needs a different reader. See the design
doc; deferred.

Emits the standard graphify ``{nodes, edges, stats}``. The ``triggers`` edge is
EXTRACTED (the decorator binds the handler exactly, in source). Handler function
nodes reuse the ``svc_*_fn_*`` id scheme so ``reconcile_contract`` folds them onto
the tree-sitter AST nodes for the same functions (blast radius then traverses
from a real code node into the schedule). Best-effort static regexes, Tier-A
scoped — nested-paren schedule expressions (e.g. Celery ``crontab(...)``) are
captured as raw text, not evaluated.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# ── def-finding (kept local & tiny, mirroring contract_introspect's spike style) ─

_PY_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(")
_TS_METHOD = re.compile(r"^\s*(?:public |private |protected )?(?:static )?(?:async )?(\w+)\s*\(")
_JAVA_METHOD = re.compile(r"^\s*(?:public|private|protected)\s+[\w<>\[\], ?]+\s+(\w+)\s*\(")
_NON_FN = {"if", "for", "while", "switch", "catch", "return", "constructor", "class", "new"}
_DEFS = {"python": _PY_DEF, "ts": _TS_METHOD, "java": _JAVA_METHOD}


def _next_def_after(lines: list[str], start: int, lang: str) -> tuple[str | None, int | None]:
    """Name + line index of the first function/method def at or after ``start``."""
    pat = _DEFS[lang]
    for j in range(start, len(lines)):
        m = pat.match(lines[j])
        if m and m.group(1) not in _NON_FN:
            return m.group(1), j
    return None, None


# ── schedule decorator/annotation patterns ───────────────────────────────────
# Each entry: (regex on the decorator, provider, explicit trigger or None).

# Patterns anchor to the start of the line (a real decorator/annotation leads the
# line) so a comment mentioning "@Cron"/"@Scheduled" is not a false positive.
_SCHED: dict[str, list[tuple[re.Pattern, str, str | None]]] = {
    "python": [
        (re.compile(r"^\s*@(?:\w+\.)?scheduled_job\b"), "apscheduler", None),
        (re.compile(r"^\s*@periodic_task\b"), "celery", None),
    ],
    "java": [
        (re.compile(r"^\s*@Scheduled\b"), "spring", None),
    ],
    "ts": [
        (re.compile(r"^\s*@Cron\b"), "nestjs", "cron"),
        (re.compile(r"^\s*@Interval\b"), "nestjs", "interval"),
        (re.compile(r"^\s*@Timeout\b"), "nestjs", "timeout"),
    ],
}

_LANG_BY_SUFFIX = {".py": "python", ".java": "java", ".ts": "ts", ".tsx": "ts"}
_PAREN = re.compile(r"\(([^)]*)\)")


def _detect_lang(path: Path) -> str | None:
    return _LANG_BY_SUFFIX.get(path.suffix)


def _paren_content(line: str) -> str:
    """Best-effort schedule expression: the text inside the decorator's parens on
    this line (stops at the first close paren — nested calls are truncated)."""
    m = _PAREN.search(line)
    return m.group(1).strip() if m else ""


def _derive_trigger(provider: str, expr: str) -> str:
    """Coarse trigger kind for providers that don't name it in the decorator."""
    low = expr.lower()
    if "cron" in low or "crontab" in low:
        return "cron"
    if provider == "apscheduler" and ("interval" in low or "seconds=" in low or "minutes=" in low):
        return "interval"
    if provider == "spring":
        if "cron" in low:
            return "cron"
        if "fixedrate" in low or "fixeddelay" in low:
            return "rate"
    if provider == "celery":
        return "cron" if "crontab" in low else "rate"
    return "schedule"


def _service_of(f: Path, root: Path) -> str:
    """Service = the immediate subdirectory of the scan root the file lives under
    (monorepo-of-services layout); the root's own name when the file is top-level."""
    try:
        parts = f.relative_to(root).parts
    except ValueError:
        return root.name or "app"
    return parts[0] if len(parts) > 1 else (root.name or "app")


def _sid(service: str, handler: str, line: int) -> str:
    return f"sched_{service}_{handler}_l{line}".lower()


def _fn_id(service: str, fn: str) -> str:
    return f"svc_{service}_fn_{fn}".lower()


def schedule_graph(root: str | Path) -> dict[str, Any]:
    """Scan ``root`` for in-code schedulers and emit ``{nodes, edges, stats}`` in
    graphify schema: a ``schedule`` node per trigger and a ``triggers`` edge to the
    handler function it invokes (EXTRACTED)."""
    root = Path(root)
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    stats: dict[str, Any] = {"schedules": 0, "by_provider": {}}

    for f in sorted(root.rglob("*")):
        if not f.is_file():
            continue
        lang = _detect_lang(f)
        if lang is None:
            continue
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        service = _service_of(f, root)
        for i, line in enumerate(lines):
            for pat, provider, fixed_trigger in _SCHED[lang]:
                if not pat.search(line):
                    continue
                expr = _paren_content(line)
                trigger = fixed_trigger or _derive_trigger(provider, expr)
                handler, dline = _next_def_after(lines, i, lang)
                if handler is None or dline is None:
                    continue
                sid, hid = _sid(service, handler, i + 1), _fn_id(service, handler)
                nodes[sid] = {
                    "id": sid,
                    "label": expr or f"{provider}:{trigger}",
                    "file_type": "code",
                    "kind": "schedule",
                    "source_file": str(f),
                    "source_location": f"L{i + 1}",
                    "metadata": {"provider": provider, "trigger": trigger,
                                 "expr": expr, "service": service},
                }
                nodes.setdefault(hid, {
                    "id": hid,
                    "label": f"{handler}()",
                    "file_type": "code",
                    "kind": "function",
                    "source_file": str(f),
                    "source_location": f"L{dline + 1}",
                    "metadata": {"service": service},
                })
                edges.append({
                    "source": sid, "target": hid, "relation": "triggers",
                    "confidence": "EXTRACTED", "confidence_score": 1.0,
                    "source_file": str(f), "source_location": f"L{i + 1}",
                    "context": "schedule",
                    "metadata": {"provider": provider, "trigger": trigger, "expr": expr},
                })
                stats["schedules"] += 1
                stats["by_provider"][provider] = stats["by_provider"].get(provider, 0) + 1

    return {"nodes": list(nodes.values()), "edges": edges, "stats": stats}
