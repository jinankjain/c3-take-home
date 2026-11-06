from __future__ import annotations

from dataclasses import replace
import json

import pytest

from fixer.model import ModelReply
from fixer.patches import source_hash
from controller_support import Reviewer, controller, plan, repair_ownership


class ReviewerWithGaps(Reviewer):
    def __init__(self, gaps):
        super().__init__()
        self.gaps = gaps

    def messages(self, system, messages, tools, **kwargs):
        self.reviews.append(messages)
        verdict = {"approve": True, "issues": [], "missing_evidence": self.gaps}
        return ModelReply([{"type": "text", "text": json.dumps(verdict)}], "end_turn", {}, 0, 3)


def repair_all_protocol_failures(instance):
    return instance.sources.apply_edits(instance.sources.candidate_hash, [
        {"path": "src/service.py", "old": "OWNERSHIP_BROKEN = True", "new": "OWNERSHIP_BROKEN = False"},
        {"path": "src/service.py", "old": "RECOVERY_BROKEN = True", "new": "RECOVERY_BROKEN = False"},
    ])["source_hash"]


def test_baseline_passes_public_tests_but_exposes_both_protocol_failures(controller):
    assert controller.baseline_tests.ok
    assert {v["invariant"] for result in controller.baseline for v in result.violations} == {"OWNERSHIP", "RECOVERY"}
    assert all(result.scenario == "public" for result in controller.baseline)


def test_valid_partial_repair_is_promoted_while_other_bug_remains(controller):
    repaired = repair_ownership(controller)["source_hash"]
    result = controller.validate(plan())
    assert result["accepted"] and not result["full_local_pass"]
    assert result["closed_invariants"] == ["OWNERSHIP"]
    assert source_hash(controller.sources.live / "src") == repaired == controller.sources.best_hash
    assert all(r["violations"][0]["invariant"] == "RECOVERY" for r in result["results"] if r["scenario"] == "public")
    assert controller.model.reviews
    assert controller.finalize()["verified"]


def test_public_regression_keeps_original_live(controller):
    original = controller.sources.best_hash
    repair_ownership(controller)
    controller.sources.apply_edits(controller.sources.candidate_hash, [{"path": "src/service.py", "old": "value * 2", "new": "value * 3"}])
    result = controller.validate(plan())
    assert not result["accepted"] and "public tests" in result["reason"]
    assert source_hash(controller.sources.live / "src") == original


def test_healthy_control_regression_keeps_original_live(controller):
    original = controller.sources.best_hash
    repair_ownership(controller)
    controller.sources.apply_edits(controller.sources.candidate_hash, [{"path": "src/service.py", "old": "NORMAL_BROKEN = False", "new": "NORMAL_BROKEN = True"}])
    result = controller.validate(plan())
    assert not result["accepted"] and "regressed" in result["reason"]
    assert source_hash(controller.sources.live / "src") == original


def test_review_rejection_keeps_candidate_out_of_live_source(controller):
    original = controller.sources.best_hash
    controller.model = Reviewer(False)
    repair_ownership(controller)
    result = controller.validate(plan())
    assert not result["accepted"] and result["review"]["issues"]
    assert source_hash(controller.sources.live / "src") == original


def test_validation_can_spend_time_reserved_from_investigation(controller):
    from fixer.runtime import Deadline
    import time
    controller.deadline = controller.runner.deadline = Deadline(time.time() + 60)
    controller.final_reserve = 90
    repair_ownership(controller)
    assert controller.validate(plan())["accepted"]


def test_second_partial_repair_cannot_reintroduce_a_verified_obligation(controller):
    repair_ownership(controller)
    assert controller.validate(plan())["accepted"]
    checkpoint = controller.sources.best_hash
    controller.sources.apply_edits(controller.sources.candidate_hash, [
        {"path": "src/service.py", "old": "OWNERSHIP_BROKEN = False", "new": "OWNERSHIP_BROKEN = True"},
        {"path": "src/service.py", "old": "RECOVERY_BROKEN = True", "new": "RECOVERY_BROKEN = False"}])
    result = controller.validate({**plan(), "target_invariants": ["RECOVERY"]})
    assert not result["accepted"] and result["lost_obligations"]
    assert source_hash(controller.sources.live / "src") == checkpoint


def test_installation_allows_harmless_root_metadata_in_trace(controller):
    for case in (controller.sources.original, controller.sources.best, controller.sources.candidate, controller.sources.live):
        path = case / "sim/__main__.py"
        path.write_text(path.read_text().replace('"node": "sim",', '"node": "sim", "root": str(root),'))
    controller._cache.clear()
    repair_ownership(controller)
    assert controller.validate(plan())["accepted"]


def test_json_shipped_scenarios_are_supported(controller):
    for case in (controller.sources.best, controller.sources.candidate):
        (case / "scenarios/json_case.json").write_text('{"name": "json_case"}')
    assert controller.replay("candidate", "json_case", 1).scenario == "json_case"


def test_empty_path_parts_are_rejected(controller):
    with pytest.raises(ValueError):
        controller.replay("candidate", ".", 1)


def test_targeted_probe_must_be_deterministic(controller):
    from fixer.tools import ToolDispatcher
    probe = ToolDispatcher(controller).execute("write_probe", {"name": "targeted", "scenario": {"name": "targeted", "checks": ["OWNERSHIP"]}})
    assert probe["ok"]
    for case in (controller.sources.original, controller.sources.best, controller.sources.candidate, controller.sources.live):
        path = case / "sim/__main__.py"
        path.write_text(path.read_text().replace('text = json.dumps(event, sort_keys=True)',
                                               'event["nonce"] = id(object()) if scenario["name"] == "targeted" else 0\n    text = json.dumps(event, sort_keys=True)'))
    controller._cache.clear()
    repair_ownership(controller)
    result = controller.validate({**plan(), "reproductions": [{"scenario": "probe:targeted", "seed": 1}]})
    assert not result["accepted"] and "nondeterministic" in result["reason"]
    record = controller.status()["last_validation"]["determinism"]
    assert record["complete"] and not record["ok"]
    assert record["failed_experiment"]["scenario"] == "probe:targeted"


@pytest.mark.parametrize("scenario,faults", [("../secret", None), ("/etc/passwd", None), ("public", "../../secret")])
def test_replay_paths_cannot_leave_case_inputs(controller, scenario, faults):
    with pytest.raises(ValueError):
        controller.replay("candidate", scenario, 1, faults)


def test_failed_final_tests_restore_previous_checkpoint(controller, monkeypatch):
    from fixer.runtime import CommandResult
    original = controller.sources.best_hash
    repair_ownership(controller)
    assert controller.validate(plan())["accepted"]
    real = controller.simulator.tests
    failed = False
    def fail_once(root, **kwargs):
        nonlocal failed
        if root == controller.sources.live and not failed:
            failed = True
            return CommandResult(("pytest",), 1, "failed", "", 0)
        return real(root, **kwargs)
    monkeypatch.setattr(controller.simulator, "tests", fail_once)
    assert controller.finalize()["verified"]
    assert source_hash(controller.sources.live / "src") == original
    assert not controller.accepted


def test_interrupted_final_tests_retain_verified_repair(controller, monkeypatch):
    from fixer.runtime import CommandResult
    repair_ownership(controller)
    assert controller.validate(plan())["accepted"]
    accepted = controller.sources.best_hash
    monkeypatch.setattr(controller.simulator, "tests", lambda *args, **kwargs: CommandResult(("pytest",), -9, "", "", 0, cancelled=True))
    result = controller.finalize()
    assert result["verified"] is None
    assert source_hash(controller.sources.live / "src") == accepted


def test_passing_candidate_review_rejection_remains_in_source_bound_validation_ledger(controller):
    original = controller.sources.best_hash
    candidate = repair_all_protocol_failures(controller)
    gaps = ["Replay a persisted winning operation after a restart", "Hidden-seed grader results"]
    controller.model = ReviewerWithGaps(gaps)
    result = controller.validate(plan())
    assert not result["accepted"] and controller.sources.best_hash == original
    record = controller.status()["last_validation"]
    assert record["source_hash"] == candidate and record["accepted_source_hash"] == original
    assert not record["accepted"] and record["stage"] == "review" and record["complete"]
    assert record["reason"] == "repair review requires changes or evidence"
    assert record["tests"]["ok"]
    assert record["matrix"]["complete"] and record["matrix"]["full_pass"]
    assert {row["source_hash"] for row in record["matrix"]["before"]} == {original}
    assert all(row["source_hash"] == candidate and row["status"] == "pass"
               for row in record["matrix"]["after"])
    assert record["witness_count"] and all(row["run_id"] for row in record["witnesses"])
    assert record["determinism"]["complete"] and record["determinism"]["ok"]
    assert all(item["run_ids"] for item in record["determinism"]["experiments"])
    assert record["review"]["approve"] and record["review"]["missing_evidence"] == gaps
    record["review"]["missing_evidence"].clear()
    assert controller.status()["last_validation"]["review"]["missing_evidence"] == gaps


def test_changing_source_does_not_attribute_a_previous_validation_verdict_to_new_candidate(controller):
    previous = repair_all_protocol_failures(controller)
    controller.model = ReviewerWithGaps(["Replay the recovery transition"])
    assert not controller.validate(plan())["accepted"]
    revised = controller.sources.apply_edits(previous, [{
        "path": "src/service.py", "old": "NORMAL_BROKEN = False",
        "new": "NORMAL_BROKEN = False  # source revision",
    }])["source_hash"]
    status = controller.status()
    assert revised != previous and status["source_hash"] == revised
    assert status["last_validation"] is None and not status["candidate_results"]
    assert status["validation_history"][-1]["source_hash"] == previous
    assert controller.validation_history[previous]["review"]["missing_evidence"] == ["Replay the recovery transition"]


def test_status_distinguishes_accepted_failures_from_passing_current_candidate(controller):
    accepted = controller.sources.best_hash
    candidate = repair_all_protocol_failures(controller)
    assert controller.replay("candidate", "public", 1).ok
    status = controller.status()
    assert status["known_failures_source"] == "accepted"
    assert status["known_failures_source_hash"] == accepted
    assert status["known_failures"] and all(row["snapshot"] == "accepted" and row["source_hash"] == accepted
                                            and not row["ok"] for row in status["known_failures"])
    assert status["candidate_results"] and all(row["snapshot"] == "candidate" and row["source_hash"] == candidate
                                               and row["ok"] for row in status["candidate_results"])


def test_revalidation_updates_same_source_outcome_and_returned_status(controller):
    candidate = repair_all_protocol_failures(controller)
    controller.model = ReviewerWithGaps(["Replay the recovery transition"])
    assert not controller.validate(plan())["accepted"]
    controller.model.gaps = []
    result = controller.validate(plan())
    assert result["accepted"] and controller.sources.best_hash == candidate
    record = result["status"]["last_validation"]
    assert record["source_hash"] == candidate and record["accepted"] and record["stage"] == "accepted"
    assert record["attempt"] == 2 and not record["review"]["missing_evidence"]
    assert len(controller.validation_history) == 1 and not result["status"]["known_failures"]


def test_validation_ledger_bounds_review_text_without_discarding_acceptance_requirements(controller):
    repair_all_protocol_failures(controller)
    gaps = [f"required witness {index}: " + "x" * 2000 for index in range(20)]
    controller.model = ReviewerWithGaps(gaps)
    result = controller.validate({**plan(), "reason": "proposal " + "x" * 10_000})
    assert not result["accepted"] and result["review"]["missing_evidence"] == gaps
    record = controller.status()["last_validation"]
    assert len(record["proposal_reason"]) <= 800
    assert record["review"]["missing_evidence_count"] == 20 and record["review"]["truncated"]
    assert len(record["review"]["missing_evidence"]) <= 6
    assert all(len(gap) <= 800 for gap in record["review"]["missing_evidence"])
    assert len(record["matrix"]["before"]) <= 12 and len(record["matrix"]["after"]) <= 12
    assert len(json.dumps(record)) < 25_000


def test_validation_history_bounds_source_versions_and_retains_early_test_failure(controller, monkeypatch):
    from fixer.runtime import CommandResult
    monkeypatch.setattr(controller.simulator, "tests",
                        lambda *_args, **_kwargs: CommandResult(("pytest",), 1, "failed", "", .001))
    hashes = []
    previous = "NORMAL_BROKEN = False"
    for version in range(12):
        updated = f"NORMAL_BROKEN = False  # candidate {version}"
        hashes.append(controller.sources.apply_edits(controller.sources.candidate_hash, [{
            "path": "src/service.py", "old": previous, "new": updated,
        }])["source_hash"])
        previous = updated
        assert not controller.validate(plan())["accepted"]
    status = controller.status()
    assert len(controller.validation_history) == 8
    assert list(controller.validation_history) == hashes[-8:]
    assert len(status["validation_history"]) == 8
    record = status["last_validation"]
    assert record["source_hash"] == hashes[-1] and record["stage"] == "public_tests"
    assert not record["tests"]["ok"] and not record["matrix"]["complete"]
    assert record["review"] is None and "public tests" in record["reason"]


def test_status_bounds_replay_rows_and_failure_payloads(controller):
    baseline = controller.baseline[0]
    for index in range(40):
        controller.runs[f"replay-{index}"] = replace(
            baseline, run_id=f"replay-{index}", scenario="s" * 1000, seed=1 << 100_000, error="e" * 10_000,
            violations=[{"invariant": f"I{number:02d}" + "x" * 1000, "detail": "d" * 10_000}
                        for number in range(20)])
    status = controller.status()
    for field in ("known_failures", "candidate_results"):
        assert len(status[field]) <= 12
        for row in status[field]:
            assert len(row["scenario"]) <= 256 and len(row["error"]) <= 400
            assert len(row["invariants"]) <= 8 and all(len(name) <= 100 for name in row["invariants"])
            assert isinstance(row["seed"], str) and len(row["seed"]) < 100
    assert len(json.dumps(status)) < 100_000


@pytest.mark.parametrize("stage", ["public_tests", "determinism"])
def test_interrupted_validation_records_completed_facts_and_preserves_exception_behavior(controller, monkeypatch, stage):
    from fixer.runtime import TimeLimitError
    candidate = repair_all_protocol_failures(controller)
    def interrupt(*_args, **_kwargs):
        raise TimeLimitError("validation budget exhausted")
    monkeypatch.setattr(controller.simulator, "tests" if stage == "public_tests" else "determinism", interrupt)
    with pytest.raises(TimeLimitError, match="validation budget exhausted"):
        controller.validate(plan())
    record = controller.status()["last_validation"]
    assert record["source_hash"] == candidate and not record["accepted"] and not record["complete"]
    assert record["stage"] == stage
    if stage == "determinism":
        assert record["tests"]["ok"] and record["matrix"]["complete"] and record["matrix"]["full_pass"]
    else:
        assert record["tests"] is None and not record["matrix"]["complete"]
    assert not record["determinism"]["complete"] and record["review"] is None
    assert "TimeLimitError" in record["reason"] and not controller._validating
