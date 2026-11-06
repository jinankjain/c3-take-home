from __future__ import annotations

import pytest

from fixer.patches import source_hash
from controller_support import Reviewer, controller, plan, repair_ownership


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
