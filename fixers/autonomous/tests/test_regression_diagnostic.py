from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json

import pytest

from controller_support import Reviewer, controller, plan, repair_ownership, review_reply
from fixer.model import BudgetExhausted, ModelError, ModelReply
from fixer.patches import source_hash


class Diagnoser(Reviewer):
    def __init__(self, *, approve=False, error=None, invalid_reference=False, text_only=False):
        super().__init__()
        self.calls = []
        self.approve = approve
        self.error = error
        self.invalid_reference = invalid_reference
        self.text_only = text_only

    def messages(self, system, messages, tools, **kwargs):
        self.calls.append((system, deepcopy(messages), deepcopy(tools), kwargs))
        if self.error is not None:
            raise self.error
        if self.text_only:
            return ModelReply([{"type": "text", "text": "approve"}], "end_turn", {}, 0, 3)
        pairs = json.loads(messages[0]["content"].rsplit("# Matched experiment pairs and host facts\n", 1)[1])["pairs"]
        pair = next(item for item in pairs if item["facts"]["passing_control_regressed"])
        if self.invalid_reference:
            pair = next(item for item in pairs if not item["facts"]["before_pass"])
        issues = [] if self.approve else [{
            "kind": "regression", "source_path": "src/service.py", "pair_ids": [pair["pair_id"]],
            "obligation": "The previously healthy normal workload must remain supported.",
            "detail": "The staged normal-path failure branch causes the matched healthy input to fail; inspect its guard before changing recovery logic.",
        }]
        return review_reply({"approve": self.approve, "issues": issues,
                             "missing_evidence": [] if self.approve else [
                                 "Inspect the normal-path state writer and replay this same healthy input after a source correction."],
                             "limitations": ["This is causal advice; the host regression rejection remains authoritative."]}, tools)


def stage_regression(instance):
    original = instance.sources.best_hash
    repair_ownership(instance)
    candidate = instance.sources.apply_edits(instance.sources.candidate_hash, [{
        "path": "src/service.py", "old": "NORMAL_BROKEN = False", "new": "NORMAL_BROKEN = True"}])["source_hash"]
    instance.remember_repair_plan(original, candidate, plan())
    return original, candidate


def assert_rejected(instance, original, result):
    assert result["accepted"] is False
    assert result["reason"] == "previously passing scenario regressed"
    assert instance.sources.best_hash == source_hash(instance.sources.live / "src") == original
    assert not instance.accepted
    assert result["regression_diagnostic"]["scope"] == "diagnostic_only"
    assert result["regression_diagnostic"]["authorizes_promotion"] is False


def test_native_diagnosis_keeps_matching_sources_inputs_and_the_original_rejection(controller):
    original, candidate = stage_regression(controller)
    reviewer = controller.model = Diagnoser()
    result = controller.validate(controller.staged_plan())
    assert_rejected(controller, original, result)
    diagnosis = result["regression_diagnostic"]
    assert diagnosis["status"] == "completed" and len(reviewer.calls) == 1
    assert diagnosis["source_hash"] == candidate and diagnosis["accepted_source_hash"] == original
    reference = diagnosis["review"]["issue_evidence"][0]["experiments"][0]
    before = controller.runs[reference["before_run_id"]]
    after = controller.runs[reference["after_run_id"]]
    assert before.ok and not after.ok
    assert before.source_hash == reference["before_source_hash"] == original
    assert after.source_hash == reference["after_source_hash"] == candidate
    assert before.scenario_hash == after.scenario_hash == reference["input"]["scenario_hash"]
    assert before.faults_hash == after.faults_hash == reference["input"]["faults_hash"]
    assert before.seed == after.seed == reference["input"]["seed"]
    assert diagnosis["primary_pair"]["pair_id"] == reference["pair_id"]
    system, messages, tools, kwargs = reviewer.calls[0]
    assert "diagnostic-only" in system and "cannot authorize promotion" in system
    identity = json.loads(messages[0]["content"].split("\n\n", 1)[0].split("\n", 1)[1])
    assert identity["diagnostic_only"] is True and identity["authorizes_promotion"] is False
    assert plan()["reason"] in messages[0]["content"]
    assert "# Current source and unchanged consumers\n" in messages[0]["content"]
    assert tools[0]["input_schema"]["properties"]["source_hash"]["enum"] == [candidate]
    assert kwargs["tool_choice"] == {"type": "tool", "name": "submit_review"}
    assert kwargs["max_tokens"] == 2048
    assert kwargs["reserve"] >= controller.final_reserve + 30
    recorded = controller.status()["last_validation"]
    assert recorded["regression_diagnostic"] == diagnosis and recorded.get("review") is None


def test_even_an_approving_native_diagnosis_cannot_promote(controller):
    original, _ = stage_regression(controller)
    reviewer = controller.model = Diagnoser(approve=True)
    result = controller.validate(plan())
    assert_rejected(controller, original, result)
    assert result["regression_diagnostic"]["status"] == "completed"
    assert result["regression_diagnostic"]["review"]["approve"] is True
    assert len(reviewer.calls) == 1


def test_mixed_protected_loss_keeps_diagnostic_timelines_on_the_verified_passing_control(controller):
    partial = repair_ownership(controller)["source_hash"]
    assert controller.validate(plan())["accepted"]
    candidate = controller.sources.apply_edits(partial, [
        {"path": "src/service.py", "old": "OWNERSHIP_BROKEN = False", "new": "OWNERSHIP_BROKEN = True"},
        {"path": "src/service.py", "old": "NORMAL_BROKEN = False", "new": "NORMAL_BROKEN = True"},
    ])["source_hash"]
    controller.remember_repair_plan(partial, candidate, plan())
    reviewer = controller.model = Diagnoser()
    result = controller.validate(plan())
    assert not result["accepted"] and result["reason"] == "previously passing scenario regressed"
    assert source_hash(controller.sources.live / "src") == controller.sources.best_hash == partial
    assert len(controller.accepted) == 1 and len(reviewer.calls) == 1
    diagnosis = result["regression_diagnostic"]
    pairs = json.loads(reviewer.calls[0][1][0]["content"].rsplit(
        "# Matched experiment pairs and host facts\n", 1)[1])["pairs"]
    protected = next(pair for pair in pairs if pair["facts"]["protected_obligation_regressed"])
    control = next(pair for pair in pairs if pair["pair_id"] == diagnosis["primary_pair"]["pair_id"])
    assert pairs.index(protected) < pairs.index(control)
    assert control["facts"]["passing_control_regressed"]
    assert "timeline" in control["before"] and "timeline" in control["after"]
    assert "timeline" not in protected["before"] and "timeline" not in protected["after"]
    assert control["before"]["run_id"] == diagnosis["primary_pair"]["before_run_id"]
    assert control["after"]["run_id"] == diagnosis["primary_pair"]["after_run_id"]
    assert diagnosis["status"] == "completed"


def test_same_run_identity_reuses_diagnosis_but_another_source_gets_no_old_advice(controller):
    original, candidate = stage_regression(controller)
    reviewer = controller.model = Diagnoser()
    first = controller.validate(plan())
    second = controller.validate(plan())
    assert_rejected(controller, original, first)
    assert_rejected(controller, original, second)
    assert second["regression_diagnostic"]["cached"] is True and len(reviewer.calls) == 1
    assert second["regression_diagnostic"]["primary_pair"] == first["regression_diagnostic"]["primary_pair"]
    changed = controller.sources.apply_edits(candidate, [{
        "path": "src/service.py", "old": "def calculate(value):", "new": "# changed candidate\ndef calculate(value):"}])["source_hash"]
    third = controller.validate(plan())
    assert_rejected(controller, original, third)
    assert third["regression_diagnostic"]["source_hash"] == changed
    assert third["regression_diagnostic"]["status"] == "skipped"
    assert "review" not in third["regression_diagnostic"] and len(reviewer.calls) == 1


def test_same_source_with_new_experiment_does_not_relabel_the_old_diagnosis(controller):
    original, candidate = stage_regression(controller)
    reviewer = controller.model = Diagnoser()
    controller.validate(plan())
    result = controller.validate({**plan(), "reproductions": [{"scenario": "base", "seed": 3}]})
    assert_rejected(controller, original, result)
    diagnosis = result["regression_diagnostic"]
    assert diagnosis["status"] == "skipped" and diagnosis["source_hash"] == candidate
    assert "review" not in diagnosis and "primary_pair" not in diagnosis and len(reviewer.calls) == 1


@pytest.mark.parametrize("balance", [0, 0.1, float("nan"), float("inf")])
def test_budget_or_feedback_headroom_cannot_turn_a_regression_into_approval(controller, balance):
    original, _ = stage_regression(controller)
    reviewer = controller.model = Diagnoser(approve=True)
    reviewer.remaining_budget = balance
    result = controller.validate(plan())
    assert_rejected(controller, original, result)
    assert result["regression_diagnostic"]["status"] == "skipped" and not reviewer.calls


def test_late_matrix_keeps_final_verification_and_feedback_reserves(controller, monkeypatch):
    original, _ = stage_regression(controller)
    reviewer = controller.model = Diagnoser()
    matrix = controller._matrix

    def late(source, *args, **kwargs):
        result = matrix(source, *args, **kwargs)
        if source == "candidate":
            monkeypatch.setattr(controller.deadline, "remaining", lambda reserve=0: 60 - reserve)
        return result

    monkeypatch.setattr(controller, "_matrix", late)
    result = controller.validate(plan())
    assert_rejected(controller, original, result)
    assert result["regression_diagnostic"]["status"] == "skipped" and not reviewer.calls
    assert "time" in result["regression_diagnostic"]["reason"]


@pytest.mark.parametrize("error", [ModelError("upstream unavailable", code="http_error"), BudgetExhausted(4, 3)])
def test_unavailable_or_budget_refused_native_request_preserves_rejection_and_one_call(controller, error):
    original, _ = stage_regression(controller)
    reviewer = controller.model = Diagnoser(error=error)
    first = controller.validate(plan())
    second = controller.validate(plan())
    assert_rejected(controller, original, first)
    assert_rejected(controller, original, second)
    assert first["regression_diagnostic"]["status"] == "failed"
    assert not first["regression_diagnostic"]["review"]["approve"]
    assert second["regression_diagnostic"]["cached"] and len(reviewer.calls) == 1


@pytest.mark.parametrize("during_request", [False, True])
def test_cancellation_never_overrides_the_completed_regression_gate(controller, monkeypatch, during_request):
    original, _ = stage_regression(controller)

    class Cancelling(Diagnoser):
        def messages(self, *args, **kwargs):
            reply = super().messages(*args, **kwargs)
            controller.deadline.stop_event.set()
            return reply

    reviewer = controller.model = Cancelling(approve=True)
    if not during_request:
        matrix = controller._matrix

        def cancel(source, *args, **kwargs):
            result = matrix(source, *args, **kwargs)
            if source == "candidate":
                controller.deadline.stop_event.set()
            return result

        monkeypatch.setattr(controller, "_matrix", cancel)
    result = controller.validate(plan())
    assert_rejected(controller, original, result)
    assert result["regression_diagnostic"]["status"] == ("failed" if during_request else "skipped")
    assert len(reviewer.calls) == int(during_request)


def test_mismatched_host_input_cannot_spend_the_single_valid_diagnosis_opportunity(controller, monkeypatch):
    original, _ = stage_regression(controller)
    reviewer = controller.model = Diagnoser()
    matrix = controller._matrix

    def mismatch(source, *args, **kwargs):
        results = matrix(source, *args, **kwargs)
        if source == "candidate":
            return [replace(run, scenario_hash="f" * 64) if not run.ok and run.scenario == "base" else run
                    for run in results]
        return results

    monkeypatch.setattr(controller, "_matrix", mismatch)
    first = controller.validate(plan())
    assert_rejected(controller, original, first)
    assert first["regression_diagnostic"]["status"] == "skipped" and not reviewer.calls
    monkeypatch.setattr(controller, "_matrix", matrix)
    second = controller.validate(plan())
    assert_rejected(controller, original, second)
    assert second["regression_diagnostic"]["status"] == "completed" and len(reviewer.calls) == 1


@pytest.mark.parametrize("damage", ["changed", "missing", "symlink"])
def test_invalid_causal_trace_is_not_sent_to_the_independent_model(controller, monkeypatch, tmp_path, damage):
    original, _ = stage_regression(controller)
    reviewer = controller.model = Diagnoser()
    matrix = controller._matrix

    def corrupt(source, *args, **kwargs):
        results = matrix(source, *args, **kwargs)
        if source == "candidate":
            trace = next(run.trace_path for run in results if run.scenario == "base")
            if damage == "changed":
                trace.write_text("{}\n")
            else:
                trace.unlink()
                if damage == "symlink":
                    outside = tmp_path / "unrelated-trace.jsonl"
                    outside.write_text("{}\n")
                    trace.symlink_to(outside)
        return results

    monkeypatch.setattr(controller, "_matrix", corrupt)
    result = controller.validate(plan())
    assert_rejected(controller, original, result)
    assert result["regression_diagnostic"]["status"] == "skipped" and not reviewer.calls


def test_invalid_native_reference_is_reported_without_a_correction_or_fallback(controller):
    original, _ = stage_regression(controller)
    reviewer = controller.model = Diagnoser(invalid_reference=True)
    result = controller.validate(plan())
    assert_rejected(controller, original, result)
    diagnosis = result["regression_diagnostic"]
    assert diagnosis["status"] == "failed" and diagnosis["review"]["evidence_error"]
    assert diagnosis["review"]["approve"] is False and len(reviewer.calls) == 1
    assert not diagnosis["review"].get("correction_attempted")


def test_text_only_response_is_failed_advice_and_never_an_approval_fallback(controller):
    original, _ = stage_regression(controller)
    reviewer = controller.model = Diagnoser(text_only=True)
    result = controller.validate(plan())
    assert_rejected(controller, original, result)
    assert result["regression_diagnostic"]["status"] == "failed" and len(reviewer.calls) == 1
    assert result["regression_diagnostic"]["review"]["approve"] is False


def test_bounded_diagnostic_advice_keeps_exact_references_and_omission_counts(controller):
    original, candidate = stage_regression(controller)

    class LongAdvice(Diagnoser):
        def messages(self, *args, **kwargs):
            reply = super().messages(*args, **kwargs)
            reply.content[0]["input"]["missing_evidence"] = ["x" * 2000 for _ in range(20)]
            return reply

    controller.model = LongAdvice()
    result = controller.validate(plan())
    assert_rejected(controller, original, result)
    diagnosis = result["regression_diagnostic"]
    assert len(json.dumps(diagnosis, ensure_ascii=False)) < 16000
    assert diagnosis["review"]["truncated"]
    assert diagnosis["review"]["missing_evidence_count"] == 20
    assert len(diagnosis["review"]["missing_evidence"]) < 20
    reference = diagnosis["review"]["issue_evidence"][0]["experiments"][0]
    assert reference["before_source_hash"] == original and reference["after_source_hash"] == candidate
    assert reference["input"]["scenario_hash"] == controller.runs[reference["after_run_id"]].scenario_hash
