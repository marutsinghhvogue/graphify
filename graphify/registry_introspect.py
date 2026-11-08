"""registry_introspect.py — cross-service event edges from an event-routing registry.

Some estates route events at runtime: the publisher asks a registry service for
the subscribers of an event type and delivers to each subscription URL
(it looks up the subscribers of an event type and POSTs to each URL). The publisher's
code never names a receiver, so no static parse of it, and no SCIP symbol, can
link the two. The registry's own data can: a ``producers`` table
(service, event_type) and a ``subscriptions`` table (service, event_type, url,
method). This module reads it and resolves both ends to code:

* **subscriber** — the subscription URL's path and method name an endpoint in the
  subscribing service, resolved by the cross-service contract layer's endpoint
  catalog (context paths included) to its handler;
* **publisher** — the code in the producing service that declares the event type:
  ``return "ET"`` / an ``EVENT_TYPE``-style constant (INFERRED 0.85), else a use of
  an enum constant ``X.ET`` or the bare literal (INFERRED 0.65).

Edges follow the estate's dependent → dependency direction, so blast radius from
a publisher reaches every subscriber handler:

    subscriber handler --subscribes_to--> event:ET --published_by--> publisher

Sources: a SQL seed/dump (``INSERT INTO producers|subscriptions (cols) VALUES``),
a YAML/JSON export (``{producers: [...], subscriptions: [...]}``), or a Postgres
DSN (``postgresql://…``) to read the live registry's two tables.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from graphify.contract_introspect import (
    ANY_METHOD,
    _collect,
    _enclosing,
    _fn_id,
    _function_index,
    _is_test_path,
    normalize_path,
)

# ── registry model + loaders ──────────────────────────────────────────────────


@dataclass(frozen=True)
class Subscription:
    service: str
    event_type: str
    url: str
    method: str


@dataclass
class Registry:
    producers: dict[str, set[str]] = field(default_factory=dict)   # event_type → services
    subscriptions: list[Subscription] = field(default_factory=list)
    source: str = ""


def _sql_values(body: str) -> list[list[str | None]]:
    """Rows of a ``VALUES (…), (…)`` body. String literals are unquoted; any other
    expression (``NOW()``, ``some_fn()::text``) becomes ``None``. SQL ``--``
    comments are skipped."""
    rows: list[list[str | None]] = []
    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        if body.startswith("--", i):
            i = body.find("\n", i)
            i = n if i < 0 else i
            continue
        if ch != "(":
            i += 1
            continue
        row: list[str | None] = []
        depth, cur, is_lit, i = 0, "", False, i + 1
        while i < n:
            ch = body[i]
            if ch == "'":
                j = i + 1
                lit = []
                while j < n:
                    if body[j] == "'" and j + 1 < n and body[j + 1] == "'":
                        lit.append("'"); j += 2; continue
                    if body[j] == "'":
                        break
                    lit.append(body[j]); j += 1
                cur, is_lit, i = "".join(lit), True, j + 1
                continue
            if ch == "(":
                depth += 1
            elif ch == ")" and depth:
                depth -= 1
            elif (ch == "," or ch == ")") and depth == 0:
                row.append(cur if is_lit else None)
                cur, is_lit = "", False
                if ch == ")":
                    i += 1
                    break
            i += 1
        rows.append(row)
    return rows


_INSERT = re.compile(r"INSERT\s+INTO\s+([\w.\"]+)\s*\(([^)]*)\)\s*VALUES(.*?);", re.I | re.S)


def _rows_from_sql(text: str) -> dict[str, list[dict[str, str | None]]]:
    tables: dict[str, list[dict[str, str | None]]] = {}
    for m in _INSERT.finditer(text):
        table = m.group(1).replace('"', "").rsplit(".", 1)[-1].lower()
        cols = [c.strip().strip('"').lower() for c in m.group(2).split(",")]
        for vals in _sql_values(m.group(3)):
            tables.setdefault(table, []).append(dict(zip(cols, vals)))
    return tables


def _rows_from_dsn(dsn: str) -> dict[str, list[dict[str, str | None]]]:
    try:
        import psycopg
    except ImportError:  # pragma: no cover - exercised only without the extra
        raise ImportError("reading a live routing registry needs psycopg: "
                          "pip install 'graphifyy[postgres]'") from None
    tables: dict[str, list[dict[str, str | None]]] = {}
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        for table in ("producers", "subscriptions"):
            cur.execute(f"SELECT * FROM {table}")  # noqa: S608 - fixed table names
            cols = [d.name.lower() for d in cur.description]
            tables[table] = [dict(zip(cols, (None if v is None else str(v) for v in row)))
                             for row in cur.fetchall()]
    return tables


def _pick(row: dict, *keys: str) -> str:
    for k in keys:
        if row.get(k):
            return str(row[k]).strip()
    return ""


def load_registry(source: str | Path) -> Registry:
    """Read producers + subscriptions from a SQL file, a YAML/JSON export, or a
    Postgres DSN. Column/key names: ``service_name``|``service``, ``event_type``,
    ``url``, ``method`` (default POST)."""
    src = str(source)
    if src.startswith(("postgresql://", "postgres://")):
        tables = _rows_from_dsn(src)
    else:
        path = Path(src)
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix.lower() == ".sql":
            tables = _rows_from_sql(text)
        elif path.suffix.lower() == ".json":
            tables = {k.lower(): v for k, v in json.loads(text).items()}
        else:
            import yaml
            tables = {k.lower(): v for k, v in (yaml.safe_load(text) or {}).items()}
    reg = Registry(source=src)
    for row in tables.get("producers", []):
        svc, et = _pick(row, "service_name", "service"), _pick(row, "event_type", "eventtype")
        if svc and et:
            reg.producers.setdefault(et, set()).add(svc)
    for row in tables.get("subscriptions", []):
        svc, et = _pick(row, "service_name", "service"), _pick(row, "event_type", "eventtype")
        if svc and et:
            reg.subscriptions.append(Subscription(svc, et, _pick(row, "url", "endpoint"),
                                                  (_pick(row, "method") or "POST").upper()))
    return reg


# ── publisher anchors ─────────────────────────────────────────────────────────

_INLINE_DEF = re.compile(r"\b(?:public|protected|private)\s+[\w<>\[\], ?]+\s+(\w+)\s*\([^)]*\)\s*\{")
_JAVA_TYPE = re.compile(r"^\s*(?:(?:public|protected|private|abstract|final|static|sealed)\s+)*"
                        r"(?:class|interface|enum|record)\s+(\w+)")


def _publisher_anchors(svc_dir: Path, event_type: str) -> tuple[list[tuple[Path, str, str, int]], float]:
    """``([(file, kind, name, line)], confidence)`` for the code in one service that
    declares ``event_type``. Strongest strategy that finds anything wins."""
    et = re.escape(event_type)
    declares = [re.compile(rf'return\s+(?:"{et}"|[A-Z]\w*\.{et})\s*;'),
                re.compile(rf'\b(?:[A-Z][A-Z0-9_]*TYPE\w*|eventType|EVENT)\s*[=:(,]\s*"{et}"')]
    enum_use = re.compile(rf"\b[A-Z]\w*\.{et}\b")
    literal = re.compile(rf'"{et}"')
    strong: list = []
    weak: list = []
    for f in sorted(svc_dir.rglob("*.java")):
        if _is_test_path(f.relative_to(svc_dir)):
            continue
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        if not any(event_type in ln for ln in lines):
            continue
        defs = _function_index(lines, "java")
        types = [(i, m.group(1)) for i, ln in enumerate(lines) if (m := _JAVA_TYPE.match(ln))]
        is_endpoint = any("Mapping(" in ln for ln in lines)
        for i, ln in enumerate(lines):
            if any(p.search(ln) for p in declares):
                # a one-line method (`@Override public String getEventType() { return …; }`)
                # declares itself; the def index misses it behind the annotation
                inline = _INLINE_DEF.search(ln)
                fn = inline.group(1) if inline else _enclosing(defs, i)
                if fn != "<module>":
                    strong.append((f, "function", fn, i + 1))
                else:
                    owner = _enclosing(types, i)
                    if owner != "<module>":
                        strong.append((f, "class", owner, i + 1))
            elif not is_endpoint and (enum_use.search(ln) or literal.search(ln)):
                fn = _enclosing(defs, i)
                if fn != "<module>":
                    weak.append((f, "function", fn, i + 1))
    return (strong, 0.85) if strong else (weak, 0.65)


# ── graph ─────────────────────────────────────────────────────────────────────


def _event_id(event_type: str) -> str:
    return "event_" + re.sub(r"[^a-z0-9]+", "_", event_type.lower()).strip("_")


def registry_graph(root: str | Path, source: str | Path) -> dict[str, Any]:
    """Event nodes plus ``subscribes_to`` / ``published_by`` edges for every event
    type in the registry, resolved to handler and publisher code under ``root``
    (each immediate subdirectory is a service, as for ``--cross-service``)."""
    root = Path(root)
    reg = load_registry(source)
    endpoints, _calls, names = _collect(root)
    dir_of: dict[str, str] = {}
    for svc_dir, svc_names in names.items():
        for nm in svc_names:
            dir_of.setdefault(nm, svc_dir)

    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    stats: dict[str, Any] = {"event_types": 0, "subscriptions": len(reg.subscriptions),
                             "subscriptions_resolved": 0, "publishers_anchored": 0,
                             "unresolved_subscriptions": [], "unanchored_event_types": []}
    src_file = reg.source if not reg.source.startswith(("postgres://", "postgresql://")) else ""

    event_types = sorted(set(reg.producers) | {s.event_type for s in reg.subscriptions})
    stats["event_types"] = len(event_types)
    for et in event_types:
        nodes[_event_id(et)] = {
            "id": _event_id(et), "label": et, "file_type": "code", "kind": "event",
            "source_file": src_file, "source_location": "",
            "metadata": {"provider": "routing_registry", "event_type": et,
                         "producers": sorted(reg.producers.get(et, ())),
                         "subscribers": sorted({s.service for s in reg.subscriptions if s.event_type == et})},
        }

    def _fn_node(service: str, fn: str, f: str, line: int, kind: str, anchor: str) -> str:
        # File-qualified: every event class has its own getEventType(), and a
        # service-wide id would fold them all into one node.
        stem = re.sub(r"[^a-z0-9]+", "_", Path(f).stem.lower())
        nid = f"{_fn_id(service, fn)}__{stem}" if kind == "function" else f"svc_{service}_cls_{fn}__{stem}".lower()
        nodes.setdefault(nid, {"id": nid, "label": f"{fn}()" if kind == "function" else fn,
                               "file_type": "code", "kind": kind, "source_file": f,
                               "source_location": f"L{line}",
                               "metadata": {"service": service, "anchor": anchor}})
        return nid

    # subscribers: URL path + method → the subscribing service's endpoint handler
    for sub in reg.subscriptions:
        svc_dir = dir_of.get(sub.service, sub.service)
        path = normalize_path(urlparse(sub.url).path or sub.url)
        hits = [e for e in endpoints
                if e.service == svc_dir and e.path == path and e.method in (sub.method, ANY_METHOD)]
        if len(hits) != 1:
            stats["unresolved_subscriptions"].append(f"{sub.service} {sub.method} {path} ({sub.event_type})")
            continue
        e = hits[0]
        hid = _fn_node(e.service, e.handler, e.source_file, e.line, "function", "next")
        edges.append({"source": hid, "target": _event_id(sub.event_type), "relation": "subscribes_to",
                      "confidence": "EXTRACTED", "confidence_score": 1.0,
                      "source_file": e.source_file, "source_location": f"L{e.line}",
                      "context": "routing_registry",
                      "metadata": {"event_type": sub.event_type, "url": sub.url, "method": sub.method}})
        stats["subscriptions_resolved"] += 1

    # publishers: the code in each producing service that declares the event type
    for et, services in sorted(reg.producers.items()):
        anchored = False
        for svc in sorted(services):
            svc_dir = root / dir_of.get(svc, svc)
            if not svc_dir.is_dir():
                continue
            anchors, score = _publisher_anchors(svc_dir, et)
            for f, kind, name, line in anchors:
                pid = _fn_node(svc_dir.name, name, str(f), line, kind, "enclosing" if kind == "function" else "next")
                edges.append({"source": _event_id(et), "target": pid, "relation": "published_by",
                              "confidence": "INFERRED", "confidence_score": score,
                              "source_file": str(f), "source_location": f"L{line}",
                              "context": "routing_registry", "metadata": {"event_type": et}})
                anchored = True
        if anchored:
            stats["publishers_anchored"] += 1
        else:
            stats["unanchored_event_types"].append(et)

    # one edge per (source, target, relation)
    seen: set[tuple] = set()
    unique = []
    for e in edges:
        k = (e["source"], e["target"], e["relation"])
        if k not in seen:
            seen.add(k)
            unique.append(e)
    return {"nodes": list(nodes.values()), "edges": unique, "stats": stats}
