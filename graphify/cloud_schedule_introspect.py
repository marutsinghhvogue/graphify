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
_TF_SCHEDULE = re.compile(r'(?:schedule_expression|schedule)\s*=\s*"([^"]+)"')


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


def _scan_terraform(text: str, path: str, service: str,
                    nodes: dict, edges: list, stats: dict) -> None:
    lines = text.splitlines()
    for m in _TF_RESOURCE.finditer(text):
        rtype, rname = m.group(1), m.group(2)
        if rtype not in _PROVIDER_BY_TF:
            continue
        provider, trigger = _PROVIDER_BY_TF[rtype]
        start = text[:m.start()].count("\n")
        # read the block body (brace-balanced) for the schedule attribute
        expr = ""
        depth = 0
        for j in range(start, len(lines)):
            depth += lines[j].count("{") - lines[j].count("}")
            sm = _TF_SCHEDULE.search(lines[j])
            if sm and not expr:
                expr = sm.group(1)
            if depth <= 0 and j > start:
                break
        sid = _sid(provider, f"{rname}")
        nodes.setdefault(sid, _schedule_node(sid, expr, provider, trigger,
                                             service, path, start + 1))
        stats["schedules"] += 1
        stats["by_provider"][provider] = stats["by_provider"].get(provider, 0) + 1


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
                    edges.append(_triggers_edge(sid, handler, "serverless", path, 1))
                    stats["schedules"] += 1
                    stats["by_provider"]["serverless"] = stats["by_provider"].get("serverless", 0) + 1
    return True


def cloud_schedule_graph(root: str | Path) -> dict[str, Any]:
    """Scan ``root`` for cloud/IaC schedule declarations and emit
    ``{nodes, edges, stats}``: a ``schedule`` node per declaration (INFERRED) and
    a best-effort ``triggers`` edge to the handler it names."""
    root = Path(root)
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    stats: dict[str, Any] = {"schedules": 0, "by_provider": {}, "yaml_supported": True}

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
            _scan_terraform(text, str(f), service, nodes, edges, stats)
        else:
            yaml_seen = True
            yaml_ok = _scan_k8s_and_serverless(text, str(f), service, nodes, edges, stats)

    if yaml_seen and not yaml_ok:
        stats["yaml_supported"] = False
        print("[graphify cloud-schedulers] note: PyYAML not installed — k8s/serverless "
              "YAML skipped (Terraform still scanned). pip install pyyaml", file=sys.stderr)

    return {"nodes": list(nodes.values()), "edges": edges, "stats": stats}
