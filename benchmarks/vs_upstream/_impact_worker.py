"""Run one tool's own impact traversal over its own graph, for every case.

Executed by ``run.py`` in a subprocess whose PYTHONPATH points at the tool under
test (upstream checkout or this fork), so ``graphify`` below is *that* tool's code:
its ``load_graph`` and its ``affected_nodes`` with its own default relations.
Nothing here is shared with the other tool except the seed rule.

Prints JSON: {case_id: {"seeds": [...], "by_depth": {depth: [service, ...]}}}.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from graphify.affected import DEFAULT_AFFECTED_RELATIONS, affected_nodes, load_graph


def _norm_label(label: str) -> str:
    # '.getAccountByName()' / 'ShipOrder()' / 'GetCart' -> bare name
    return label.strip().lstrip(".").split("(", 1)[0].strip()


def _service_of(source_file: str) -> str | None:
    parts = Path(source_file.replace("\\", "/")).parts
    return parts[0] if len(parts) > 1 else None


def main() -> None:
    graph_path, cases_path, depths_arg = sys.argv[1], sys.argv[2], sys.argv[3]
    depths = [int(d) for d in depths_arg.split(",")]
    G = load_graph(Path(graph_path))
    cases = json.loads(Path(cases_path).read_text(encoding="utf-8"))

    by_file: dict[str, list[str]] = {}
    for nid, data in G.nodes(data=True):
        sf = (data.get("source_file") or "").replace("\\", "/")
        if sf:
            by_file.setdefault(sf, []).append(nid)

    out: dict[str, dict] = {}
    for case in cases:
        seeds = [
            nid for nid in by_file.get(case["file"], [])
            if _norm_label(str(G.nodes[nid].get("label", ""))) == case["symbol"]
        ]
        result = {"seeds": seeds, "by_depth": {}}
        for depth in depths:
            services: set[str] = set()
            for seed in seeds:
                for hit in affected_nodes(G, seed, relations=DEFAULT_AFFECTED_RELATIONS, depth=depth):
                    svc = _service_of(str(G.nodes[hit.node_id].get("source_file") or ""))
                    if svc:
                        services.add(svc)
            result["by_depth"][str(depth)] = sorted(services)
        out[case["id"]] = result
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()
