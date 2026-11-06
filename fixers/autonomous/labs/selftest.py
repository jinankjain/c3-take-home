#!/usr/bin/env python3
"""Offline lab proof: real C3 grading, causal stages, and Controller gates.

No model client or proxy is created. The Controller interface proof uses a
zero-cost native review stub; it establishes plumbing, not autonomous solving.
"""
from __future__ import annotations

import sys
sys.dont_write_bytecode = True

import argparse
import ast
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import time

import yaml

LABS = Path(__file__).resolve().parent
REPO = LABS.parents[2]
sys.path.insert(0, str(REPO / "grader"))
sys.path.insert(0, str(LABS.parent))
from common import copy_tree, load_json, tree_files, write_json
from evaluate import Case, build_eval_tree, compute_baseline, evaluate
import simcli


def source_digest(path):
    digest = hashlib.sha256()
    for name, contents in sorted(tree_files(path).items()):
        digest.update(name.encode() + b"\0" + contents)
    return digest.hexdigest()


def checked_replay(tree, scenario, seed, out, faults=None):
    replay = simcli.replay(tree, scenario, seed, out, faults)
    if not replay.ok:
        raise AssertionError(f"replay {scenario}/{seed}: {replay.error}")
    summary = simcli.load_summary(out)
    assert summary.get("error") is None
    verdict = simcli.check(tree, out)
    assert verdict.error is None, verdict.error
    trace = out / "trace.jsonl"
    assert summary["trace_sha256"] == hashlib.sha256(trace.read_bytes()).hexdigest()
    return summary, verdict, trace


def trace_checker(case):
    spec = importlib.util.spec_from_file_location(case.id + "_trusted_checks", case.dir / "sim/checks.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.check_trace


def prepare_private_grading(case, work, workers):
    """Supply private baselines/refcounts so evaluate writes no grader cache."""
    baseline = compute_baseline(case, work / "baseline")
    assert not baseline["tests_not_passing"] and not baseline["regression_not_passing"]
    assert not baseline["tests_unmasked"] and not baseline["regression_unmasked"]
    assert baseline["tests"] and baseline["regression"]
    write_json(case.private / "regression-baseline.json", baseline)
    tree = work / "reference-count-tree"
    build_eval_tree(case, case.private / "full/src", tree)
    counts = {}
    for bug in case.bugs:
        bid, scenario = bug["id"], case.scenario_name(bug)
        seeds = case.bug_seeds(bid)
        assert len(seeds) == len(set(seeds)) == 20

        def one(seed):
            summary, verdict, trace = checked_replay(tree, scenario, seed, work / "reference-counts" / bid / str(seed))
            assert not verdict.violations and summary["all_resolved"]
            return str(seed), simcli.count_messages(trace)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            counts[bid] = dict(pool.map(one, seeds))
    write_json(case.private / "refcounts.json", counts)
    return {"public_tests": len(baseline["tests"]), "private_regressions": len(baseline["regression"]),
            "reference_messages": {bid: {"min": min(rows.values()), "max": max(rows.values())}
                                   for bid, rows in counts.items()}}


def verify_stages(case, work, workers):
    assert len(case.bugs) == 2 and case.bugs[1].get("gates") == [case.bugs[0]["id"]]
    first, second = (bug["invariant"] for bug in case.bugs)
    checker = trace_checker(case)
    report = {}
    for index, stage in enumerate(("unfixed", "stage1", "full")):
        src = case.dir / "src" if stage == "unfixed" else case.private / stage / "src"
        stage_work = work / stage
        result = evaluate(case, src, stage_work, workers=workers, runs_per_seed=2)
        assert result["tests_ok"] and result["regression_ok"]
        assert not result["tests_failed"] and not result["regression_failed"]
        assert result["bugs_fixed"] == index, (case.id, stage, result["bugs"])
        expected = [0, 0] if index == 0 else [20, 0] if index == 1 else [20, 20]
        assert [bug["seeds_passed"] for bug in result["bugs"]] == expected
        # Every negative result must be a causal protocol failure, not a crash,
        # nondeterministic trace or a hidden second failure on the initial stage.
        for ordinal, bug in enumerate(case.bugs):
            expected_family = first if index == 0 else second if index == 1 and ordinal == 1 else None
            for seed in case.bug_seeds(bug["id"]):
                run = stage_work / "runs" / bug["id"] / str(seed)
                first_trace, second_trace = run / "r1/trace.jsonl", run / "r2/trace.jsonl"
                assert first_trace.read_bytes() == second_trace.read_bytes()
                assert simcli.load_summary(run / "r1").get("error") is None
                verdict = checker(first_trace)
                families = {item["invariant"] for item in verdict["invariants"]} - {"expect"}
                assert families == ({expected_family} if expected_family else set()), (case.id, stage, seed, families)
        public = []
        for seed in case.meta["public_seeds"]:
            summary, verdict, trace = checked_replay(stage_work / "tree", "public", seed,
                                                     work / "public" / stage / str(seed))
            again = work / "public" / stage / str(seed) / "again"
            _, _, second_trace = checked_replay(stage_work / "tree", "public", seed, again)
            assert trace.read_bytes() == second_trace.read_bytes()
            families = {row["invariant"] for row in verdict.violations} - {"expect"}
            assert families == ({first} if index == 0 else {second} if index == 1 else set())
            public.append({"seed": seed, "families": sorted(families), "resolved": summary["all_resolved"]})
        for scenario in ("base", "quiet", "reuse"):
            summary, verdict, _ = checked_replay(stage_work / "tree", scenario, 211,
                                                 work / "healthy" / stage / scenario)
            assert summary["all_resolved"] and not verdict.violations
        report[stage] = {"bugs_fixed": result["bugs_fixed"], "seeds_passed": expected,
                         "tests_ok": result["tests_ok"], "regression_ok": result["regression_ok"],
                         "determinism": True, "public": public}
        print(json.dumps({"case": case.id, "stage": stage, "bugs_fixed": result["bugs_fixed"],
                          "seeds_passed": expected}), flush=True)
    return report


def logging_counterexample(case, work):
    """Diagnostic-only source edits must not change faults or invariant families."""
    class WithoutDiagnosticLog(ast.NodeTransformer):
        def visit_Expr(self, node):
            call = node.value
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name) and call.func.value.id == "self"
                    and call.func.attr == "log"):
                return None
            return self.generic_visit(node)

    families = []
    for stage, bug in (("unfixed", case.bugs[0]), ("stage1", case.bugs[1])):
        src = case.dir / "src" if stage == "unfixed" else case.private / stage / "src"
        tree = work / "without-source-logs" / stage
        build_eval_tree(case, src, tree)
        for path in (tree / "src").glob("*.py"):
            transformed = WithoutDiagnosticLog().visit(ast.parse(path.read_text()))
            path.write_text(ast.unparse(ast.fix_missing_locations(transformed)) + "\n")
        _, verdict, _ = checked_replay(tree, "public", case.meta["public_seeds"][0],
                                       work / "without-source-logs-runs" / stage)
        observed = {item["invariant"] for item in verdict.violations} - {"expect"}
        assert observed == {bug["invariant"]}, (case.id, stage, observed)
        families.append(sorted(observed))
    return {"logging_only_edit_cannot_avoid_failure": True, "families": families}


def controller_proof(case, work):
    from fixer.controller import Controller
    from fixer.model import ModelReply
    from fixer.runtime import CaseConfig, Deadline
    from fixer.tools import ToolDispatcher

    class OfflineReview:
        remaining_budget = 6.0
        spent_usd = 0.0

        def messages(self, system, messages, tools, **kwargs):
            assert len(tools) == 1 and tools[0]["name"] == "submit_review"
            assert kwargs["tool_choice"] == {"type": "tool", "name": "submit_review"}
            source = tools[0]["input_schema"]["properties"]["source_hash"]["enum"][0]
            verdict = {"source_hash": source, "approve": True, "issues": [],
                       "missing_evidence": [], "limitations": []}
            return ModelReply([{"type": "tool_use", "id": "offline-review", "name": "submit_review",
                                "input": verdict}], "tool_use", {}, 0, 6)

    workspace = work / "controller-case"
    copy_tree(case.dir, workspace)
    config = CaseConfig(workspace, LABS / "corpus", Deadline(time.time() + 180), 6, case.meta)
    controller = Controller(config, model=OfflineReview())
    try:
        controller.bootstrap()
        assert controller.baseline_tests.ok
        first = case.bugs[0]
        scenario = yaml.safe_load((case.private / "scenarios" / (case.scenario_name(first) + ".yaml")).read_text())
        scenario["name"] = "offline-isolation"
        written = ToolDispatcher(controller).execute("write_probe", {"name": "offline-isolation", "scenario": scenario})
        assert written["ok"], written
        controller.record_hypothesis({"id": "restart-obligation", "status": "proposed",
                                     "obligation": first["invariant"],
                                     "mechanism": "Preserve the current journal and RPC obligation across recovery.",
                                     "evidence": [f"service/{case.codebase}/design.md", "Original isolated replay fails."]})
        for stage, bug in (("stage1", first), ("full", case.bugs[1])):
            edits = [{"path": "src/" + name, "content": contents.decode()}
                     for name, contents in tree_files(case.private / stage / "src").items()]
            controller.sources.apply_edits(controller.sources.candidate_hash, edits)
            reproduction = {"scenario": "probe:offline-isolation" if stage == "stage1" else "public",
                            "seed": case.meta["public_seeds"][0]}
            outcome = controller.validate({"target_invariants": [bug["invariant"]],
                                           "reproductions": [reproduction],
                                           "reason": "Close the source-bound protocol obligation while preserving earlier progress."})
            assert outcome["accepted"], outcome
            assert outcome["full_local_pass"] == (stage == "full")
        assert len(controller.accepted) == 2 and controller.finalize()["verified"]
        assert source_digest(controller.sources.live / "src") == source_digest(case.private / "full/src")
        return {"partial_accepted": True, "full_accepted": True, "installed_verified": True,
                "review": "offline native stub; no model calls"}
    finally:
        controller.runner.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    ids = sorted(path.name for path in (LABS / "cases").iterdir() if (path / "case.json").is_file())
    parser.add_argument("--cases", nargs="+", choices=ids, default=ids)
    parser.add_argument("--workers", type=int, choices=(1, 2), default=2)
    parser.add_argument("--out", type=Path, default=LABS / "selftest-report.json")
    args = parser.parse_args()
    os.environ["C3_GRADER_PYTHON"] = sys.executable
    before = {cid: source_digest(LABS / "cases" / cid / "src") for cid in args.cases}
    started = time.monotonic()
    report = {"model_calls": 0, "cases": {}}
    with tempfile.TemporaryDirectory(prefix="c3-workflow-labs-") as scratch:
        scratch = Path(scratch)
        for cid in args.cases:
            case = Case(LABS / "cases" / cid, LABS / "private/labs" / cid)
            work = scratch / cid
            work.mkdir()
            grading = prepare_private_grading(case, work, args.workers)
            stages = verify_stages(case, work, args.workers)
            logging = logging_counterexample(case, work)
            interface = controller_proof(case, work)
            public_out = scratch / "public-logs" / cid
            checked_replay(case.dir, "public", case.meta["public_seeds"][0], public_out)
            copy_tree(public_out / "logs", case.dir / "logs")
            report["cases"][cid] = {"grading": grading, "stages": stages, "logging_boundary": logging,
                                    "controller": interface}
    for cid, digest in before.items():
        assert source_digest(LABS / "cases" / cid / "src") == digest, "selftest changed unfixed source"
    report["seconds"] = round(time.monotonic() - started, 2)
    report["ok"] = True
    write_json(args.out, report)
    print(json.dumps({"ok": True, "model_calls": 0, "cases": args.cases,
                      "seconds": report["seconds"], "report": str(args.out)}), flush=True)


if __name__ == "__main__":
    main()
