from __future__ import annotations

import json
import time

import pytest

from fixer.main import compact_messages, prompt_status, repair_loop, validate_pending_candidate
from fixer.model import BudgetExhausted, ModelError, ModelReply
from fixer.patches import source_hash
from controller_support import Reviewer, controller, plan


def test_tool_loop_applies_and_validates_a_native_model_patch(controller):
    class ScriptedModel(Reviewer):
        turns = 0
        def messages(self, system, messages, tools, **kwargs):
            if not tools:
                return super().messages(system, messages, tools, **kwargs)
            self.turns += 1
            if self.turns == 1:
                block = {"type": "tool_use", "id": "edit", "name": "apply_edits", "input": {
                    "base_hash": controller.sources.candidate_hash,
                    "edits": [{"path": "src/service.py", "old": "OWNERSHIP_BROKEN = True", "new": "OWNERSHIP_BROKEN = False"},
                              {"path": "src/service.py", "old": "RECOVERY_BROKEN = True", "new": "RECOVERY_BROKEN = False"}]}}
            elif self.turns == 2:
                block = {"type": "tool_use", "id": "validate", "name": "validate", "input": plan()}
            else:
                return ModelReply([{"type": "text", "text": "All observed obligations are repaired."}], "end_turn", {}, 0, 3)
            return ModelReply([block], "tool_use", {}, 0, 3)
    repair_loop(controller, ScriptedModel())
    assert len(controller.accepted) == 1
    assert controller.accepted[0]["full_local_pass"]
    assert "OWNERSHIP_BROKEN = False" in (controller.sources.live / "src/service.py").read_text()


def test_compaction_keeps_native_tool_use_and_result_together(controller):
    messages = [{"role": "user", "content": "initial"}]
    for number in range(6):
        messages += [{"role": "assistant", "content": [{"type": "tool_use", "id": str(number), "name": "status", "input": {}}]},
                     {"role": "user", "content": [{"type": "tool_result", "tool_use_id": str(number), "content": "result"}]}]
    compacted = compact_messages(controller, messages, force=True)
    assert compacted[0]["role"] == "user"
    for index, message in enumerate(compacted):
        if message["role"] == "assistant":
            tool_id = message["content"][0]["id"]
            assert compacted[index + 1]["content"][0]["tool_use_id"] == tool_id


def test_compaction_enforces_total_serialized_bound(controller):
    from fixer.main import MAX_CONTEXT_CHARS
    controller.initial_context = "i" * 160000
    messages = [{"role": "user", "content": controller.initial_context}]
    for number in range(6):
        messages += [{"role": "assistant", "content": [{"type": "tool_use", "id": str(number), "name": "status", "input": {}}]},
                     {"role": "user", "content": [{"type": "tool_result", "tool_use_id": str(number), "content": "r" * 32000}]}]
    for force in (False, True):
        compacted = compact_messages(controller, messages, force=force)
        assert len(json.dumps(compacted, ensure_ascii=False)) <= MAX_CONTEXT_CHARS


@pytest.mark.parametrize("full_repair", [True, False])
def test_compaction_retains_pending_diagnosis_review_gap_and_source_bound_results(controller, full_repair):
    from fixer.main import MAX_CONTEXT_CHARS

    mechanism = "Late retries must use the winning durable operation identity; equal output does not establish ownership."
    evidence = "service/Unseen/design.md: retries retain the recorded owner identity after recovery."
    gap = "Replay probe:restart-after-commit against accepted and candidate source before accepting the repair."
    original = controller.sources.best_hash
    controller.record_hypothesis({"id": "durable-ownership", "obligation": "A completed operation has one durable owner.",
                                  "mechanism": mechanism, "evidence": [evidence], "status": "proposed"})
    patch = final_patch(controller)
    if not full_repair:
        patch["edits"] = patch["edits"][:1]
    controller.sources.apply_edits(**patch)
    candidate = controller.sources.candidate_hash

    class NeedsRestartEvidence(Reviewer):
        def messages(self, *args, **kwargs):
            verdict = {"approve": False, "issues": [], "missing_evidence": [gap]}
            return ModelReply([{"type": "text", "text": json.dumps(verdict)}], "end_turn", {}, 0, 3)

    controller.model = NeedsRestartEvidence()
    rejected = controller.validate(plan())
    assert not rejected["accepted"] and controller.sources.best_hash == original
    controller.initial_context = "Original incident context.\n" + "i" * 160000
    messages = [{"role": "user", "content": controller.initial_context},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "old-validation",
                                                       "name": "validate", "input": plan()}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "old-validation",
                                                 "content": json.dumps(rejected)}]}]
    for number in range(6):
        call_id = "read-" + str(number)
        messages += [{"role": "assistant", "content": [{"type": "tool_use", "id": call_id,
                                                           "name": "read_file", "input": {"path": "src/service.py"}}]},
                     {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id,
                                                      "content": "unrelated file range " + "r" * 32000}]}]
    for force in (False, True):
        compacted = compact_messages(controller, messages, force=force)
        assert len(json.dumps(compacted, ensure_ascii=False)) <= MAX_CONTEXT_CHARS
        retained_ids = []
        for index, message in enumerate(compacted):
            if message["role"] == "assistant":
                ids = [block["id"] for block in message["content"] if block["type"] == "tool_use"]
                following = [block["tool_use_id"] for block in compacted[index + 1]["content"]
                             if block["type"] == "tool_result"]
                assert ids == following
                retained_ids.extend(ids)
        assert "old-validation" not in retained_ids
        content = compacted[-1]["content"]
        ledger_text = content[-1]["text"] if isinstance(content, list) else content
        ledger = json.loads(ledger_text.split("# Current repair ledger\n", 1)[1])
        hypothesis = ledger["hypotheses"][0]
        assert hypothesis["source_hash"] == original and hypothesis["mechanism"] == mechanism
        assert evidence in hypothesis["evidence"]
        assert ledger["known_failures_source"] == "accepted"
        assert ledger["known_failures_source_hash"] == original
        assert ledger["known_failures"] and all(row["snapshot"] == "accepted" and row["source_hash"] == original
                                                for row in ledger["known_failures"])
        assert ledger["candidate_results"] and all(row["source_hash"] == candidate for row in ledger["candidate_results"])
        if full_repair:
            assert all(row["status"] == "pass" for row in ledger["candidate_results"])
        else:
            assert any("RECOVERY" in row["invariants"] for row in ledger["candidate_results"])
        validation = ledger["last_validation"]
        assert validation["source_hash"] == candidate and not validation["accepted"]
        assert validation["reason"] == rejected["reason"]
        assert gap in validation["review"]["missing_evidence"]
        assert validation["matrix"]["complete"] and validation["matrix"]["full_pass"] == full_repair
        assert "OWNERSHIP" in validation["closed_invariants"]
        assert (validation["witness_count"] > 0) == full_repair
        assert validation["matrix"]["after"] and all(row["source_hash"] == candidate for row in validation["matrix"]["after"])
        if full_repair:
            assert all(row["status"] == "pass" for row in validation["matrix"]["after"])
        assert "OWNERSHIP_BROKEN = False" in ledger["pending_diff"]


def test_compaction_bounds_escaped_hypothesis_metadata_without_losing_source_identity(controller):
    from fixer.main import MAX_CONTEXT_CHARS, MAX_LEDGER_CHARS

    mechanism = "Persist the operation identity before retrying. "
    for number in range(6):
        controller.record_hypothesis({"id": "obligation-" + str(number), "status": "proposed",
                                      "obligation": "Preserve durable ownership. " + "\x01" * 1900,
                                      "mechanism": mechanism + "\x01" * 3900,
                                      "evidence": ["service/Unseen/design.md " + "\x01" * 2900] * 20})
    text = prompt_status(controller)
    assert len(text) <= MAX_LEDGER_CHARS
    ledger = json.loads(text)
    assert len(ledger["hypotheses"]) == 6
    assert all(row["source_hash"] == controller.sources.best_hash and row["mechanism"].startswith(mechanism)
               for row in ledger["hypotheses"])
    controller.initial_context = "i" * 160000
    compacted = compact_messages(controller, [{"role": "user", "content": controller.initial_context}], force=True)
    assert len(json.dumps(compacted, ensure_ascii=False)) <= MAX_CONTEXT_CHARS


@pytest.mark.parametrize("force_minimal", [False, True])
def test_prompt_ledger_terminates_with_enormous_integer_seeds(controller, monkeypatch, force_minimal):
    import threading
    import fixer.main as main

    controller.sources.apply_edits(**final_patch(controller))
    status = controller.status()
    candidate = status["source_hash"]
    original = status["accepted_source_hash"]
    enormous_seed = 10 ** 3999
    gap = "Run the restart control before accepting this candidate."
    status["public_seeds"] = [enormous_seed] * 16
    status["known_failures"] = [{"source_hash": original, "scenario": "public", "seed": enormous_seed,
                                 "status": "invariant_failure", "violations": [{"invariant": "OWNERSHIP"}]}] * 6
    status["candidate_results"] = [{"source_hash": candidate, "scenario": "public", "seed": enormous_seed,
                                    "status": "pass", "violations": []},
                                   {"source_hash": candidate, "scenario": "public", "seed": 7,
                                    "status": "pass", "violations": []}]
    status["last_validation"] = {"source_hash": candidate, "accepted": False, "complete": True,
                                 "stage": "review", "reason": "review requires a local control",
                                 "determinism": {"complete": False, "ok": None,
                                                 "failed_experiment": {"source_hash": candidate, "scenario": "public",
                                                                       "seed": enormous_seed, "faults": "scenarios/restart.yaml",
                                                                       "ok": False, "run_ids": ["first", "second"]}},
                                 "review": {"approve": False, "issues": [], "missing_evidence": [gap]}}
    monkeypatch.setattr(controller, "status", lambda: status)
    if force_minimal:
        monkeypatch.setattr(main, "MAX_LEDGER_CHARS", 1300)
    output = []
    worker = threading.Thread(target=lambda: output.append(prompt_status(controller)), daemon=True)
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive(), "ledger adaptation did not terminate"
    assert output and len(output[0]) <= main.MAX_LEDGER_CHARS
    ledger = json.loads(output[0])
    assert ledger["details_truncated"] and ledger["source_hash"] == candidate
    assert ledger["last_validation"]["source_hash"] == candidate
    assert not ledger["last_validation"]["accepted"] and gap in ledger["last_validation"]["review"]["missing_evidence"]
    if not force_minimal:
        assert all(isinstance(seed, str) and "integer omitted" in seed for seed in ledger["public_seeds"])
        assert ledger["candidate_results"][-1]["seed"] == 7
        deterministic = ledger["last_validation"]["determinism"]
        assert deterministic["complete"] is False and deterministic["ok"] is None
        assert deterministic["failed_experiment"]["run_ids"] == ["first", "second"]
    assert status["public_seeds"] == [enormous_seed] * 16
    controller.initial_context = "i" * 160000
    compacted = compact_messages(controller, [{"role": "user", "content": controller.initial_context}], force=True)
    assert len(json.dumps(compacted, ensure_ascii=False)) <= main.MAX_CONTEXT_CHARS
    if not force_minimal:
        compacted_ledger = json.loads(compacted[-1]["content"].split("# Current repair ledger\n", 1)[1])
        failed = compacted_ledger["last_validation"]["determinism"]["failed_experiment"]
        assert failed["source_hash"] == candidate and failed["scenario"] == "public"
        assert failed["faults"] == "scenarios/restart.yaml" and failed["ok"] is False
        assert failed["run_ids"] == ["first", "second"]


def test_budget_recovery_remains_available_after_incomplete_response(controller):
    from fixer.model import BudgetExhausted, ModelError
    class RecoveringModel(Reviewer):
        limits = []
        def messages(self, *args, **kwargs):
            self.limits.append(kwargs["max_tokens"])
            if len(self.limits) == 1:
                raise ModelError("incomplete", code="incomplete_response")
            if len(self.limits) == 2:
                raise BudgetExhausted(.3, .2)
            controller.deadline.stop_event.set()
            return ModelReply([{"type": "text", "text": "Finished."}], "end_turn", {}, 0, 3)
    model = RecoveringModel()
    repair_loop(controller, model)
    assert model.limits[:3] == [4096, 8192, 2048]


def test_pending_native_tools_stop_after_cancellation(controller, monkeypatch):
    from fixer.tools import ToolDispatcher
    seen = []
    def execute(self, name, args):
        seen.append(name)
        controller.deadline.stop_event.set()
        return {"ok": True, "result": {}}
    monkeypatch.setattr(ToolDispatcher, "execute", execute)
    class CancellingModel(Reviewer):
        def messages(self, *args, **kwargs):
            return ModelReply([{"type": "tool_use", "id": str(n), "name": "status", "input": {}} for n in range(3)],
                              "tool_use", {}, 0, 3)
    repair_loop(controller, CancellingModel())
    assert seen == ["status"]


def final_patch(controller):
    return {"base_hash": controller.sources.candidate_hash, "edits": [
        {"path": "src/service.py", "old": "OWNERSHIP_BROKEN = True", "new": "OWNERSHIP_BROKEN = False"},
        {"path": "src/service.py", "old": "RECOVERY_BROKEN = True", "new": "RECOVERY_BROKEN = False"}]}


@pytest.mark.parametrize("termination", ["budget", "turn_bound"])
def test_final_validation_promotes_full_passing_candidate_without_model_review(controller, monkeypatch, termination):
    if termination == "turn_bound":
        monkeypatch.setattr("fixer.main.MAX_TURNS", 1)

    class LimitedModel(Reviewer):
        turns = 0
        remaining_budget = .02

        def messages(self, system, messages, tools, **kwargs):
            if tools:
                self.turns += 1
                if self.turns == 1:
                    block = {"type": "tool_use", "id": "final-edit", "name": "apply_edits",
                             "input": final_patch(controller)}
                    return ModelReply([block], "tool_use", {}, 0, .02)
            raise BudgetExhausted(.1, .02)

    model = LimitedModel()
    original = controller.sources.best_hash
    repair_loop(controller, model)
    candidate = controller.sources.candidate_hash
    assert candidate != original and source_hash(controller.sources.live / "src") == original
    result = validate_pending_candidate(controller)
    assert result["accepted"] and result["full_local_pass"]
    assert result["review"]["skipped"]
    assert source_hash(controller.sources.live / "src") == candidate == controller.sources.best_hash
    assert controller.finalize()["verified"]


def test_final_validation_keeps_partial_candidate_out_when_review_is_unavailable(controller):
    from controller_support import repair_ownership

    class UnavailableModel(Reviewer):
        def messages(self, *args, **kwargs):
            raise BudgetExhausted(.1, .02)

    controller.model = UnavailableModel()
    original = controller.sources.best_hash
    repair_ownership(controller)
    result = validate_pending_candidate(controller)
    assert not result["accepted"] and result["review"]["skipped"]
    assert not controller.accepted
    assert source_hash(controller.sources.live / "src") == original


def test_final_validation_does_not_bypass_a_prior_unchanged_review_rejection(controller):
    class RejectingThenUnavailableModel(Reviewer):
        turns = 0
        review_calls = 0

        def messages(self, system, messages, tools, **kwargs):
            if not tools:
                self.review_calls += 1
                if self.review_calls == 1:
                    verdict = {"approve": False, "issues": ["external operation identity still changes on retry"],
                               "missing_evidence": []}
                    return ModelReply([{"type": "text", "text": json.dumps(verdict)}], "end_turn", {}, 0, 3)
                raise ModelError("upstream payment unavailable", code="payment_error")
            self.turns += 1
            if self.turns == 1:
                block = {"type": "tool_use", "id": "edit", "name": "apply_edits", "input": final_patch(controller)}
            elif self.turns == 2:
                block = {"type": "tool_use", "id": "validate", "name": "validate", "input": plan()}
            else:
                raise ModelError("upstream payment unavailable", code="payment_error")
            return ModelReply([block], "tool_use", {}, 0, 3)

    original = controller.sources.best_hash
    model = RejectingThenUnavailableModel()
    repair_loop(controller, model)
    assert model.review_calls == 1 and controller.sources.candidate_hash != original
    assert validate_pending_candidate(controller) is None
    assert model.review_calls == 1 and not controller.accepted
    assert source_hash(controller.sources.live / "src") == original


def test_end_turn_does_not_repeat_rejected_source_review_when_review_becomes_unavailable(controller):
    defect = "External operation identity still changes on retry."

    class RejectingThenUnavailableModel(Reviewer):
        turns = 0
        review_calls = 0
        final_feedback = ""

        def messages(self, system, messages, tools, **kwargs):
            if not tools:
                self.review_calls += 1
                if self.review_calls == 1:
                    verdict = {"approve": False, "issues": [defect], "missing_evidence": []}
                    return ModelReply([{"type": "text", "text": json.dumps(verdict)}], "end_turn", {}, 0, 3)
                raise BudgetExhausted(.1, .02)
            self.turns += 1
            if self.turns == 1:
                block = {"type": "tool_use", "id": "edit", "name": "apply_edits", "input": final_patch(controller)}
            elif self.turns == 2:
                block = {"type": "tool_use", "id": "validate", "name": "validate", "input": plan()}
            elif self.turns == 3:
                return ModelReply([{"type": "text", "text": "The checks are complete."}], "end_turn", {}, 0, 3)
            else:
                self.final_feedback = messages[-1]["content"]
                raise ModelError("upstream payment unavailable", code="upstream_payment_error")
            return ModelReply([block], "tool_use", {}, 0, 3)

    original = controller.sources.best_hash
    model = RejectingThenUnavailableModel()
    repair_loop(controller, model)
    assert model.review_calls == 1 and not controller.accepted
    assert defect in model.final_feedback and controller.sources.candidate_hash in model.final_feedback
    assert validate_pending_candidate(controller) is None
    assert model.review_calls == 1 and source_hash(controller.sources.live / "src") == original


def test_explicit_validation_can_revisit_same_source_after_new_reproduction_evidence(controller):
    class RequestsExtraEvidenceModel(Reviewer):
        turns = 0
        review_calls = 0

        def messages(self, system, messages, tools, **kwargs):
            if not tools:
                self.review_calls += 1
                verdict = {"approve": self.review_calls > 1, "issues": [],
                           "missing_evidence": ["Reproduce public seed 99 on original and candidate."] if self.review_calls == 1 else []}
                return ModelReply([{"type": "text", "text": json.dumps(verdict)}], "end_turn", {}, 0, 3)
            self.turns += 1
            if self.turns == 1:
                blocks = [{"type": "tool_use", "id": "edit", "name": "apply_edits", "input": final_patch(controller)}]
            elif self.turns == 2:
                blocks = [{"type": "tool_use", "id": "validate-first", "name": "validate", "input": plan()}]
            elif self.turns == 3:
                blocks = [{"type": "tool_use", "id": "original-repro", "name": "replay", "input": {"source": "original", "seed": 99}},
                          {"type": "tool_use", "id": "candidate-repro", "name": "replay", "input": {"source": "candidate", "seed": 99}}]
            elif self.turns == 4:
                revised = {**plan(), "reproductions": [{"scenario": "public", "seed": 99}]}
                blocks = [{"type": "tool_use", "id": "validate-with-evidence", "name": "validate", "input": revised}]
            else:
                return ModelReply([{"type": "text", "text": "The repair is now accepted."}], "end_turn", {}, 0, 3)
            return ModelReply(blocks, "tool_use", {}, 0, 3)

    model = RequestsExtraEvidenceModel()
    repair_loop(controller, model)
    assert model.review_calls == 2 and len(controller.accepted) == 1
    assert controller.accepted[0]["reproductions"] == [{"scenario": "public", "seed": 99, "faults": None}]
    assert source_hash(controller.sources.live / "src") == controller.sources.candidate_hash == controller.sources.best_hash


def test_final_validation_skips_cancelled_pending_candidate(controller, monkeypatch):
    controller.sources.apply_edits(**final_patch(controller))
    original = controller.sources.best_hash
    controller.deadline.stop_event.set()
    monkeypatch.setattr(controller, "validate", lambda _plan: pytest.fail("validation ran after cancellation"))
    assert validate_pending_candidate(controller) is None
    assert source_hash(controller.sources.live / "src") == original


def test_final_validation_skips_when_installation_reserve_cannot_fit(controller, monkeypatch):
    controller.sources.apply_edits(**final_patch(controller))
    original = controller.sources.best_hash
    monkeypatch.setattr(controller.deadline, "_end", time.monotonic() + 1)
    monkeypatch.setattr(controller, "validate", lambda _plan: pytest.fail("validation exhausted installation reserve"))
    assert validate_pending_candidate(controller) is None
    assert source_hash(controller.sources.live / "src") == original
