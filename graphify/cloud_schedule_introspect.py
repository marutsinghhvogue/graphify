"""cloud_schedule_introspect.py — Tier-B (cloud/IaC) scheduler detection.

The schedules the compiler AND the in-code binding engine can't see: those bound
at the *infrastructure* layer, where the trigger lives in Terraform / a Kubernetes
manifest / serverless.yml, not in application code (a Lambda handler has no marker
saying it runs at 02:00 — the cron is in the EventBridge rule). See
docs/scheduler-detection-design.md.

Detects the schedule declaration + its cron/rate expression across:
  Terraform   aws_cloudwatch_event_rule, aws_scheduler_schedule (AWS),
              google_cloud_scheduler_job (GCP), azurerm_logic_app_trigger_recurrence
  Kubernetes  kind: CronJob  (spec.schedule)
  serverless  functions.*.events[].schedule  (+ the handler it names)

Emits ``schedule`` nodes tagged **INFERRED** (cloud config is less certain than an
in-code decorator, and the handler binding often crosses the config↔code boundary
opaquely) with a best-effort ``triggers`` edge to the target it names. Terraform
is scanned by regex (dependency-free); YAML via a lazy PyYAML import — if PyYAML
isn't installed, Terraform still works and YAML is skipped with a note.

Deliberately best-effort on handler resolution: EventBridge→Lambda and k8s
container targets are opaque without cross-resource/ARN resolution, so those
edges point at a raw target hint (kept, never dropped). ClickOps schedules (only
in the live cloud account) are invisible to any static scan — a live cloud-API
tier is the future upgrade.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

_PROVIDER_BY_TF = {
    "aws_cloudwatch_event_rule": ("aws", "eventbridge"),
    "aws_scheduler_schedule": ("aws", "scheduler"),
    "google_cloud_scheduler_job": ("gcp", "cloud_scheduler"),
    "azurerm_logic_app_trigger_recurrence": ("azure", "logic_app"),
}
_TF_RESOURCE = re.compile(r'resource\s+"([a-z0-9_]+)"\s+"([^"]+)"\s*\{')
_TF_ATTR = re.compile(r'^\s*([a-z0-9_]+)\s*=\s*(.+?)\s*$')
# A Terraform cross-resource reference: `aws_lambda_function.rollup.arn` → (type, name).
_TF_REF = re.compile(r'([a-z0-9_]+)\.([A-Za-z0-9_-]+)\.\w+')


def _service_of(f: Path, root: Path) -> str:
    try:
        parts = f.relative_to(root).parts
    except ValueError:
        return root.name or "app"
    return parts[0] if len(parts) > 1 else (root.name or "app")


def _sid(provider: str, name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return f"cloudsched_{provider}_{slug}"


def _schedule_node(sid: str, expr: str, provider: str, trigger: str,
                   service: str, path: str, line: int) -> dict:
    return {
        "id": sid, "label": expr or f"{provider}:{trigger}", "file_type": "code",
        "kind": "schedule", "source_file": path, "source_location": f"L{line}",
        "metadata": {"provider": provider, "trigger": trigger, "expr": expr,
                     "service": service, "source": "cloud"},
    }


def _triggers_edge(sid: str, target: str, provider: str, path: str, line: int) -> dict:
    return {
        "source": sid, "target": target, "relation": "triggers",
        "confidence": "INFERRED", "confidence_score": 0.7,
        "source_file": path, "source_location": f"L{line}", "context": "cloud_schedule",
        "metadata": {"provider": provider, "target_hint": target},
    }


def _parse_tf_resources(text: str, path: str, service: str) -> list[dict]:
    """Parse every ``resource "type" "name" { … }`` block into
    ``{type, name, attrs, path, line, service}``, flattening one level of nested
    blocks (so a `target { arn = … }` still surfaces `arn`)."""
    lines = text.splitlines()
    out: list[dict] = []
    for m in _TF_RESOURCE.finditer(text):
        rtype, rname = m.group(1), m.group(2)
        start = text[:m.start()].count("\n")
        attrs: dict[str, str] = {}
        depth = 0
        for j in range(start, len(lines)):
            depth += lines[j].count("{") - lines[j].count("}")
            am = _TF_ATTR.match(lines[j])
            if am and am.group(1) not in attrs:
                attrs[am.group(1)] = am.group(2)
            if depth <= 0 and j > start:
                break
        out.append({"type": rtype, "name": rname, "attrs": attrs,
                    "path": path, "line": start + 1, "service": service})
    return out


def _ref(rhs: str) -> tuple[str | None, str | None]:
    m = _TF_REF.search(rhs or "")
    return (m.group(1), m.group(2)) if m else (None, None)


def _handler_func(handler: str) -> str:
    """Function name from a Lambda handler string: `src/jobs.rollup_daily` →
    `rollup_daily` (the part after the last dot — module.export / module.func)."""
    h = handler.strip().strip('"')
    return h.rsplit(".", 1)[-1] if "." in h else h


def _resolve_terraform(resources: list[dict], nodes: dict, edges: list, stats: dict) -> None:
    """Emit schedule nodes for scheduler resources, and — following the module's
    reference chain (rule → target → lambda.handler, or scheduler.target.arn →
    lambda.handler) — a `triggers` edge to the handler function so blast radius can
    cross infra→code (resolved onto the AST node by reconcile_contract)."""
    lambdas = {r["name"]: r["attrs"].get("handler", "").strip('"')
               for r in resources if r["type"] == "aws_lambda_function"}
    rule_to_lambda: dict[str, str] = {}
    for r in resources:
        if r["type"] == "aws_cloudwatch_event_target":
            _rt, rule_name = _ref(r["attrs"].get("rule", ""))
            ltype, lname = _ref(r["attrs"].get("arn", ""))
            if rule_name and ltype == "aws_lambda_function" and lname:
                rule_to_lambda[rule_name] = lname

    for r in resources:
        if r["type"] not in _PROVIDER_BY_TF:
            continue
        provider, trigger = _PROVIDER_BY_TF[r["type"]]
        expr = (r["attrs"].get("schedule_expression")
                or r["attrs"].get("schedule") or "").strip().strip('"')
        sid = _sid(provider, r["name"])
        nodes.setdefault(sid, _schedule_node(sid, expr, provider, trigger,
                                             r["service"], r["path"], r["line"]))
        stats["schedules"] += 1
        stats["by_provider"][provider] = stats["by_provider"].get(provider, 0) + 1

        # follow the reference chain to the Lambda handler (AWS only)
        handler = ""
        if r["type"] == "aws_cloudwatch_event_rule":
            handler = lambdas.get(rule_to_lambda.get(r["name"], ""), "")
        elif r["type"] == "aws_scheduler_schedule":
            ltype, lname = _ref(r["attrs"].get("arn", ""))
            if ltype == "aws_lambda_function" and lname:
                handler = lambdas.get(lname, "")
        if handler:
            func = _handler_func(handler)
            e = _triggers_edge(sid, func, provider, r["path"], r["line"])
            e["metadata"].update(handler=handler, resolved=True)
            edges.append(e)
            stats["resolved"] = stats.get("resolved", 0) + 1


def _scan_k8s_and_serverless(text: str, path: str, service: str,
                             nodes: dict, edges: list, stats: dict) -> bool:
    """Returns True if YAML was parsed. k8s CronJob + serverless schedule."""
    try:
        import yaml
    except ImportError:
        return False
    try:
        docs = list(yaml.safe_load_all(text))
    except Exception:
        return True  # unparseable YAML — counted as attempted, skipped
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        # Kubernetes CronJob
        if doc.get("kind") == "CronJob":
            name = (doc.get("metadata") or {}).get("name", "cronjob")
            schedule = str((doc.get("spec") or {}).get("schedule", ""))
            sid = _sid("kubernetes", name)
            nodes.setdefault(sid, _schedule_node(sid, schedule, "kubernetes",
                                                "cronjob", service, path, 1))
            stats["schedules"] += 1
            stats["by_provider"]["kubernetes"] = stats["by_provider"].get("kubernetes", 0) + 1
        # serverless.yml functions with a schedule event
        funcs = doc.get("functions")
        if isinstance(funcs, dict):
            for fname, fdef in funcs.items():
                if not isinstance(fdef, dict):
                    continue
                handler = str(fdef.get("handler") or fname)
                for ev in fdef.get("events") or []:
                    if not (isinstance(ev, dict) and "schedule" in ev):
                        continue
                    sched = ev["schedule"]
                    expr = sched if isinstance(sched, str) else str(
                        (sched or {}).get("rate") or (sched or {}).get("cron") or sched)
                    sid = _sid("serverless", f"{fname}")
                    nodes.setdefault(sid, _schedule_node(sid, expr, "serverless",
                                                        "schedule", service, path, 1))
                    # target the handler *function* so it resolves onto the AST node
                    e = _triggers_edge(sid, _handler_func(handler), "serverless", path, 1)
                    e["metadata"].update(handler=handler, resolved=True)
                    edges.append(e)
                    stats["schedules"] += 1
                    stats["resolved"] = stats.get("resolved", 0) + 1
                    stats["by_provider"]["serverless"] = stats["by_provider"].get("serverless", 0) + 1
    return True


def cloud_schedule_graph(root: str | Path) -> dict[str, Any]:
    """Scan ``root`` for cloud/IaC schedule declarations and emit
    ``{nodes, edges, stats}``: a ``schedule`` node per declaration (INFERRED) and
    a best-effort ``triggers`` edge to the handler it names."""
    root = Path(root)
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    stats: dict[str, Any] = {"schedules": 0, "by_provider": {}, "resolved": 0,
                             "yaml_supported": True}

    # Collect Terraform resources across the whole module first, so a rule → target
    # → lambda chain resolves even when the resources live in different .tf files.
    tf_resources: list[dict] = []
    yaml_seen = False
    yaml_ok = True
    for f in sorted(root.rglob("*")):
        if not f.is_file():
            continue
        suf = f.suffix.lower()
        if suf not in (".tf", ".yaml", ".yml"):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        service = _service_of(f, root)
        if suf == ".tf":
            tf_resources += _parse_tf_resources(text, str(f), service)
        else:
            yaml_seen = True
            yaml_ok = _scan_k8s_and_serverless(text, str(f), service, nodes, edges, stats)

    _resolve_terraform(tf_resources, nodes, edges, stats)

    if yaml_seen and not yaml_ok:
        stats["yaml_supported"] = False
        print("[graphify cloud-schedulers] note: PyYAML not installed — k8s/serverless "
              "YAML skipped (Terraform still scanned). pip install pyyaml", file=sys.stderr)

    return {"nodes": list(nodes.values()), "edges": edges, "stats": stats}
