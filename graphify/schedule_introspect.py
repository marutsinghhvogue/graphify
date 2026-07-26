"""schedule_introspect.py — Tier-A (in-code) scheduler detection.

Thin adapter over the generic binding-rule engine (``binding_rules``): schedulers
are just the ``scheduler`` category of binding rules. This module preserves the
``schedule_graph`` API/output while the detection itself is now data-driven and
shares one engine with event listeners and any user/LLM-authored construct.

Detects decorator/annotation schedulers whose trigger binds the handler locally
in source (Spring ``@Scheduled``, NestJS ``@Cron``/``@Interval``/``@Timeout``,
APScheduler ``@scheduled_job``, Celery ``@periodic_task``) and emits a
``schedule`` node + an EXTRACTED ``triggers`` edge to the handler. Handler nodes
fold onto the tree-sitter AST via ``reconcile_contract`` so ``blast_radius``
surfaces "what runs this on a timer". Tier B (cloud/IaC) remains designed, not
built — see docs/scheduler-detection-design.md.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from graphify.binding_rules import run_bindings


def schedule_graph(root: str | Path) -> dict[str, Any]:
    """Scan ``root`` for in-code schedulers and emit ``{nodes, edges, stats}`` in
    graphify schema (``schedule`` nodes + ``triggers`` edges). Backed by the
    binding-rule engine, scheduler category only."""
    result = run_bindings(root, categories=["scheduler"])
    st = result["stats"]
    # Preserve the historical scheduler-facing stats shape.
    result["stats"] = {
        "schedules": st.get("bindings", 0),
        "by_provider": st.get("by_provider", {}),
    }
    return result
