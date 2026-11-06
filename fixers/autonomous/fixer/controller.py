"""Repair orchestration and source-version-bound acceptance gates."""

from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import re
import threading
import time
from typing import Any

from .model import BudgetExhausted, ModelClient, ModelError
from .experiments import select_experiments
from .patches import PatchError, SourceManager, source_hash
from .prompts import REVIEW
from .retrieval import Corpus, FileTools
from .runtime import CaseConfig, ProcessRunner, TimeLimitError, emit
from .validation import ReplayResult, Simulator, _scenario_hashes


VALID_REPLAY_STATUSES = {"pass", "invariant_failure", "unresolved"}
VALIDATION_HISTORY_LIMIT = 8
VALIDATION_RESULT_LIMIT = 12
VALIDATION_REVIEW_LIMIT = 6
VALIDATION_TEXT_LIMIT = 800
REVIEW_TOOL_NAME = "submit_review"


def _ledger_text(value: Any, limit: int = VALIDATION_TEXT_LIMIT) -> str | None:
    return None if value is None else str(value)[:limit]


def _ledger_seed(value: int) -> int | str:
    if isinstance(value, int) and value.bit_length() > 256:
        return f"[seed integer omitted: {value.bit_length()} bits]"
    return value


def _ledger_replay(result: ReplayResult, snapshot: str) -> dict:
    invariants = sorted({str(item.get("invariant", "?"))[:100]
                         for item in result.violations})[:8]
    return {
        "run_id": _ledger_text(result.run_id, 64),
        "source_hash": _ledger_text(result.source_hash, 128), "snapshot": snapshot,
        "scenario": _ledger_text(result.scenario, 256),
        "scenario_hash": _ledger_text(result.scenario_hash, 128),
        "seed": _ledger_seed(result.seed), "faults": _ledger_text(result.faults, 256),
        "status": _ledger_text(result.status, 64), "ok": result.ok,
        "violation_count": len(result.violations), "invariants": invariants,
        "violations": [{"invariant": name} for name in invariants],
        "error": _ledger_text(result.error, 400),
    }


def _ledger_tests(result: Any) -> dict:
    return {"ok": result.ok, "returncode": result.returncode,
            "timed_out": result.timed_out, "cancelled": result.cancelled,
            "duration": round(result.duration, 3)}


def _ledger_review(verdict: dict) -> dict:
    issues = verdict.get("issues", [])
    missing = verdict.get("missing_evidence", [])
    limitations = verdict.get("limitations", [])
    result = {"approve": verdict.get("approve"),
              "issues": [_ledger_text(item) for item in issues[:VALIDATION_REVIEW_LIMIT]],
              "missing_evidence": [_ledger_text(item) for item in missing[:VALIDATION_REVIEW_LIMIT]],
              "limitations": [_ledger_text(item) for item in limitations[:VALIDATION_REVIEW_LIMIT]],
              "issues_count": len(issues), "missing_evidence_count": len(missing),
              "limitations_count": len(limitations),
              "truncated": (any(len(items) > VALIDATION_REVIEW_LIMIT for items in (issues, missing, limitations))
                            or any(len(str(item)) > VALIDATION_TEXT_LIMIT for item in [*issues, *missing, *limitations]))}
    if "source_hash" in verdict:
        result["source_hash"] = verdict["source_hash"]
    if "skipped" in verdict:
        result["skipped"] = _ledger_text(verdict["skipped"])
    return result


def _brief_records(records: list, limit: int) -> list:
    if len(records) <= limit:
        return list(records)
    head = (limit + 1) // 2
    return records[:head] + records[-(limit - head):]


def _brief_window(window: dict) -> dict:
    records = window.get("events", [])
    events = _brief_records(records, 8)
    selected = window.get("selected_events", len(records))
    selected = selected if isinstance(selected, int) and not isinstance(selected, bool) else len(records)
    return {**window, "events": events, "returned_events": len(events),
            "omitted_selected_events": max(0, selected - len(events)),
            "selection_truncated": bool(window.get("selection_truncated")) or len(events) < len(records),
            "brief_omitted_events": len(records) - len(events)}


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
        records = evidence.get("timeline", [])
        events = _brief_records(records, 16)
        matched = evidence.get("timeline_matched_events", len(records))
        matched = matched if isinstance(matched, int) and not isinstance(matched, bool) else len(records)
        output.update(affected_entities=evidence.get("affected_entities", []),
                      timeline=events, timeline_omitted_events=max(0, matched - len(events)),
                      timeline_truncated=bool(evidence.get("timeline_truncated")) or len(events) < len(records),
                      abnormal_events=_brief_records(evidence.get("abnormal_events", []), 6),
                      fault_events=_brief_records(evidence.get("fault_events", []), 6),
                      failure_witnesses=_brief_records(evidence.get("failure_witnesses", []), 4),
                      failure_witnesses_brief_omitted=max(0, len(evidence.get("failure_witnesses", [])) - 4),
                      causal_windows=[_brief_window(window)
                                      for window in _brief_records(evidence.get("causal_windows", []), 2)],
                      causal_windows_brief_omitted=max(0, len(evidence.get("causal_windows", [])) - 2))
    return output


def _invariants(results: list[ReplayResult]) -> set[str]:
    return {item["invariant"] for result in results for item in result.violations}


def _review_tool(source_hash: str) -> dict:
    strings = {"type": "array", "items": {"type": "string"}}
    return {
        "name": REVIEW_TOOL_NAME,
        "description": "Submit the independent repair verdict for the supplied candidate source hash.",
        "input_schema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "source_hash": {"type": "string", "enum": [source_hash]},
                "approve": {"type": "boolean"}, "issues": deepcopy(strings),
                "missing_evidence": deepcopy(strings), "limitations": deepcopy(strings),
            },
            "required": ["source_hash", "approve", "issues", "missing_evidence"],
        },
    }


def _review_verdict(content: list[dict], source_hash: str) -> dict:
    """Accept one typed native decision bound to the reviewed source version."""
    if not isinstance(content, list) or any(not isinstance(block, dict) for block in content):
        raise ModelError("review returned invalid content blocks", code="review_protocol")
    if any(not isinstance(block.get("type"), str)
           or block["type"] not in {"text", "thinking", "redacted_thinking", "tool_use"}
           for block in content):
        raise ModelError("review returned an unexpected content block", code="review_protocol")
    decisions = [block for block in content if block.get("type") == "tool_use"]
    if len(decisions) != 1:
        raise ModelError("review must return exactly one submit_review decision", code="review_protocol")
    call = decisions[0]
    if (call.get("name") != REVIEW_TOOL_NAME or not isinstance(call.get("id"), str)
            or not call["id"] or not isinstance(call.get("input"), dict)):
        raise ModelError("review returned an invalid submit_review call", code="review_protocol")
    verdict = call["input"]
    required = {"source_hash", "approve", "issues", "missing_evidence"}
    if not required.issubset(verdict) or set(verdict) - (required | {"limitations"}):
        raise ModelError("review returned invalid verdict fields", code="review_protocol")
    if not isinstance(verdict["source_hash"], str) or verdict["source_hash"] != source_hash:
        raise ModelError("review decision does not match the candidate source hash", code="review_protocol")
    if not isinstance(verdict["approve"], bool):
        raise ModelError("review returned an invalid approval", code="review_protocol")
    for key in ("issues", "missing_evidence", "limitations"):
        if key in verdict and (not isinstance(verdict[key], list)
                               or any(not isinstance(item, str) for item in verdict[key])):
            raise ModelError("review returned invalid evidence lists", code="review_protocol")
    result = {"source_hash": source_hash, "approve": verdict["approve"],
              "issues": list(verdict["issues"]), "missing_evidence": list(verdict["missing_evidence"])}
    if "limitations" in verdict:
        result["limitations"] = list(verdict["limitations"])
    return result


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
        self.model = model
        self.corpus = Corpus(config.corpus, config.metadata.get("codebase"), relevancy=self._document_relevance)
        self.runs: dict[str, ReplayResult] = {}
        self.hypotheses: dict[str, dict] = {}
        self.accepted: list[dict] = []
        self._cache: dict[tuple, ReplayResult] = {}
        self._lock = threading.RLock()
        self._validating = False
        self._validation_progress: dict | None = None
        self.validation_history: dict[str, dict] = {}
        self.baseline: list[ReplayResult] = []
        self.baseline_tests = None
        self.test_seconds = 2.0
        self.replay_seconds = 1.0
        self.contract_context = ""
        self.initial_context = ""
        self.retained_experiments: dict[tuple[str, int], dict] = {}
        self._experiment_timings: dict[tuple[str | None, int], dict] = {}
        self.review_seconds = 15.0

    def _document_relevance(self, query: str, documents: list[dict]) -> dict | None:
        handler = getattr(self.model, "relevance", None)
        return handler(query, documents, reserve=self.final_reserve) if callable(handler) else None

    def _retain_experiment(self, result: ReplayResult) -> None:
        if not result.ok or result.scenario_hash is None:
            return
        self.retained_experiments[(result.scenario_hash, result.seed)] = {
            "scenario": result.scenario, "seed": result.seed, "faults": result.faults,
            "scenario_hash": result.scenario_hash, "faults_hash": result.faults_hash,
            "source_hash": result.source_hash, "run_id": result.run_id,
        }

    def _experiment_input(self, item: dict, source: str) -> tuple:
        root = self.case_root(source)
        scenario, faults = self._paths(root, item["scenario"], item.get("faults"))
        return _scenario_hashes(root, scenario, faults)

    def _experiment_cost(self, item: dict, source: str, digest: str) -> float:
        identity = self._experiment_input(item, source)
        if (digest, identity, item["seed"]) in self._cache:
            return 0.0
        timing = self._experiment_timings.get((identity[0], item["seed"]))
        return timing["worst_seconds"] if timing else self.replay_seconds

    def _select_validation_experiments(self, reproductions: list[dict], install_allowance: float):
        required = [{"scenario": "public", "seed": seed, "faults": None} for seed in self.config.seeds]
        for item in reproductions:
            if (not isinstance(item, dict) or not isinstance(item.get("scenario"), str)
                    or not isinstance(item.get("seed"), int) or isinstance(item["seed"], bool)):
                raise ValueError("each reproduction requires a scenario and integer seed")
            required.append({"scenario": item["scenario"], "seed": item["seed"], "faults": item.get("faults")})
        for retained in self.retained_experiments.values():
            item = {key: retained[key] for key in ("scenario", "seed", "faults")}
            if self._experiment_input(item, "candidate") != (retained["scenario_hash"], retained["faults_hash"]):
                raise ValueError("a retained experiment's effective inputs changed")
            required.append(item)
        # Existing checkpoint metadata remains sufficient if loaded by a test
        # harness or an older controller without the richer retained registry.
        historical_selectors = [item for accepted in self.accepted
                                for item in [*accepted["reproductions"], *accepted.get("results", [])]]
        for accepted in self.accepted:
            required.extend({"scenario": item["scenario"], "seed": item["seed"], "faults": item.get("faults")}
                            for item in accepted["reproductions"])
            for obligation in accepted["verified_obligations"]:
                scenario, seed, expected_hash = (obligation.get(key) for key in ("scenario", "seed", "scenario_hash"))
                if (not isinstance(scenario, str) or not isinstance(seed, int) or isinstance(seed, bool)
                        or not isinstance(expected_hash, str) or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None):
                    raise ValueError("a retained obligation lacks a valid effective input identity")
                # The recorded scenario must still exist. Older checkpoints did
                # not store fault selectors; only a matching effective hash from
                # their historical selectors can recover that missing field.
                self._paths(self.sources.candidate, scenario, None)
                choices = [{"scenario": scenario, "seed": seed, "faults": obligation.get("faults")}]
                if "faults" not in obligation:
                    choices.extend({"scenario": item["scenario"], "seed": seed, "faults": item.get("faults")}
                                   for item in historical_selectors if item.get("seed") == seed)
                matched = None
                for item in choices:
                    try:
                        identity = self._experiment_input(item, "candidate")
                    except (ValueError, OSError):
                        continue
                    if (identity[0] == expected_hash
                            and ("faults_hash" not in obligation or identity[1] == obligation["faults_hash"])):
                        matched = item
                        break
                if matched is None:
                    raise ValueError("a retained obligation's effective inputs are missing or changed")
                # Removing an invariant is a retained obligation even when
                # other invariants still fail on this scenario and seed.
                required.append(matched)
        if (self.sources.candidate / "scenarios/base.yaml").is_file():
            required.append({"scenario": "base", "seed": self.config.seeds[0], "faults": None})
        seen = set()
        unique = []
        for item in required:
            identity = (self._experiment_input(item, "candidate"), item["seed"])
            if identity not in seen:
                seen.add(identity)
                unique.append(item)
        optional = []
        for seed in self._extra_seeds():
            item = {"scenario": "public", "seed": seed, "faults": None}
            identity = (self._experiment_input(item, "candidate"), seed)
            if identity not in seen:
                seen.add(identity)
                optional.append(item)
        best, current = self.sources.best_hash, self.sources.candidate_hash

        def costs(item):
            return (self._experiment_cost(item, "accepted", best),
                    self._experiment_cost(item, "candidate", current))

        def priority(item):
            identity = self._experiment_input(item, "accepted")
            previous = self._cache.get((best, identity, item["seed"]))
            failure = previous is not None and not previous.ok
            families = len(_invariants([previous])) if previous is not None else 0
            return (0 if failure else 1 if item["scenario"].startswith("probe:") else 2,
                    -families, sum(costs(item)), item["scenario"], item["seed"])

        # Extra seeds consume only the space left after required regressions,
        # deterministic repeats, review and verification of the live install.
        repeats = (1 + len(reproductions)) * self.replay_seconds * 2
        available = self.deadline.remaining() - install_allowance - self.review_seconds - repeats
        selected = select_experiments(unique, optional, costs=costs, priority=priority,
                                      available_seconds=available)
        emit("validation_plan", source_hash=current, **selected.summary())
        return selected

    def _experiment_recommendations(self, current: str, best: str) -> list[dict]:
        choices = []
        seen = set()
        for run in reversed(list(self.runs.values())):
            if run.source_hash != current or run.ok or run.status not in VALID_REPLAY_STATUSES:
                continue
            key = (run.scenario_hash, run.seed)
            if key in seen:
                continue
            seen.add(key)
            evidence = run.evidence or {}
            entities = [name for name in evidence.get("affected_entities", [])
                        if isinstance(name, str) and 0 < len(name) <= 512][:4]
            arguments = {"run_id": run.run_id, "entities": entities, "limit": 40}
            windows = evidence.get("causal_windows", [])
            anchor = windows[0].get("at") if windows and isinstance(windows[0], dict) else None
            if isinstance(anchor, (int, float)) and not isinstance(anchor, bool) and math.isfinite(anchor):
                arguments.update(at=anchor, window_s=2.0, context_lines=2)
            choices.append({"tool": "trace_slice", "arguments": arguments, "source_hash": current,
                            "reason": "inspect the current failure's affected entities and causal window",
                            "estimated_seconds": 0.0})
            if len(choices) == 2:
                break
        available = self.deadline.remaining() - self.final_reserve
        scenarios = ["probe:" + path.stem for path in sorted(self.probe_root.glob("*.yaml"))]
        if current != best:
            scenarios += ["public"]
        for scenario in scenarios:
            item = {"scenario": scenario, "seed": self.config.seeds[0], "faults": None}
            for source, digest in (("accepted", best), ("candidate", current)):
                seconds = self._experiment_cost(item, source, digest)
                if seconds <= 0 or seconds > available:
                    continue
                choices.append({"tool": "replay", "arguments": {"source": source, **item},
                                "source_hash": digest, "estimated_seconds": round(seconds, 3),
                                "reason": "compare identical effective scenario inputs before and after the repair"})
                if len(choices) >= 4:
                    return choices
        return choices

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
            return replace(cached, scenario=scenario, faults=faults)
        result = self.simulator.replay(root, scenario_arg, seed=seed, faults=faults_arg,
                                       timeout=60, reserve=self.final_reserve if reserve is None else reserve)
        result = replace(result, scenario=scenario, faults=faults)
        with self._lock:
            self.runs[result.run_id] = result
            if identity[0] is not None and result.status in VALID_REPLAY_STATUSES:
                self._cache[key] = result
            seconds = result.replay_command.duration + (result.check_command.duration if result.check_command else 0)
            timing_key = (result.scenario_hash, result.seed)
            timing = self._experiment_timings.setdefault(timing_key, {"count": 0, "total_seconds": 0.0,
                                                                     "worst_seconds": 0.0})
            timing["count"] += 1
            timing["total_seconds"] += seconds
            timing["worst_seconds"] = max(timing["worst_seconds"], seconds)
            if result.source_hash == self.sources.best_hash:
                self._retain_experiment(result)
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
            failures = [_ledger_replay(r, "accepted") for r in self.runs.values()
                        if r.source_hash == best and not r.ok][-VALIDATION_RESULT_LIMIT:]
            candidate = [_ledger_replay(r, "candidate") for r in self.runs.values()
                         if r.source_hash == current][-VALIDATION_RESULT_LIMIT:]
            last_validation = deepcopy(self.validation_history.get(current))
            history = [{key: outcome[key] for key in ("source_hash", "accepted", "reason", "stage", "attempt")}
                       for outcome in self.validation_history.values()]
            retained = [{"scenario": _ledger_text(item["scenario"], 256), "seed": _ledger_seed(item["seed"]),
                         "faults": _ledger_text(item["faults"], 256), "scenario_hash": item["scenario_hash"],
                         "source_hash": item["source_hash"]}
                        for item in list(self.retained_experiments.values())[-VALIDATION_RESULT_LIMIT:]]
            timings = list(self._experiment_timings.values())
            recommendations = self._experiment_recommendations(current, best)
        return {
            "source_hash": current, "accepted_source_hash": best,
            "unaccepted_changes": current != best,
            "last_validation": last_validation,
            "seconds_remaining": round(self.deadline.remaining(), 1),
            "final_reserve": round(self.final_reserve, 1),
            "remaining_budget_usd": self.model.remaining_budget if self.model is not None else self.config.budget_usd,
            "public_seeds": self.config.seeds, "accepted_repairs": len(self.accepted),
            "known_failures_source": "accepted", "known_failures_source_hash": best,
            "known_failures": failures, "candidate_results": candidate,
            "validation_history": history,
            "hypotheses": list(self.hypotheses.values()),
            "retained_experiments": retained, "retained_experiment_count": len(self.retained_experiments),
            "experiment_recommendations": recommendations,
            "experiment_metrics": {"measured_runs": sum(item["count"] for item in timings),
                                   "total_compute_seconds": round(sum(item["total_seconds"] for item in timings), 3),
                                   "worst_replay_seconds": round(self.replay_seconds, 3)},
        }

    def _validation_update(self, stage: str, **facts: Any) -> None:
        if self._validation_progress is not None:
            self._validation_progress.update(stage=stage, **facts)

    def _record_validation(self, result: dict, *, complete: bool = True) -> None:
        """Keep source-bound diagnostic history without changing acceptance gates."""
        if self._validation_progress is None:
            return
        outcome = deepcopy(self._validation_progress)
        outcome.update(accepted=result.get("accepted", False), complete=complete,
                       reason=_ledger_text(result.get("reason", "validation ended")))
        key = outcome["source_hash"]
        with self._lock:
            previous = self.validation_history.pop(key, None)
            outcome["attempt"] = previous["attempt"] + 1 if previous is not None else 1
            self.validation_history[key] = outcome
            while len(self.validation_history) > VALIDATION_HISTORY_LIMIT:
                del self.validation_history[next(iter(self.validation_history))]

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
        reviewed_hash = self.sources.candidate_hash
        tool = _review_tool(reviewed_hash)
        changed = self.sources.diff(max_chars=30000)
        context = "\n\n".join([
            "# Candidate review identity\n" + json.dumps({"source_hash": reviewed_hash}),
            "# Proposed repair\n" + json.dumps(plan), "# Hypotheses\n" + json.dumps(list(self.hypotheses.values())),
            "# Previously accepted obligations\n" + json.dumps([
                {key: accepted[key] for key in ("source_hash", "reason", "verified_obligations", "reproductions")}
                for accepted in self.accepted]),
            "# Current contracts\n" + self.contract_context,
            "# Source diff\n" + changed,
            "# Before/after evidence\n" + json.dumps({"before": [brief(r) for r in before], "after": [brief(r) for r in after]}),
            "# Focused local failure evidence\n" + json.dumps({
                "before": [brief(r, timeline=True) for r in before if not r.ok][:2],
                "after": [brief(r, timeline=True) for r in after if not r.ok][:2],
            }),
        ])
        started = time.monotonic()
        try:
            reply = self.model.messages(REVIEW, [{"role": "user", "content": context}], [tool],
                                        max_tokens=2048, reserve=max(10.0, self.test_seconds * 2 + self.replay_seconds * 3 + 5),
                                        tool_choice={"type": "tool", "name": REVIEW_TOOL_NAME})
            if reply.stop_reason != "tool_use":
                raise ModelError("review did not finish with a native tool decision", code="review_protocol")
            verdict = _review_verdict(reply.content, reviewed_hash)
            emit("repair_review", **verdict, cost_usd=reply.cost_usd)
            return verdict
        except (ModelError, TimeLimitError) as error:
            emit("review_unavailable", error=str(error))
            # A complete passing local matrix can still be accepted. Partial
            # repairs need a review to explain remaining/newly exposed failures.
            return {"approve": full_pass, "issues": [], "missing_evidence": [], "skipped": str(error)}
        finally:
            self.review_seconds = max(self.review_seconds, time.monotonic() - started)

    def validate(self, plan: dict) -> dict:
        if self._validating:
            raise ValueError("validation is already running")
        candidate_hash = self.sources.candidate_hash
        accepted_hash = self.sources.best_hash
        if candidate_hash == accepted_hash:
            return {"accepted": False, "reason": "candidate contains no source change"}
        self._validating = True
        self._validation_progress = {
            "source_hash": candidate_hash, "accepted_source_hash": accepted_hash,
            "proposal_reason": _ledger_text(plan.get("reason") if isinstance(plan, dict) else None),
            "stage": "starting",
            "tests": None,
            "matrix": {"complete": False, "full_pass": None, "before": [], "after": [], "counts": {}},
            "determinism": {"complete": False, "ok": None, "expected": 0, "completed": 0, "experiments": []},
            "witnesses": [], "witness_count": 0, "closed_invariants": [], "review": None,
        }
        try:
            result = self._validate(plan)
            self._record_validation(result)
            if "status" in result:
                result["status"] = self.status()
            return result
        except Exception as error:
            self._record_validation({"accepted": False, "reason": f"{type(error).__name__}: {error}"},
                                    complete=False)
            raise
        finally:
            self._validating = False
            self._validation_progress = None

    def _validate(self, plan: dict) -> dict:
        frozen = self.sources.candidate_hash
        self._validation_update("planning", source_hash=frozen)
        install_allowance = self.test_seconds * 2 + self.replay_seconds * 3 + 10
        if self.deadline.remaining() < install_allowance + 5:
            raise TimeLimitError("insufficient time for installed-source verification and rollback")
        targets = plan.get("target_invariants", [])
        reproductions = plan.get("reproductions", [])
        if not isinstance(targets, list) or any(not isinstance(v, str) for v in targets):
            raise ValueError("target_invariants must contain invariant names")
        if not isinstance(reproductions, list) or len(reproductions) > 12:
            raise ValueError("at most 12 reproductions are supported")
        self._validation_update("public_tests")
        tests = self.simulator.tests(self.sources.candidate, timeout=max(30.0, self.test_seconds * 3),
                                     reserve=install_allowance)
        self._validation_update("public_tests", tests=_ledger_tests(tests))
        if not tests.ok or self.sources.candidate_hash != frozen:
            return {"accepted": False, "reason": "public tests failed or source changed", "tests": tests.as_dict()}
        selection = self._select_validation_experiments(reproductions, install_allowance)
        experiments = selection.experiments
        matrix = {"complete": False, "full_pass": None, "before": [], "after": [],
                  "counts": {"experiments": len(experiments), "before": 0, "after": 0},
                  "selection": selection.summary(),
                  "truncated": len(experiments) > VALIDATION_RESULT_LIMIT}
        self._validation_update("matrix", matrix=matrix)
        before = self._matrix("accepted", experiments, reserve=install_allowance)
        matrix.update(before=[_ledger_replay(r, "accepted") for r in before[:VALIDATION_RESULT_LIMIT]])
        matrix["counts"].update(before=len(before), before_statuses=dict(Counter(r.status for r in before)))
        after = self._matrix("candidate", experiments, reserve=install_allowance)
        matrix.update(complete=True, full_pass=all(r.ok for r in after),
                      after=[_ledger_replay(r, "candidate") for r in after[:VALIDATION_RESULT_LIMIT]])
        matrix["counts"].update(after=len(after), after_statuses=dict(Counter(r.status for r in after)))
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
        self._validation_update("obligations", closed_invariants=[str(name)[:100] for name in sorted(closed)[:VALIDATION_RESULT_LIMIT]],
                                witnesses=[_ledger_replay(new, "candidate")
                                           for old, new in zip(before, after)
                                           if old.status in VALID_REPLAY_STATUSES | {"runtime_error"}
                                           and not old.ok and new.ok][:VALIDATION_RESULT_LIMIT],
                                witness_count=len(witnesses))
        if not closed and not witnesses:
            return {"accepted": False, "reason": "no reproduced obligation was resolved; construct a probe that fails on accepted source and passes on candidate",
                    "before": [brief(r) for r in before], "after": [brief(r) for r in after]}
        determinism_plan = [{"scenario": "public", "seed": self.config.seeds[0]}]
        for item in reproductions:
            if item not in determinism_plan:
                determinism_plan.append(item)
        determinism = {"complete": False, "ok": None, "expected": len(determinism_plan),
                       "completed": 0, "experiments": [], "truncated": len(determinism_plan) > VALIDATION_RESULT_LIMIT}
        self._validation_update("determinism", determinism=determinism)
        for item in determinism_plan:
            scenario_arg, faults_arg = self._paths(self.sources.candidate, item["scenario"], item.get("faults"))
            deterministic = self.simulator.determinism(self.sources.candidate, scenario_arg,
                                                       seed=item["seed"], faults=faults_arg, reserve=install_allowance)
            for run in deterministic.runs:
                self.runs[run.run_id] = replace(run, scenario=item["scenario"], faults=item.get("faults"))
            determinism["completed"] += 1
            entry = {
                "scenario": _ledger_text(item["scenario"], 256), "seed": _ledger_seed(item["seed"]),
                "faults": _ledger_text(item.get("faults"), 256), "ok": deterministic.ok,
                "run_ids": [_ledger_text(run.run_id, 64) for run in deterministic.runs[:4]],
                "source_hash": frozen,
            }
            if len(determinism["experiments"]) < VALIDATION_RESULT_LIMIT:
                determinism["experiments"].append(entry)
            if not deterministic.ok or self.sources.candidate_hash != frozen:
                determinism.update(ok=False, complete=determinism["completed"] == determinism["expected"],
                                   failed_experiment=entry)
                return {"accepted": False, "reason": "candidate is nondeterministic or changed", "experiment": item,
                        "determinism": deterministic.as_dict()}
        determinism.update(complete=True, ok=True)
        self._validation_update("review")
        verdict = self._review(plan, before, after, full_pass)
        self._validation_update("review", review=_ledger_review(verdict))
        if not verdict["approve"] or verdict["issues"] or verdict["missing_evidence"]:
            return {"accepted": False, "reason": "repair review requires changes or evidence", "review": verdict,
                    "results": [brief(r) for r in after]}
        if self.deadline.remaining() < install_allowance:
            raise TimeLimitError("insufficient time to promote the reviewed candidate")
        expected = next(r for r in after if r.scenario == "public" and r.seed == self.config.seeds[0] and r.faults is None)
        self._validation_update("installation")
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
                        {"scenario_hash": new.scenario_hash, "faults_hash": new.faults_hash,
                         "scenario": item["scenario"], "seed": new.seed, "faults": item.get("faults"),
                         "invariants": sorted(_invariants([old]) - _invariants([new]))}
                        for item, old, new in zip(experiments, before, after)
                        if _invariants([old]) - _invariants([new])],
                    "closed_invariants": sorted(closed), "full_local_pass": full_pass,
                    "results": [brief(r) for r in after], "promotion": promotion}
        self.accepted.append(accepted)
        for run in after:
            self._retain_experiment(run)
        self._validation_update("accepted")
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
