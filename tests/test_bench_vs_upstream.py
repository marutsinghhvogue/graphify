"""Scoring and case-file integrity for benchmarks/vs_upstream (no network, no extraction)."""
import importlib.util
import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent.parent / "benchmarks" / "vs_upstream"
_spec = importlib.util.spec_from_file_location("bench_vs_upstream_run", BENCH / "run.py")
run = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = run  # dataclasses resolve their module via sys.modules
_spec.loader.exec_module(run)


def _case(expected, file="svc-a/src/Handler.java"):
    return {"id": "c", "file": file, "symbol": "h", "expected": expected}


def test_own_service_hits_are_ignored():
    tp, fp, fn = run.score_case(_case(["svc-b"]), ["svc-a", "svc-b"])
    assert (tp, fp, fn) == ({"svc-b"}, set(), set())


def test_extra_and_missing_services_are_counted():
    tp, fp, fn = run.score_case(_case(["svc-b", "svc-c"]), ["svc-b", "svc-d"])
    assert (tp, fp, fn) == ({"svc-b"}, {"svc-d"}, {"svc-c"})


def test_negative_control_only_adds_false_positives():
    s = run.Score()
    impact = {"c": {"seeds": ["n1"], "by_depth": {"2": ["svc-a", "svc-x"]}}}
    run.score([_case([])], impact, 2, s)
    assert (s.tp, s.fp, s.fn, s.neg_fp, s.positives) == (0, 0, 0, 1, 0)
    assert s.recall is None and s.precision is None


def test_missing_seed_counts_as_a_full_miss():
    s = run.Score()
    impact = {"c": {"seeds": [], "by_depth": {"2": []}}}
    run.score([_case(["svc-b"])], impact, 2, s)
    assert (s.fn, s.seeds_missing, s.recall) == (1, 1, 0.0)


def test_every_case_file_matches_a_pinned_estate():
    estates = json.loads((BENCH / "estates.json").read_text(encoding="utf-8"))
    for name, spec in estates.items():
        assert len(spec["sha"]) == 40, name
        cases = json.loads((BENCH / "cases" / f"{name}.json").read_text(encoding="utf-8"))
        ids = [c["id"] for c in cases]
        assert len(ids) == len(set(ids)), name
        for c in cases:
            assert set(c) >= {"id", "file", "symbol", "expected", "evidence"}, c["id"]
            # a labelled consumer must be cited; the seed's own service is never expected
            assert len(c["evidence"]) >= (1 if c["expected"] else 0), c["id"]
            assert run.seed_service(c) not in c["expected"], c["id"]
