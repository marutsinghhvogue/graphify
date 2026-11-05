"""Benchmark: cross-service impact recall, this fork vs upstream graphify.

Question it answers: "if I change this endpoint handler, which OTHER services are
affected?" Each case seeds one handler in a public microservice estate; the
expected answer is the set of services whose source code calls it, hand-labelled
by reading the code (see cases/*.json, every label cites the call site). Labels
are never derived from either tool's output.

Each tool runs in its own interpreter against its own code:
  upstream   upstream/v8 checkout, plain `extract --code-only`
  fork-ast   this repo, same command (ablation: our code, no framework layers)
  fork       this repo + --cross-service --bindings --schedulers --cloud-schedulers
and answers with its own `affected_nodes` (its own default relation set), so
neither side borrows the other's traversal. Scores are service-level: a hit in
the seed's own service is ignored; precision/recall are over other services.
Negative controls (expected = []) only add false positives.

    python benchmarks/vs_upstream/run.py [--estates a,b] [--tools t1,t2]
                                         [--depths 2,4,6] [--rebuild]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
CACHE = HERE / ".cache"
FRAMEWORK_FLAGS = ["--cross-service", "--bindings", "--schedulers", "--cloud-schedulers"]
TOOLS = {
    "upstream": {"src": "upstream", "flags": []},
    "fork-ast": {"src": "fork", "flags": []},
    "fork": {"src": "fork", "flags": FRAMEWORK_FLAGS},
}


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def ensure_estate(name: str, spec: dict) -> Path:
    """Shallow-fetch the pinned commit; reuse the checkout if already at it."""
    path = CACHE / "estates" / name
    if (path / ".git").exists() and _git("rev-parse", "HEAD", cwd=path) == spec["sha"]:
        return path
    path.mkdir(parents=True, exist_ok=True)
    if not (path / ".git").exists():
        _git("init", "-q", cwd=path)
        _git("remote", "add", "origin", spec["url"], cwd=path)
    _git("fetch", "-q", "--depth", "1", "origin", spec["sha"], cwd=path)
    _git("checkout", "-q", "--detach", "FETCH_HEAD", cwd=path)
    return path


def ensure_upstream(ref: str) -> tuple[Path, str]:
    """A detached worktree of ``ref`` (default upstream/v8) at CACHE/upstream-src."""
    sha = _git("rev-parse", ref, cwd=REPO)
    path = CACHE / "upstream-src"
    if not path.exists():
        _git("worktree", "add", "-q", "--detach", str(path), sha, cwd=REPO)
    elif _git("rev-parse", "HEAD", cwd=path) != sha:
        _git("checkout", "-q", "--detach", sha, cwd=path)
    return path, sha


def build_graph(tool: str, src: Path, estate: str, root: Path, rebuild: bool) -> tuple[Path, float | None]:
    out = CACHE / "out" / tool / estate
    graph = out / "graph.json"
    if graph.exists() and not rebuild:
        return graph, None
    env = {**os.environ, "GRAPHIFY_OUT": str(out), "PYTHONPATH": str(src)}
    cmd = [sys.executable, "-m", "graphify", "extract", str(root), "--code-only", "--no-cluster",
           "--force", *TOOLS[tool]["flags"]]
    t0 = time.perf_counter()
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    elapsed = time.perf_counter() - t0
    if r.returncode != 0 or not graph.exists():
        raise RuntimeError(f"{tool} extract failed on {estate}:\n{r.stderr[-2000:]}")
    (out / "extract.log").write_text(r.stdout + r.stderr, encoding="utf-8")
    return graph, elapsed


def run_impact(src: Path, graph: Path, cases_path: Path, depths: list[int]) -> dict:
    env = {**os.environ, "PYTHONPATH": str(src)}
    r = subprocess.run(
        [sys.executable, str(HERE / "_impact_worker.py"), str(graph), str(cases_path),
         ",".join(map(str, depths))],
        env=env, capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"impact worker failed:\n{r.stderr[-2000:]}")
    return json.loads(r.stdout)


# ── scoring ──────────────────────────────────────────────────────────────────


@dataclass
class Score:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    neg_fp: int = 0          # false positives on negative controls
    seeds_missing: int = 0
    exact: int = 0           # positive cases with predicted == expected
    positives: int = 0
    rows: list = field(default_factory=list)

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else None


def seed_service(case: dict) -> str:
    return Path(case["file"]).parts[0]


def score_case(case: dict, predicted: list[str]) -> tuple[set[str], set[str], set[str]]:
    """(tp, fp, fn) service sets, ignoring the seed's own service."""
    expected = set(case["expected"])
    got = set(predicted) - {seed_service(case)}
    return expected & got, got - expected, expected - got


def score(cases: list[dict], impact: dict, depth: int, into: Score) -> None:
    for case in cases:
        res = impact[case["id"]]
        tp, fp, fn = score_case(case, res["by_depth"][str(depth)])
        if not res["seeds"]:
            into.seeds_missing += 1
        if case["expected"]:
            into.positives += 1
            into.tp += len(tp)
            into.fp += len(fp)
            into.fn += len(fn)
            into.exact += int(not fp and not fn)
        else:
            into.neg_fp += len(fp)
        into.rows.append((case["id"], sorted(tp), sorted(fp), sorted(fn), len(res["seeds"])))


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.0f}%"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--estates", default=None, help="comma list (default: all in estates.json)")
    ap.add_argument("--split", default=None, help="only estates in this split (dev | heldout)")
    ap.add_argument("--tools", default=",".join(TOOLS))
    ap.add_argument("--depths", default="2,4,6")
    ap.add_argument("--upstream-ref", default="upstream/v8")
    ap.add_argument("--rebuild", action="store_true", help="re-extract even if a graph exists")
    ap.add_argument("--json", type=Path, default=CACHE / "results.json")
    args = ap.parse_args()

    estates = json.loads((HERE / "estates.json").read_text(encoding="utf-8"))
    names = args.estates.split(",") if args.estates else list(estates)
    if args.split:
        names = [n for n in names if estates[n].get("split", "dev") == args.split]
    tools = args.tools.split(",")
    depths = [int(d) for d in args.depths.split(",")]
    upstream_src, upstream_sha = ensure_upstream(args.upstream_ref)
    fork_sha = _git("rev-parse", "HEAD", cwd=REPO)
    srcs = {"upstream": upstream_src, "fork": REPO}

    # scores["<split> · <transport>"][tool][depth]; held-out estates never blend with dev
    scores: dict[str, dict[str, dict[int, Score]]] = {}
    raw: dict = {"upstream_sha": upstream_sha, "fork_sha": fork_sha, "estates": {}}
    for name in names:
        spec = estates[name]
        root = ensure_estate(name, spec) / spec["root"]
        cases_path = HERE / "cases" / f"{name}.json"
        cases = json.loads(cases_path.read_text(encoding="utf-8"))
        raw["estates"][name] = {}
        for tool in tools:
            src = srcs[TOOLS[tool]["src"]]
            graph, secs = build_graph(tool, src, name, root, args.rebuild)
            impact = run_impact(src, graph, cases_path, depths)
            raw["estates"][name][tool] = {"extract_seconds": secs, "impact": impact}
            for d in depths:
                group = f'{spec.get("split", "dev")} · {spec["transport"]}'
                s = scores.setdefault(group, {}).setdefault(tool, {}).setdefault(d, Score())
                score(cases, impact, d, s)
            print(f"  {name:<32} {tool:<9} done" + (f" (extract {secs:.1f}s)" if secs else " (cached graph)"),
                  file=sys.stderr)

    print(f"\nupstream {upstream_sha[:10]}  vs  fork {fork_sha[:10]}\n")
    for group, by_tool in scores.items():
        print(f"## {group.upper()} estates\n")
        print("| tool | depth | recall | precision | exact cases | FP on negative controls | seeds not found |")
        print("|---|---|---|---|---|---|---|")
        for tool in tools:
            for d in depths:
                s = by_tool[tool][d]
                print(f"| {tool} | {d} | {_pct(s.recall)} ({s.tp}/{s.tp + s.fn}) | {_pct(s.precision)} "
                      f"| {s.exact}/{s.positives} | {s.neg_fp} | {s.seeds_missing} |")
        print()
    deepest = max(depths)
    print(f"## Per case (depth {deepest}): TP / FP / FN other-services\n")
    print("| case | " + " | ".join(tools) + " |")
    print("|---|" + "---|" * len(tools))
    for group_scores in scores.values():
        case_ids = [r[0] for r in group_scores[tools[0]][deepest].rows]
        for i, cid in enumerate(case_ids):
            cells = []
            for tool in tools:
                _, tp, fp, fn, nseeds = group_scores[tool][deepest].rows[i]
                cell = f"✓{len(tp)} ✗{len(fp)} miss{len(fn)}"
                if not nseeds:
                    cell += " (no seed)"
                cells.append(cell)
            print(f"| {cid} | " + " | ".join(cells) + " |")

    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(raw, indent=1), encoding="utf-8")
    print(f"\nraw results: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
