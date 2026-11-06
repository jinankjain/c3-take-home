from __future__ import annotations

import pytest

from dataclasses import replace

from controller_support import controller, plan, repair_ownership
from fixer.controller import brief
from fixer.experiments import select_experiments
from fixer.runtime import TimeLimitError
from fixer.tools import ToolDispatcher


def test_selection_preserves_required_checks_and_fits_cheaper_extra_coverage():
    required = [{"name": "public", "cost": (0, 10)}, {"name": "retained", "cost": (0, 10)}]
    optional = [{"name": "expensive", "cost": (20, 20)}, {"name": "cheap", "cost": (0, 1)}]
    selection = select_experiments(required, optional, costs=lambda item: item["cost"],
                                   priority=lambda item: sum(item["cost"]), available_seconds=16)
    assert {item["name"] for item in selection.experiments} == {"public", "retained", "cheap"}
    assert selection.required_count == 2 and selection.optional_count == selection.omitted_count == 1
    assert selection.estimated_seconds <= 16


def test_required_regressions_are_rejected_when_they_cannot_fit_not_silently_dropped():
    required = [{"name": "retained", "cost": (10, 10)}]
    with pytest.raises(TimeLimitError, match="required"):
        select_experiments(required, [], costs=lambda item: item["cost"],
                           priority=lambda _item: (), available_seconds=5)


def test_matrix_estimate_preserves_separate_before_and_after_execution_phases():
    required = [{"cost": (10, 1)}, {"cost": (1, 10)}]
    with pytest.raises(TimeLimitError):
        select_experiments(required, [], costs=lambda item: item["cost"],
                           priority=lambda _item: (), available_seconds=20)
    selected = select_experiments(required, [], costs=lambda item: item["cost"],
                                 priority=lambda _item: (), available_seconds=30)
    assert selected.estimated_seconds == 27


def test_exploratory_passing_probe_is_retained_even_when_omitted_from_a_later_plan(controller):
    for root in (controller.sources.original, controller.sources.best,
                 controller.sources.candidate, controller.sources.live):
        path = root / "src/service.py"
        path.write_text(path.read_text() + "AUDIT_BROKEN = False\n")
    controller._cache.clear()
    dispatcher = ToolDispatcher(controller)
    assert dispatcher.execute("write_probe", {"name": "audit", "scenario": {"name": "audit", "checks": ["AUDIT"]}})["ok"]
    original = controller.sources.best_hash
    assert controller.replay("accepted", "probe:audit", 23).ok
    repair_ownership(controller)
    controller.sources.apply_edits(controller.sources.candidate_hash, [{
        "path": "src/service.py", "old": "AUDIT_BROKEN = False", "new": "AUDIT_BROKEN = True"}])
    result = controller.validate(plan())
    assert not result["accepted"] and "previously passing scenario regressed" in result["reason"]
    assert controller.sources.best_hash == original
    assert any(item["scenario"] == "probe:audit" for item in controller.retained_experiments.values())


def test_retained_probe_input_changes_cannot_reuse_a_previous_passing_witness(controller):
    dispatcher = ToolDispatcher(controller)
    assert dispatcher.execute("write_probe", {"name": "control", "scenario": {"name": "control", "checks": ["NORMAL"]}})["ok"]
    assert controller.replay("accepted", "probe:control", 5).ok
    probe = controller.probe_root / "control.yaml"
    probe.chmod(0o644)
    probe.write_text(probe.read_text() + "runtime: 42\n")
    repair_ownership(controller)
    with pytest.raises(ValueError, match="effective inputs changed"):
        controller.validate(plan())
    assert not controller.accepted


def test_cached_scenario_alias_retains_requested_identity_and_measured_work_is_not_recounted(controller):
    initial_count = controller.status()["experiment_metrics"]["measured_runs"]
    seed = controller.config.seeds[0]
    original = controller.replay("accepted", "public", seed)
    alias = controller.replay("candidate", "scenarios/public.yaml", seed)
    assert alias.run_id == original.run_id and alias.scenario == "scenarios/public.yaml"
    assert controller.status()["experiment_metrics"]["measured_runs"] == initial_count


def test_successful_validation_retains_every_passing_matrix_experiment(controller):
    repair_ownership(controller)
    assert controller.validate(plan())["accepted"]
    retained = controller.retained_experiments.values()
    assert any(item["scenario"] == "base" for item in retained)
    assert all(item["source_hash"] == controller.sources.best_hash for item in retained)
    selection = controller.status()["last_validation"]["matrix"]["selection"]
    assert selection["required"] >= len(controller.config.seeds) + 1
    assert selection["additional"] == 3 and selection["additional_omitted"] == 0


def _stage_recovery_repair_with_seed_specific_ownership_regression(controller, seed):
    condition = ('("--seed" in __import__("sys").argv and '
                 'int(__import__("sys").argv[__import__("sys").argv.index("--seed") + 1]) == ' + str(seed) + ')')
    controller.sources.apply_edits(controller.sources.candidate_hash, [
        {"path": "src/service.py", "old": "OWNERSHIP_BROKEN = False", "new": "OWNERSHIP_BROKEN = " + condition},
        {"path": "src/service.py", "old": "RECOVERY_BROKEN = True", "new": "RECOVERY_BROKEN = False"},
    ])


@pytest.mark.parametrize("legacy_checkpoint", [False, True])
def test_partial_repair_obligation_on_still_failing_extra_seed_is_mandatory(controller, legacy_checkpoint):
    repair_ownership(controller)
    first = controller.validate(plan())
    assert first["accepted"]
    original = controller.sources.best_hash
    extra = controller._extra_seeds()[0]
    obligation = next(item for item in controller.accepted[0]["verified_obligations"] if item["seed"] == extra)
    assert obligation["invariants"] == ["OWNERSHIP"]
    assert next(item for item in first["results"] if item["seed"] == extra)["status"] == "invariant_failure"
    assert not any(item["seed"] == extra for item in controller.retained_experiments.values())
    if legacy_checkpoint:
        for item in controller.accepted[0]["verified_obligations"]:
            item.pop("faults", None)
            item.pop("faults_hash", None)
    _stage_recovery_repair_with_seed_specific_ownership_regression(controller, extra)
    result = controller.validate({"target_invariants": ["RECOVERY"], "reproductions": [],
                                  "reason": "repair recovery while preserving accepted ownership"})
    assert not result["accepted"]
    assert "previously verified obligation regressed" in result["reason"]
    assert controller.sources.best_hash == original
    matrix = controller.status()["last_validation"]["matrix"]
    assert matrix["selection"]["required"] == len(controller.config.seeds) + len(controller._extra_seeds()) + 1
    assert matrix["selection"]["additional"] == 0
    assert any(item["seed"] == extra for item in matrix["after"])


def test_expensive_partial_extra_obligation_cannot_be_omitted_for_a_later_repair(controller, monkeypatch):
    repair_ownership(controller)
    assert controller.validate(plan())["accepted"]
    original = controller.sources.best_hash
    extra = controller._extra_seeds()[0]
    _stage_recovery_repair_with_seed_specific_ownership_regression(controller, extra)
    measured = controller._experiment_cost
    assert measured({"scenario": "public", "seed": extra, "faults": None}, "accepted", original) == 0
    monkeypatch.setattr(controller, "_experiment_cost", lambda item, source, digest:
                        10_000.0 if item["seed"] == extra and source == "candidate" else measured(item, source, digest))
    with pytest.raises(TimeLimitError, match="required"):
        controller.validate({"target_invariants": ["RECOVERY"], "reproductions": [], "reason": "repair recovery"})
    assert controller.sources.best_hash == original and len(controller.accepted) == 1
    assert len(controller.model.reviews) == 1
    assert not controller.status()["last_validation"]["matrix"]["complete"]


@pytest.mark.parametrize("mutation", ["changed", "missing"])
def test_retained_partial_obligation_rejects_missing_or_changed_effective_scenario_inputs(controller, mutation):
    repair_ownership(controller)
    assert controller.validate(plan())["accepted"]
    original = controller.sources.best_hash
    controller.sources.apply_edits(controller.sources.candidate_hash, [
        {"path": "src/service.py", "old": "RECOVERY_BROKEN = True", "new": "RECOVERY_BROKEN = False"}])
    scenario = controller.sources.candidate / "scenarios/public.yaml"
    if mutation == "changed":
        scenario.write_text(scenario.read_text() + "end_at: 42\n")
    else:
        scenario.unlink()
    with pytest.raises(ValueError):
        controller.validate({"target_invariants": ["RECOVERY"], "reproductions": [], "reason": "repair recovery"})
    assert controller.sources.best_hash == original and len(controller.accepted) == 1
    assert len(controller.model.reviews) == 1


def test_causal_review_excerpt_keeps_both_ends_and_reports_its_actual_selection(controller):
    events = [{"seq": index} for index in range(24)]
    result = replace(controller.baseline[0], evidence={
        "timeline": events, "timeline_matched_events": 30,
        "failure_witnesses": events[:6],
        "causal_windows": [{"events": events[:12], "selected_events": 15,
                            "returned_events": 12, "omitted_selected_events": 3}],
    })
    excerpt = brief(result, timeline=True)
    assert [item["seq"] for item in excerpt["timeline"]] == list(range(8)) + list(range(16, 24))
    assert excerpt["timeline_omitted_events"] == 14 and excerpt["timeline_truncated"]
    assert excerpt["failure_witnesses_brief_omitted"] == 2
    window = excerpt["causal_windows"][0]
    assert [item["seq"] for item in window["events"]] == list(range(4)) + list(range(8, 12))
    assert window["returned_events"] == 8 and window["omitted_selected_events"] == 7
    assert window["selection_truncated"] and window["brief_omitted_events"] == 4
