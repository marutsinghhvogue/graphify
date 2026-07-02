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
