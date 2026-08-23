"""Tests for graphify.hierarchy — hierarchical (module-tree) wiki generation."""

import json

import networkx as nx
import pytest

from graphify.hierarchy import (
    build_module_tree,
    to_hierarchical_wiki,
)


def _two_cluster_graph(n_per=30):
    """One community of 2*n_per nodes = two dense rings joined by a single bridge.

    Large enough (> default budget 40) to force a sub-partition into two children.
    """
    G = nx.Graph()
    a = [f"a{i}" for i in range(n_per)]
    b = [f"b{i}" for i in range(n_per)]
    for grp, src in ((a, "alpha.py"), (b, "beta.py")):
        for nid in grp:
            G.add_node(nid, label=nid, file_type="code", source_file=src)
        for i in range(len(grp)):
            # ring + chords → dense, cohesive cluster
            G.add_edge(grp[i], grp[(i + 1) % len(grp)], relation="calls", confidence="EXTRACTED")
            G.add_edge(grp[i], grp[(i + 2) % len(grp)], relation="calls", confidence="EXTRACTED")
    G.add_edge("a0", "b0", relation="references", confidence="INFERRED")  # the bridge
    return G, a + b


def _small_graph():
    G = nx.Graph()
    nodes = [f"c{i}" for i in range(5)]
    for nid in nodes:
        G.add_node(nid, label=nid, file_type="code", source_file="c.py")
    for i in range(len(nodes) - 1):
        G.add_edge(nodes[i], nodes[i + 1], relation="calls", confidence="EXTRACTED")
    return G, nodes


# --- decomposition -------------------------------------------------------

def test_large_module_splits_into_children():
    G, nodes = _two_cluster_graph()
    roots = build_module_tree(G, {0: nodes}, max_depth=2, max_nodes_per_module=40)
    assert len(roots) == 1
    assert len(roots[0].children) >= 2  # the two rings become submodules
    assert roots[0].level == 1
    assert all(c.level == 2 for c in roots[0].children)


def test_small_module_stays_leaf():
    G, nodes = _small_graph()
    roots = build_module_tree(G, {0: nodes}, max_nodes_per_module=40)
    assert roots[0].children == []
    assert roots[0].own_nodes == sorted(nodes)


def test_max_depth_zero_children_when_depth_1():
    G, nodes = _two_cluster_graph()
    roots = build_module_tree(G, {0: nodes}, max_depth=1, max_nodes_per_module=40)
    assert roots[0].children == []  # not allowed to descend past level 1


def test_every_node_owned_exactly_once():
    G, nodes = _two_cluster_graph()
    roots = build_module_tree(G, {0: nodes})
    from graphify.hierarchy import _node_to_module, _walk
    owner = _node_to_module(roots)
    assert set(owner) == set(nodes)  # nothing lost, nothing duplicated
    # own_nodes across all modules partition the node set
    own_all = [n for m in _walk(roots) for n in m.own_nodes]
    assert sorted(own_all) == sorted(nodes)


def test_top_level_titles_use_labels():
    G, nodes = _small_graph()
    roots = build_module_tree(G, {0: nodes}, community_labels={0: "Core Logic"})
    assert roots[0].title == "Core Logic"


def test_deterministic_ids():
    G, nodes = _two_cluster_graph()
    a = build_module_tree(G, {0: nodes})
    b = build_module_tree(G, {0: nodes})
    ids_a = [m.module_id for m in __import__("graphify.hierarchy", fromlist=["_walk"])._walk(a)]
    ids_b = [m.module_id for m in __import__("graphify.hierarchy", fromlist=["_walk"])._walk(b)]
    assert ids_a == ids_b


def test_stale_nodes_dropped():
    G, nodes = _small_graph()
    roots = build_module_tree(G, {0: nodes + ["ghost"]})
    from graphify.hierarchy import _node_to_module
    assert "ghost" not in _node_to_module(roots)


# --- rendering -----------------------------------------------------------

def test_writes_overview_and_tree(tmp_path):
    G, nodes = _two_cluster_graph()
    n = to_hierarchical_wiki(G, {0: nodes}, tmp_path)
    assert (tmp_path / "overview.md").exists()
    assert (tmp_path / "module_tree.json").exists()
    assert (tmp_path / "metadata.json").exists()
    assert n >= 3  # root + >=2 children


def test_article_count_matches_modules(tmp_path):
    G, nodes = _two_cluster_graph()
    n = to_hierarchical_wiki(G, {0: nodes}, tmp_path)
    md = list(tmp_path.glob("*.md"))
    # every module article + overview.md
    assert len(md) == n + 1


def test_metadata_has_commit_and_counts(tmp_path):
    G, nodes = _two_cluster_graph()
    G.graph["built_at_commit"] = "abc123"
    n = to_hierarchical_wiki(G, {0: nodes}, tmp_path)
    meta = json.loads((tmp_path / "metadata.json").read_text())
    assert meta["built_at_commit"] == "abc123"
    assert meta["module_count"] == n
    assert meta["total_nodes"] == G.number_of_nodes()


def test_tree_json_shape(tmp_path):
    G, nodes = _two_cluster_graph()
    to_hierarchical_wiki(G, {0: nodes}, tmp_path)
    tree = json.loads((tmp_path / "module_tree.json").read_text())
    assert "modules" in tree and len(tree["modules"]) == 1
    root = tree["modules"][0]
    assert root["node_count"] == len(nodes)
    assert len(root["children"]) >= 2
    assert root["children"][0]["level"] == 2


def test_overview_links_modules(tmp_path):
    G, nodes = _two_cluster_graph()
    to_hierarchical_wiki(G, {0: nodes}, tmp_path, community_labels={0: "Engine"})
    overview = (tmp_path / "overview.md").read_text()
    assert "Engine" in overview
    assert "```mermaid" in overview


def test_submodule_breadcrumb_and_links(tmp_path):
    G, nodes = _two_cluster_graph()
    to_hierarchical_wiki(G, {0: nodes}, tmp_path, community_labels={0: "Engine"})
    root_md = (tmp_path / "m0-Engine.md").read_text()
    assert "## Submodules" in root_md
    assert "[Overview](overview.md)" in root_md


def test_summarizer_hook_used(tmp_path):
    G, nodes = _small_graph()
    calls = []

    def fake(title, labels, sources):
        calls.append(title)
        return f"Summary of {title}."

    to_hierarchical_wiki(G, {0: nodes}, tmp_path, community_labels={0: "Core"}, summarizer=fake)
    assert calls == ["Core"]
    assert "Summary of Core." in (tmp_path / "m0-Core.md").read_text()


def test_summarizer_failure_degrades(tmp_path):
    G, nodes = _small_graph()

    def boom(*_a):
        raise RuntimeError("no api key")

    # must not raise — summary degrades to empty
    to_hierarchical_wiki(G, {0: nodes}, tmp_path, community_labels={0: "Core"}, summarizer=boom)
    assert (tmp_path / "overview.md").exists()


def test_empty_communities_raises(tmp_path):
    G, _ = _small_graph()
    with pytest.raises(ValueError, match="empty"):
        to_hierarchical_wiki(G, {}, tmp_path)


def test_all_stale_raises(tmp_path):
    G, _ = _small_graph()
    with pytest.raises(ValueError, match="stale"):
        to_hierarchical_wiki(G, {0: ["ghost1", "ghost2"]}, tmp_path)


def test_rerun_clears_orphans(tmp_path):
    G, nodes = _two_cluster_graph()
    to_hierarchical_wiki(G, {0: nodes}, tmp_path)
    (tmp_path / "orphan.md").write_text("stale")
    to_hierarchical_wiki(G, {0: nodes}, tmp_path)
    assert not (tmp_path / "orphan.md").exists()
