"""Event-routing registry → subscribes_to / published_by edges resolved to code."""
from __future__ import annotations

from pathlib import Path

from graphify.registry_introspect import _sql_values, load_registry, registry_graph

SEED = """-- registry seed
INSERT INTO producers (id, service_name, event_type, created_at)
VALUES
  -- orders
  ('p1', 'orders-service', 'PARCEL_BOOKED', NOW()),
  ('p2', 'orders-service', 'INVOICE_DUE',  NOW());

INSERT INTO subscriptions (id, service_name, event_type, url, method, created_at)
VALUES
  ('s1', 'shipping-service', 'PARCEL_BOOKED',
   'https://shipping.example.com/events/parcel-booked', 'POST', NOW()),
  ('s2', 'billing-service', 'INVOICE_DUE',
   'http://billing.internal:8080/events/invoice-due', 'POST', NOW());
"""


def _w(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _estate(root: Path) -> Path:
    # publisher: each event class declares its type; two classes, two getEventType()s
    _w(root, "orders-service/src/main/java/o/ParcelBooked.java",
       "public record ParcelBooked(String id) implements Event {\n"
       "    @Override public String getEventType() { return Events.PARCEL_BOOKED; }\n"
       "}\n")
    _w(root, "orders-service/src/main/java/o/InvoiceDue.java",
       "public class InvoiceDue implements Event {\n"
       "    public String getEventType() {\n"
       '        return "INVOICE_DUE";\n'
       "    }\n"
       "}\n")
    # subscriber: class-level prefix + method mapping
    _w(root, "shipping-service/src/main/java/s/EventsController.java",
       '@RequestMapping("/events")\n'
       "public class EventsController {\n"
       '    @PostMapping("/parcel-booked")\n'
       "    public void onParcelBooked(String body) {}\n"
       "}\n")
    # billing-service exposes a different path than the registry says: drift
    _w(root, "billing-service/src/main/java/b/EventsController.java",
       '@RequestMapping("/events")\n'
       "public class EventsController {\n"
       '    @PostMapping("/invoice-due-v2")\n'
       "    public void onInvoiceDue(String body) {}\n"
       "}\n")
    _w(root.parent, "registry.sql", SEED)
    return root


def test_sql_values_handles_comments_quotes_and_functions():
    rows = _sql_values("  -- c\n (now(), 'a''b', 'x,y', fn(1, 2)::text),\n ('z', NULL)")
    assert rows == [[None, "a'b", "x,y", None], ["z", None]]


def test_load_registry_from_sql_and_yaml(tmp_path):
    sql = tmp_path / "r.sql"
    sql.write_text(SEED)
    reg = load_registry(sql)
    assert reg.producers == {"PARCEL_BOOKED": {"orders-service"}, "INVOICE_DUE": {"orders-service"}}
    assert [(s.service, s.event_type, s.method) for s in reg.subscriptions] == [
        ("shipping-service", "PARCEL_BOOKED", "POST"), ("billing-service", "INVOICE_DUE", "POST")]
    yml = tmp_path / "r.yaml"
    yml.write_text("producers:\n  - {service: a, event_type: E}\n"
                   "subscriptions:\n  - {service: b, event_type: E, url: 'http://b/events/e'}\n")
    reg = load_registry(yml)
    assert reg.producers == {"E": {"a"}} and reg.subscriptions[0].method == "POST"


def test_registry_graph_resolves_both_ends_and_reports_drift(tmp_path):
    root = _estate(tmp_path / "services")
    g = registry_graph(root, tmp_path / "registry.sql")
    st = g["stats"]
    assert (st["event_types"], st["subscriptions_resolved"], st["publishers_anchored"]) == (2, 1, 2)
    assert st["unresolved_subscriptions"] == ["billing-service POST /events/invoice-due (INVOICE_DUE)"]
    nodes = {n["id"]: n for n in g["nodes"]}
    subs = [e for e in g["edges"] if e["relation"] == "subscribes_to"]
    assert [(nodes[e["source"]]["label"], e["target"]) for e in subs] == [
        ("onParcelBooked()", "event_parcel_booked")]
    pubs = {e["source"]: e for e in g["edges"] if e["relation"] == "published_by"}
    # one getEventType() node per event class, not one per service
    targets = {nodes[pubs[ev]["target"]]["source_file"].rsplit("/", 1)[-1] for ev in pubs}
    assert targets == {"ParcelBooked.java", "InvoiceDue.java"}
    assert all(e["confidence_score"] == 0.85 for e in pubs.values())


def test_extract_with_routing_registry_crosses_the_event_bus(tmp_path):
    import os
    import subprocess
    import sys

    from graphify.affected import affected_nodes, load_graph

    root = _estate(tmp_path / "services")
    out = tmp_path / "out"
    r = subprocess.run(
        [sys.executable, "-m", "graphify", "extract", str(root), "--code-only", "--no-cluster",
         "--routing-registry", str(tmp_path / "registry.sql")],
        env={**os.environ, "GRAPHIFY_OUT": str(out)}, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr[-2000:]
    assert "1/2 subscriptions resolved to handlers" in r.stdout
    assert "registry drift" in r.stdout
    G = load_graph(out / "graph.json")
    publisher = next(n for n, d in G.nodes(data=True)
                     if (d.get("source_file") or "").endswith("ParcelBooked.java")
                     and str(d.get("label", "")).lstrip(".").startswith("getEventType"))
    hit = {G.nodes[h.node_id].get("label") for h in affected_nodes(G, publisher, depth=3)}
    assert "PARCEL_BOOKED" in hit and ".onParcelBooked()" in hit


def test_load_registry_from_a_live_postgres_dsn(monkeypatch):
    import sys
    import types

    tables = {
        "producers": (["id", "service_name", "event_type"], [("1", "orders-service", "E1")]),
        "subscriptions": (["id", "service_name", "event_type", "url", "method"],
                          [("2", "billing-service", "E1", "http://billing/events/e1", None)]),
    }

    class Cur:
        def execute(self, sql):
            self.cols, self.rows = tables[sql.rsplit(" ", 1)[-1]]
        @property
        def description(self):
            return [types.SimpleNamespace(name=c) for c in self.cols]
        def fetchall(self):
            return self.rows
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    class Conn:
        def cursor(self):
            return Cur()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setitem(sys.modules, "psycopg", types.SimpleNamespace(connect=lambda dsn: Conn()))
    reg = load_registry("postgresql://db.example/events")
    assert reg.producers == {"E1": {"orders-service"}}
    assert [(s.service, s.url, s.method) for s in reg.subscriptions] == [
        ("billing-service", "http://billing/events/e1", "POST")]
