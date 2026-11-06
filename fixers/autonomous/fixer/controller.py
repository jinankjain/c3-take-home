"""Repair orchestration and source-version-bound acceptance gates."""

from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
import threading
from typing import Any

from .model import BudgetExhausted, ModelClient, ModelError
from .patches import PatchError, SourceManager, source_hash
from .prompts import REVIEW
from .retrieval import Corpus, FileTools
from .runtime import CaseConfig, ProcessRunner, TimeLimitError, emit
from .validation import ReplayResult, Simulator, _scenario_hashes


VALID_REPLAY_STATUSES = {"pass", "invariant_failure", "unresolved"}


def brief(result: ReplayResult, *, timeline: bool = False) -> dict:
    evidence = result.evidence or {}
    output = {
        "run_id": result.run_id, "source_hash": result.source_hash,
        "scenario": result.scenario, "seed": result.seed, "faults": result.faults,
        "status": result.status, "ok": result.ok, "error": result.error,
        "violation_count": len(result.violations), "violations": result.violations[:8],
        "messages_sent": result.summary.get("messages_sent"),
        "outcomes": result.summary.get("outcomes"),
        "failure_families": evidence.get("failure_families", [])[:6],
        "unresolved_actions": evidence.get("unresolved_actions", [])[:4],
    }
    if timeline:
        output.update(affected_entities=evidence.get("affected_entities", []),
                      timeline=evidence.get("timeline", [])[:16],
                      abnormal_events=evidence.get("abnormal_events", [])[:6],
                      fault_events=evidence.get("fault_events", [])[:6])
    return output


def _invariants(results: list[ReplayResult]) -> set[str]:
    return {item["invariant"] for result in results for item in result.violations}


def _review_json(content: list[dict]) -> dict:
    text = "\n".join(block.get("text", "") for block in content if block.get("type") == "text").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    try:
        verdict = json.loads(text)
    except ValueError as error:
        raise ModelError("review returned no complete JSON verdict", code="review_protocol") from error
    if not isinstance(verdict, dict) or not isinstance(verdict.get("approve"), bool):
        raise ModelError("review returned an invalid approval", code="review_protocol")
    for key in ("issues", "missing_evidence"):
        if not isinstance(verdict.get(key), list) or any(not isinstance(v, str) for v in verdict[key]):
            raise ModelError("review returned invalid evidence requirements", code="review_protocol")
    return verdict


class Controller:
    def __init__(self, config: CaseConfig, *, model: Any = None):
        self.config = config
        self.deadline = config.deadline
        self.final_reserve = min(120.0, max(3.0, self.deadline.remaining() * 0.15))
        self.runner = ProcessRunner(self.deadline)
        self.sources = SourceManager(config.workspace)
        self.probe_root = self.sources.scratch / "probes"
        self.probe_root.mkdir()
        self.simulator = Simulator(self.runner, self.sources.scratch / "runs")
        self.corpus = Corpus(config.corpus, config.metadata.get("codebase"))
        self.model = model
        self.runs: dict[str, ReplayResult] = {}
        self.hypotheses: dict[str, dict] = {}
        self.accepted: list[dict] = []
        self._cache: dict[tuple, ReplayResult] = {}
        self._lock = threading.RLock()
        self._validating = False
        self.baseline: list[ReplayResult] = []
        self.baseline_tests = None
        self.test_seconds = 2.0
        self.replay_seconds = 1.0
        self.contract_context = ""
        self.initial_context = ""

    def case_root(self, source: str) -> Path:
        roots = {"candidate": self.sources.candidate, "original": self.sources.original,
                 "accepted": self.sources.best}
        if source not in roots:
            raise ValueError("source must be candidate, original, or accepted")
        return roots[source]

    def _paths(self, root: Path, scenario: str, faults: str | None) -> tuple[str, str | None]:
        if not isinstance(scenario, str) or not scenario or len(scenario) > 256:
            raise ValueError("invalid scenario")
        if scenario.startswith("probe:"):
            name = scenario.split(":", 1)[1]
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name):
                raise ValueError("invalid probe name")
            path = (self.probe_root / f"{name}.yaml").resolve(strict=True)
            if not path.is_relative_to(self.probe_root.resolve()) or path.is_symlink():
                raise ValueError("probe path escapes scratch")
            scenario_arg = str(path)
        else:
            supplied = Path(scenario)
            if not supplied.parts or supplied.is_absolute() or ".." in supplied.parts:
                raise ValueError("use a shipped scenario name or probe:name")
            base = root / supplied if supplied.parts[0] == "scenarios" else root / "scenarios" / supplied
            options = [base] if base.suffix else [base.with_suffix(".yaml"), base.with_suffix(".yml"), base.with_suffix(".json"), base]
            path = next((value for value in options if value.is_file()), None)
            if path is None or not path.resolve().is_relative_to((root / "scenarios").resolve()):
                raise ValueError("scenario is not a shipped scenario")
            scenario_arg = str(path.resolve())
        faults_arg = None
        if faults is not None:
            if not isinstance(faults, str) or len(faults) > 256:
                raise ValueError("invalid fault profile")
            supplied = Path(faults)
            if supplied.is_absolute() or ".." in supplied.parts:
                raise ValueError("faults must be relative to the case scenarios directory")
            path = (root / supplied).resolve(strict=True)
            if not path.is_file() or not path.is_relative_to((root / "scenarios").resolve()):
                raise ValueError("fault profile escapes scenarios")
            faults_arg = str(path)
        return scenario_arg, faults_arg

    def replay(self, source: str, scenario: str, seed: int, faults: str | None = None,
               *, reserve: float | None = None) -> ReplayResult:
        root = self.case_root(source)
        scenario_arg, faults_arg = self._paths(root, scenario, faults)
        identity = _scenario_hashes(root, scenario_arg, faults_arg)
        key = (source_hash(root / "src"), identity, seed)
        with self._lock:
            cached = self._cache.get(key)
        if cached is not None:
            return cached
        result = self.simulator.replay(root, scenario_arg, seed=seed, faults=faults_arg,
                                       timeout=60, reserve=self.final_reserve if reserve is None else reserve)
        result = replace(result, scenario=scenario, faults=faults)
        with self._lock:
            self.runs[result.run_id] = result
            if identity[0] is not None and result.status in VALID_REPLAY_STATUSES:
                self._cache[key] = result
            self.replay_seconds = max(self.replay_seconds,
                                      result.replay_command.duration + (result.check_command.duration if result.check_command else 0))
        emit("replay", source=source, scenario=scenario, seed=seed,
             source_hash=result.source_hash, run_id=result.run_id, status=result.status,
             violations=len(result.violations))
        return result

    def source_inventory(self) -> list[dict]:
        files = FileTools(self.sources.candidate / "src")
        inventory = []
        for name in files.list_files("*.py", limit=256):
            result = files.read(name, max_chars=48000)
            entry: dict = {"path": "src/" + name, "lines": result["end"], "truncated": result["truncated"]}
            try:
                tree = ast.parse(result["text"])
                entry["symbols"] = [{"name": node.name, "line": node.lineno, "end": node.end_lineno}
                                    for node in ast.walk(tree)
                                    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))][:50]
            except SyntaxError:
                entry["symbols"] = []
            inventory.append(entry)
        return inventory

    def bootstrap(self) -> str:
        files = FileTools(self.sources.original)
        documents = {}
        for name in ("SYMPTOM.md", "README.md", "INVARIANTS.md", "scenario_schema.md"):
            try:
                documents[name] = files.read(name, max_chars=20000)["text"]
            except (OSError, ValueError):
                documents[name] = "unavailable; inspect other case files"
        with ThreadPoolExecutor(max_workers=2) as pool:
            tests = pool.submit(self.simulator.tests, self.sources.original,
                                timeout=60, reserve=self.final_reserve)
            futures = [pool.submit(self.replay, "original", "public", seed) for seed in self.config.seeds]
            self.contract_context = self.corpus.bootstrap(documents["SYMPTOM.md"], max_chars=40000)
            self.baseline_tests = tests.result()
            self.baseline = [future.result() for future in futures]
        self.test_seconds = max(1.0, self.baseline_tests.duration)
        self.final_reserve = max(self.final_reserve, min(180.0, self.test_seconds * 3 + self.replay_seconds * (len(self.config.seeds) + 3) + 10))
        emit("baseline", tests=self.baseline_tests.as_dict(), failures=sum(not r.ok for r in self.baseline),
             source_hash=self.sources.best_hash, final_reserve=self.final_reserve)
        if not self.baseline_tests.ok:
            raise RuntimeError("public tests failed on the original staged case; baseline is unusable")
        self.initial_context = "\n\n".join([
            "# Case documents\n" + json.dumps(documents, ensure_ascii=False),
            "# Source inventory\n" + json.dumps(self.source_inventory(), ensure_ascii=False),
            "# Applicable corpus excerpts\n" + self.contract_context,
            "# Baseline simulator results\n" + json.dumps([brief(r, timeline=i == 0) for i, r in enumerate(self.baseline)], ensure_ascii=False),
            "# Current status\n" + json.dumps(self.status()),
            "Read the relevant source and contracts, record a hypothesis, stage a focused repair, then call validate. "
            "Inspect remaining ticket items after acceptance. All tool paths are relative; probes use probe:name.",
        ])
        return self.initial_context

    def status(self) -> dict:
        current = self.sources.candidate_hash
        best = self.sources.best_hash
        with self._lock:
            failures = [brief(r) for r in self.runs.values() if r.source_hash == best and not r.ok]
        return {
            "source_hash": current, "accepted_source_hash": best,
            "unaccepted_changes": current != best,
            "seconds_remaining": round(self.deadline.remaining(), 1),
            "final_reserve": round(self.final_reserve, 1),
            "remaining_budget_usd": self.model.remaining_budget if self.model is not None else self.config.budget_usd,
            "public_seeds": self.config.seeds, "accepted_repairs": len(self.accepted),
            "hypotheses": list(self.hypotheses.values()), "known_failures": failures[-12:],
        }

    def record_hypothesis(self, args: dict) -> dict:
        name = args.get("id")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            raise ValueError("hypothesis id must be a short identifier")
        if name not in self.hypotheses and len(self.hypotheses) >= 32:
            raise ValueError("hypothesis limit reached; update an existing hypothesis")
        self.hypotheses[name] = {**args, "source_hash": self.sources.candidate_hash}
        emit("hypothesis", **self.hypotheses[name])
        return self.hypotheses[name]

    def _extra_seeds(self) -> list[int]:
        original = source_hash(self.sources.original / "src")
        values = [int.from_bytes(hashlib.sha256(f"{original}:validation:{n}".encode()).digest()[:4], "big") & 0x7fffffff
                  for n in range(3)]
        return [value for value in values if value not in self.config.seeds]

    def _matrix(self, source: str, experiments: list[dict], *, reserve: float | None = None) -> list[ReplayResult]:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.replay, source, item["scenario"], item["seed"], item.get("faults"), reserve=reserve)
                       for item in experiments]
            return [future.result() for future in futures]

    def _review(self, plan: dict, before: list[ReplayResult], after: list[ReplayResult], full_pass: bool) -> dict:
        if self.model is None:
            return {"approve": False, "issues": [], "missing_evidence": ["model review is unavailable"]}
        changed = self.sources.diff(max_chars=30000)
        context = "\n\n".join([
            "# Proposed repair\n" + json.dumps(plan), "# Hypotheses\n" + json.dumps(list(self.hypotheses.values())),
            "# Previously accepted obligations\n" + json.dumps([
                {key: accepted[key] for key in ("source_hash", "reason", "verified_obligations", "reproductions")}
                for accepted in self.accepted]),
            "# Current contracts\n" + self.contract_context,
            "# Source diff\n" + changed,
            "# Before/after evidence\n" + json.dumps({"before": [brief(r) for r in before], "after": [brief(r) for r in after]}),
        ])
        try:
            reply = self.model.messages(REVIEW, [{"role": "user", "content": context}], [],
                                        max_tokens=2048, reserve=max(10.0, self.test_seconds * 2 + self.replay_seconds * 3 + 5))
            verdict = _review_json(reply.content)
            emit("repair_review", **verdict, cost_usd=reply.cost_usd)
            return verdict
        except (ModelError, TimeLimitError) as error:
            emit("review_unavailable", error=str(error))
            # A complete passing local matrix can still be accepted. Partial
            # repairs need a review to explain remaining/newly exposed failures.
            return {"approve": full_pass, "issues": [], "missing_evidence": [], "skipped": str(error)}

    def validate(self, plan: dict) -> dict:
        if self._validating:
            raise ValueError("validation is already running")
        if self.sources.candidate_hash == self.sources.best_hash:
            return {"accepted": False, "reason": "candidate contains no source change"}
        self._validating = True
        try:
            return self._validate(plan)
        finally:
            self._validating = False

    def _validate(self, plan: dict) -> dict:
        frozen = self.sources.candidate_hash
        install_allowance = self.test_seconds * 2 + self.replay_seconds * 3 + 10
        if self.deadline.remaining() < install_allowance + 5:
            raise TimeLimitError("insufficient time for installed-source verification and rollback")
        targets = plan.get("target_invariants", [])
        reproductions = plan.get("reproductions", [])
        if not isinstance(targets, list) or any(not isinstance(v, str) for v in targets):
            raise ValueError("target_invariants must contain invariant names")
        if not isinstance(reproductions, list) or len(reproductions) > 12:
            raise ValueError("at most 12 reproductions are supported")
        tests = self.simulator.tests(self.sources.candidate, timeout=max(30.0, self.test_seconds * 3),
                                     reserve=install_allowance)
        if not tests.ok or self.sources.candidate_hash != frozen:
            return {"accepted": False, "reason": "public tests failed or source changed", "tests": tests.as_dict()}
        experiments = [{"scenario": "public", "seed": seed} for seed in self.config.seeds]
        experiments += [{"scenario": "public", "seed": seed} for seed in self._extra_seeds()]
        for item in reproductions:
            if not isinstance(item, dict) or not isinstance(item.get("scenario"), str) or not isinstance(item.get("seed"), int) or isinstance(item["seed"], bool):
                raise ValueError("each reproduction requires a scenario and integer seed")
            experiments.append(item)
        # Carry successful probes from earlier source versions into every later gate.
        for accepted in self.accepted:
            for item in accepted["reproductions"]:
                if item not in experiments:
                    experiments.append(item)
        base = self.sources.candidate / "scenarios/base.yaml"
        if base.is_file():
            experiments.append({"scenario": "base", "seed": self.config.seeds[0]})
        before = self._matrix("accepted", experiments, reserve=install_allowance)
        after = self._matrix("candidate", experiments, reserve=install_allowance)
        if any(r.status not in VALID_REPLAY_STATUSES for r in after):
            return {"accepted": False, "reason": "runtime or validation error", "results": [brief(r) for r in after]}
        regressions = [brief(new) for old, new in zip(before, after) if old.ok and not new.ok]
        if regressions:
            return {"accepted": False, "reason": "previously passing scenario regressed", "regressions": regressions}
        lost_obligations = []
        for accepted in self.accepted:
            for obligation in accepted["verified_obligations"]:
                for result in after:
                    if result.scenario_hash == obligation["scenario_hash"] and result.seed == obligation["seed"]:
                        reappeared = set(obligation["invariants"]) & _invariants([result])
                        if reappeared:
                            lost_obligations.append({"run_id": result.run_id, "seed": result.seed,
                                                     "scenario": result.scenario, "invariants": sorted(reappeared)})
        if lost_obligations:
            return {"accepted": False, "reason": "a previously verified obligation regressed on its retained experiment",
                    "lost_obligations": lost_obligations}
        closed = _invariants(before) - _invariants(after)
        if targets:
            closed &= set(targets)
        witnesses = [brief(new) for old, new in zip(before, after)
                     if old.status in VALID_REPLAY_STATUSES | {"runtime_error"} and not old.ok and new.ok]
        full_pass = all(r.ok for r in after)
        if not closed and not witnesses:
            return {"accepted": False, "reason": "no reproduced obligation was resolved; construct a probe that fails on accepted source and passes on candidate",
                    "before": [brief(r) for r in before], "after": [brief(r) for r in after]}
        determinism_plan = [{"scenario": "public", "seed": self.config.seeds[0]}]
        for item in reproductions:
            if item not in determinism_plan:
                determinism_plan.append(item)
        for item in determinism_plan:
            scenario_arg, faults_arg = self._paths(self.sources.candidate, item["scenario"], item.get("faults"))
            deterministic = self.simulator.determinism(self.sources.candidate, scenario_arg,
                                                       seed=item["seed"], faults=faults_arg, reserve=install_allowance)
            for run in deterministic.runs:
                self.runs[run.run_id] = run
            if not deterministic.ok or self.sources.candidate_hash != frozen:
                return {"accepted": False, "reason": "candidate is nondeterministic or changed", "experiment": item,
                        "determinism": deterministic.as_dict()}
        verdict = self._review(plan, before, after, full_pass)
        if not verdict["approve"] or verdict["issues"] or verdict["missing_evidence"]:
            return {"accepted": False, "reason": "repair review requires changes or evidence", "review": verdict,
                    "results": [brief(r) for r in after]}
        if self.deadline.remaining() < install_allowance:
            raise TimeLimitError("insufficient time to promote the reviewed candidate")
        expected = next(r for r in after if r.seed == self.config.seeds[0])
        def verify_live(root: Path) -> bool:
            test_result = self.simulator.tests(root, timeout=max(15.0, self.test_seconds * 3), reserve=5)
            scenario, _ = self._paths(root, "public", None)
            actual = self.simulator.replay(root, scenario, seed=self.config.seeds[0],
                                           timeout=max(15.0, self.replay_seconds * 3), reserve=5)
            emit("installed_verification", tests=test_result.as_dict(), replay=brief(actual))
            return (test_result.ok and actual.source_hash == frozen and actual.status == expected.status
                    and Counter(v["invariant"] for v in actual.violations) == Counter(v["invariant"] for v in expected.violations)
                    and actual.summary.get("outcomes") == expected.summary.get("outcomes")
                    and actual.summary.get("all_resolved") == expected.summary.get("all_resolved"))
        promotion = self.sources.promote(frozen, verify_live)
        accepted = {"source_hash": frozen, "reason": plan.get("reason", "repair"),
                    "reproductions": reproductions, "review": verdict,
                    "verified_obligations": [
                        {"scenario_hash": new.scenario_hash, "scenario": item["scenario"], "seed": new.seed,
                         "invariants": sorted(_invariants([old]) - _invariants([new]))}
                        for item, old, new in zip(experiments, before, after)
                        if _invariants([old]) - _invariants([new])],
                    "closed_invariants": sorted(closed), "full_local_pass": full_pass,
                    "results": [brief(r) for r in after], "promotion": promotion}
        self.accepted.append(accepted)
        emit("repair_accepted", source_hash=frozen, closed_invariants=sorted(closed),
             full_local_pass=full_pass, accepted_repairs=len(self.accepted))
        return {"accepted": True, **accepted, "status": self.status()}

    def finalize(self) -> dict:
        if not self.accepted:
            return {"accepted_repairs": 0, "source_hash": self.sources.best_hash}
        tests = self.simulator.tests(self.sources.live, timeout=max(15.0, self.test_seconds * 3), reserve=2)
        if tests.cancelled or tests.timed_out:
            emit("final_verification_interrupted", tests=tests.as_dict(), source_hash=self.sources.best_hash)
            return {"verified": None, "verification": "interrupted; retaining source verified at promotion",
                    "accepted_repairs": len(self.accepted), "source_hash": self.sources.best_hash}
        verified = tests.ok and source_hash(self.sources.live / "src") == self.sources.best_hash
        while not verified and self.accepted:
            restored = self.sources.rollback_last()
            self.accepted.pop()
            emit("final_rollback", source_hash=restored, accepted_repairs=len(self.accepted))
            tests = self.simulator.tests(self.sources.live, timeout=max(15.0, self.test_seconds * 3), reserve=2)
            if tests.cancelled or tests.timed_out:
                return {"verified": None, "verification": "interrupted after rollback",
                        "accepted_repairs": len(self.accepted), "source_hash": self.sources.best_hash}
            verified = tests.ok and source_hash(self.sources.live / "src") == self.sources.best_hash
        emit("final_verification", tests=tests.as_dict(), verified=verified,
             accepted_repairs=len(self.accepted), source_hash=self.sources.best_hash)
        return {"verified": verified, "accepted_repairs": len(self.accepted), "source_hash": self.sources.best_hash}
