"""Flask JSON API: query + human-in-the-loop review endpoints."""
from __future__ import annotations

import json

import pytest
from networkx.readwrite import json_graph

from graphify.build import build_from_json

pytest.importorskip("flask")

from graphify.webapi import create_app  # noqa: E402


@pytest.fixture
def graph_file(tmp_path):
    extraction = {
        "nodes": [
            {"id": "alpha", "label": "alpha", "file_type": "code", "source_file": "a.py"},
            {"id": "beta", "label": "beta", "file_type": "code", "source_file": "b.py"},
            {"id": "readme_user", "label": "User", "file_type": "document", "source_file": "README.md"},
        ],
        "edges": [
            {"source": "alpha", "target": "beta", "relation": "calls", "confidence": "EXTRACTED"},
            {
                "source": "readme_user",
                "target": "beta",
                "relation": "conceptually_related_to",
                "confidence": "INFERRED",
                "confidence_score": 0.6,
            },
        ],
    }
    G = build_from_json(extraction, directed=True, root=tmp_path)
    data = json_graph.node_link_data(G, edges="links")
    path = tmp_path / "graph.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path, tmp_path


@pytest.fixture
def client(graph_file):
    path, root = graph_file
    app = create_app(str(path), root=str(root))
    app.config.update(TESTING=True)
    return app.test_client()


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.get_json()["status"] == "ok"


def test_query(client):
    r = client.get("/api/query?q=alpha")
    assert r.status_code == 200
    assert "result" in r.get_json()


def test_query_missing_param(client):
    assert client.get("/api/query").status_code == 400


def test_callers_and_callees(client):
    callers = client.get("/api/callers?label=beta").get_json()["result"]
    assert "alpha" in callers
    callees = client.get("/api/callees?label=alpha").get_json()["result"]
    assert "beta" in callees


def test_nodes(client):
    matches = client.get("/api/nodes?label=User").get_json()["matches"]
    assert any(m["id"] == "readme_user" for m in matches)


def test_uncertain_edges(client):
    body = client.get("/api/review/uncertain-edges").get_json()
    assert body["count"] == 1
    assert body["edges"][0]["confidence"] == "INFERRED"
    assert body["edges"][0]["relation"] == "conceptually_related_to"


def test_review_questions(client):
    body = client.get("/api/review/questions").get_json()
    assert "questions" in body
    assert isinstance(body["questions"], list)


def test_alias_get_post_roundtrip(client, graph_file):
    _, root = graph_file
    assert client.get("/api/aliases").get_json()["aliases"] == []

    r = client.post("/api/aliases", json={"from": "User", "to": "beta", "mode": "same_as", "reason": "test"})
    assert r.status_code == 201
    assert r.get_json()["alias"]["from"] == "User"

    aliases = client.get("/api/aliases").get_json()["aliases"]
    assert len(aliases) == 1
    assert aliases[0]["to"] == "beta"
    # Persisted to the alias file under root.
    assert (root / ".graphify_aliases.json").exists()


def test_alias_post_validation(client):
    assert client.post("/api/aliases", json={"from": "", "to": "x"}).status_code == 400
    assert client.post("/api/aliases", json={"from": "a", "to": "b", "mode": "bad"}).status_code == 400


# --- v1 REST API (structured, for the embeddable UI) ---

def test_v1_impact(client):
    # alpha --calls--> beta, so changing beta impacts alpha (reverse reachability)
    r = client.get("/api/v1/impact?label=beta&depth=2")
    assert r.status_code == 200
    body = r.get_json()
    assert body["seed"]["id"] == "beta"
    affected_ids = {a["id"] for a in body["affected"]}
    assert "alpha" in affected_ids
    a = next(x for x in body["affected"] if x["id"] == "alpha")
    assert a["via_relation"] == "calls" and a["depth"] == 1


def test_v1_impact_unknown_label(client):
    assert client.get("/api/v1/impact?label=zzzznope").status_code == 404
    assert client.get("/api/v1/impact").status_code == 400


def test_v1_seeds(client):
    r = client.get("/api/v1/seeds?q=alpha&top=3")
    assert r.status_code == 200
    seeds = r.get_json()["seeds"]
    assert seeds and seeds[0]["name"] == "alpha"


def test_v1_subgraph_returns_nodes_and_edges(client):
    r = client.get("/api/v1/subgraph?label=alpha&depth=1")
    assert r.status_code == 200
    body = r.get_json()
    ids = {n["id"] for n in body["nodes"]}
    assert {"alpha", "beta"} <= ids
    assert any(e["relation"] == "calls" for e in body["edges"])


def test_v1_stats(client):
    body = client.get("/api/v1/stats").get_json()
    assert body["nodes"] == 3 and body["edges"] == 2
    assert body["confidence"].get("EXTRACTED", 0) >= 1


def test_api_key_gates_endpoints(graph_file):
    path, root = graph_file
    app = create_app(str(path), root=str(root))
    app.config.update(TESTING=True, GRAPHIFY_API_KEY="secret")
    c = app.test_client()
    assert c.get("/health").status_code == 200                 # health never gated
    assert c.get("/api/v1/stats").status_code == 401           # missing key
    assert c.get("/api/v1/stats", headers={"X-API-Key": "secret"}).status_code == 200
    assert c.get("/api/v1/stats", headers={"Authorization": "Bearer secret"}).status_code == 200


def test_cors_header_present(client):
    r = client.get("/api/v1/stats", headers={"Origin": "http://host.app"})
    assert r.headers.get("Access-Control-Allow-Origin") == "*"


@pytest.fixture
def taint_graph_file(tmp_path):
    """A graph carrying taint `flows_to` edges, as `extract --taint` would emit."""
    from graphify.taint import findings_to_graph, taint_scan

    (tmp_path / "db.py").write_text("def run_query(cursor, sql):\n    cursor.execute(sql)\n")
    (tmp_path / "views.py").write_text(
        "from db import run_query\n"
        "def handler(request, cursor):\n"
        "    uid = request.args.get('id')\n"
        "    q = 'SELECT ' + uid\n"
        "    run_query(cursor, q)\n"
    )
    extraction = findings_to_graph(taint_scan(tmp_path)["findings"])
    G = build_from_json(extraction, directed=True, root=tmp_path)
    data = json_graph.node_link_data(G, edges="links")
    path = tmp_path / "graph.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path, tmp_path


@pytest.fixture
def taint_client(taint_graph_file):
    path, root = taint_graph_file
    app = create_app(str(path), root=str(root))
    app.config.update(TESTING=True)
    return app.test_client()


def test_v1_taint_lists_findings(taint_client):
    body = taint_client.get("/api/v1/taint").get_json()
    assert body["count"] == 1
    assert body["by_vuln"] == {"sql_injection": 1}
    f = body["findings"][0]
    assert f["vuln"] == "sql_injection" and f["category"] == "untrusted-input"
    assert f["confidence"] == "INFERRED"
    assert f["cross_function"] is True and f["callee"] == "run_query"
    assert "request.args.get" in f["source"]["text"]
    assert f["sink"]["callee"] == "run_query"


def test_v1_taint_filter_by_vuln(taint_client):
    assert taint_client.get("/api/v1/taint?vuln=sql_injection").get_json()["count"] == 1
    assert taint_client.get("/api/v1/taint?vuln=command_injection").get_json()["count"] == 0


def test_v1_taint_empty_when_no_flows(client):
    # the default fixture graph has no flows_to edges
    body = client.get("/api/v1/taint").get_json()
    assert body["count"] == 0 and body["findings"] == []
