from __future__ import annotations

import json
import time

import pytest

from fixer.main import compact_messages, prompt_status, repair_loop, validate_pending_candidate
from fixer.model import BudgetExhausted, ModelError, ModelReply
from fixer.patches import source_hash
from controller_support import Reviewer, controller, plan, review_reply


def is_review(tools):
    return any(tool.get("name") == "submit_review" for tool in tools)


def test_tool_loop_applies_and_validates_a_native_model_patch(controller):
    class ScriptedModel(Reviewer):
        turns = 0
        def messages(self, system, messages, tools, **kwargs):
            if is_review(tools):
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


def test_compaction_shrinks_static_context_before_losing_latest_native_finding(controller):
    from fixer.main import COMPACT_CONTEXT_CHARS, MAX_CONTEXT_CHARS

    current = controller.sources.candidate_hash
    orientation = "Initial current facts: durable state belongs to the service. Source=" + current + "\n"
    controller.initial_context = orientation + "i" * 90000
    messages = [{"role": "user", "content": controller.initial_context}]
    for number in range(2):
        messages += [{"role": "assistant", "content": [{"type": "tool_use", "id": "old-" + str(number),
                                                       "name": "read_file", "input": {"path": "src/service.py"}}]},
                     {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "old-" + str(number),
                                                   "content": "old observation " + "o" * 30000}]}]
    finding = "The earlier enqueue theory was falsified; recovery omitted the durable marker."
    latest = [{"role": "assistant", "content": [{"type": "text", "text": finding}, *[
                {"type": "tool_use", "id": "current-" + str(number), "name": "read_file",
                 "input": {"path": "src/service.py"}} for number in range(2)]]},
              {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "current-" + str(number),
               "content": "current file evidence " + "r" * 26000 + " evidence end " + str(number)} for number in range(2)]}]
    messages.extend(latest)
    assert len(json.dumps(messages, ensure_ascii=False)) > MAX_CONTEXT_CHARS
    # Static orientation plus this latest pair exceeds the soft target, while
    # the complete pair and current ledger comfortably fit the hard cap.
    assert len(controller.initial_context) + len(json.dumps(latest)) > COMPACT_CONTEXT_CHARS
    compacted = compact_messages(controller, messages)
    assert_native_pairs(compacted)
    assert len(json.dumps(compacted, ensure_ascii=False)) <= COMPACT_CONTEXT_CHARS
    assert compacted[0]["content"].startswith(orientation)
    assert "initial context truncated" in compacted[0]["content"]
    assert compacted[1] == latest[0]
    results = [block for block in compacted[2]["content"] if block["type"] == "tool_result"]
    assert results == latest[1]["content"]
    context = json.dumps(compacted, ensure_ascii=False)
    assert finding in context and current in context and "known_failures_source_hash" in context


@pytest.mark.parametrize("force", [False, True])
def test_compaction_keeps_latest_pair_above_soft_target_and_adapts_next_turn(controller, force):
    from fixer.main import COMPACT_CONTEXT_CHARS, MAX_CONTEXT_CHARS

    orientation = "Current contract and source identity: " + controller.sources.candidate_hash + "\n"
    controller.initial_context = orientation + "i" * 90000
    finding = "The latest measured facts replace the previous causal explanation."
    latest = [{"role": "assistant", "content": [{"type": "text", "text": finding}, *[
                {"type": "tool_use", "id": "latest-" + str(number), "name": "trace_slice",
                 "input": {"run_id": "current-run", "entities": ["job"]}} for number in range(5)]]},
              {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "latest-" + str(number),
               "content": "complete current trace " + "r" * 26000 + " trace end " + str(number)} for number in range(5)]}]
    messages = [{"role": "user", "content": controller.initial_context}, *latest]
    assert len(json.dumps(latest)) > COMPACT_CONTEXT_CHARS
    compacted = compact_messages(controller, messages, force=force)
    assert_native_pairs(compacted)
    size = len(json.dumps(compacted, ensure_ascii=False))
    assert COMPACT_CONTEXT_CHARS < size <= MAX_CONTEXT_CHARS
    assert compacted[0]["content"].startswith(orientation)
    assert compacted[1] == latest[0]
    assert [block for block in compacted[2]["content"] if block["type"] == "tool_result"] == latest[1]["content"]
    next_pair = [{"role": "assistant", "content": [{"type": "text", "text": "The next tool confirms the updated finding."},
                  {"type": "tool_use", "id": "next-current", "name": "read_file", "input": {"path": "src/service.py"}}]},
                 {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "next-current",
                  "content": "next current source " + "s" * 31900 + " next source end"}]}]
    grown = compacted + next_pair
    assert len(json.dumps(grown, ensure_ascii=False)) > MAX_CONTEXT_CHARS
    adapted = compact_messages(controller, grown)
    assert_native_pairs(adapted)
    assert len(json.dumps(adapted, ensure_ascii=False)) <= MAX_CONTEXT_CHARS
    assert adapted[0]["content"].startswith(orientation)
    assert adapted[-2] == next_pair[0]
    assert [block for block in adapted[-1]["content"] if block["type"] == "tool_result"] == next_pair[1]["content"]


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
        def messages(self, system, messages, tools, **kwargs):
            verdict = {"approve": False, "issues": [], "missing_evidence": [gap]}
            return review_reply(verdict, tools)

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


def test_prompt_ledger_keeps_an_older_id_updated_for_current_source(controller):
    controller.record_hypothesis({"id": "active-identity", "obligation": "One durable operation identity.",
                                  "mechanism": "Original diagnosis.", "evidence": ["original trace"], "status": "open"})
    for number in range(7):
        controller.record_hypothesis({"id": "other-" + str(number), "obligation": "Other obligation.",
                                      "mechanism": "Earlier investigation.", "evidence": ["earlier source"], "status": "open"})
    controller.sources.apply_edits(**final_patch(controller))
    mechanism = "Current source preserves the winning operation identity on retries."
    controller.record_hypothesis({"id": "active-identity", "obligation": "One durable operation identity.",
                                  "mechanism": mechanism, "evidence": ["src/service.py:8"], "status": "proposed"})
    ledger = json.loads(prompt_status(controller))
    assert len(ledger["hypotheses"]) == 6
    active = next(row for row in ledger["hypotheses"] if row["id"] == "active-identity")
    assert active["source_hash"] == controller.sources.candidate_hash and active["mechanism"] == mechanism


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
            if not is_review(tools):
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
            if is_review(tools):
                self.review_calls += 1
                if self.review_calls == 1:
                    verdict = {"approve": False, "issues": ["external operation identity still changes on retry"],
                               "missing_evidence": []}
                    return review_reply(verdict, tools)
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
            if is_review(tools):
                self.review_calls += 1
                if self.review_calls == 1:
                    verdict = {"approve": False, "issues": [defect], "missing_evidence": []}
                    return review_reply(verdict, tools)
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
            if is_review(tools):
                self.review_calls += 1
                verdict = {"approve": self.review_calls > 1, "issues": [],
                           "missing_evidence": ["Reproduce public seed 99 on original and candidate."] if self.review_calls == 1 else []}
                return review_reply(verdict, tools)
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


def log_events(capsys):
    captured = capsys.readouterr()
    rows = []
    for line in (captured.out + "\n" + captured.err).splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def assert_native_pairs(messages):
    for index, message in enumerate(messages):
        if message["role"] != "assistant" or not isinstance(message["content"], list):
            continue
        ids = [block["id"] for block in message["content"] if block["type"] == "tool_use"]
        if ids:
            assert index + 1 < len(messages) and messages[index + 1]["role"] == "user"
            following = [block["tool_use_id"] for block in messages[index + 1]["content"]
                         if block["type"] == "tool_result"]
            assert ids == following


def test_repair_loop_continues_past_32_turns_with_resources(controller, capsys):
    controller.record_hypothesis({"id": "ownership", "obligation": "One durable owner.",
                                  "mechanism": "Retry must retain the recorded operation identity.",
                                  "evidence": ["src/service.py: ownership flag"], "status": "open"})

    class ReadingModel(Reviewer):
        turns = 0

        def messages(self, system, messages, tools, **kwargs):
            self.turns += 1
            assert_native_pairs(messages)
            if self.turns > 34:
                return ModelReply([{"type": "text", "text": "No further supported diagnosis."}], "end_turn", {}, 0, 3)
            return ModelReply([{"type": "tool_use", "id": str(self.turns), "name": "status", "input": {}}],
                              "tool_use", {}, 0, 3)

    model = ReadingModel()
    repair_loop(controller, model)
    assert model.turns == 37
    assert controller.deadline.remaining(controller.final_reserve) > 30
    stopped = [row for row in log_events(capsys) if row["event"] == "repair_loop_stopped"][-1]
    assert stopped["reason"] == "completion_limit" and stopped["model_turns"] == 37


def test_stalled_reads_force_one_durable_hypothesis_that_survives_compaction(controller, monkeypatch, capsys):
    from fixer.tools import ToolDispatcher

    original_execute = ToolDispatcher.execute

    def execute(self, name, args):
        if name == "read_file":
            return {"ok": True, "result": {"source_hash": controller.sources.candidate_hash,
                                           "content": "numbered source excerpt " + "r" * 31000}}
        return original_execute(self, name, args)

    monkeypatch.setattr(ToolDispatcher, "execute", execute)
    controller.initial_context = "incident orientation\n" + "i" * 160000
    mechanism = "The retry changes durable operation identity after the commit."
    evidence = "src/service.py:8, original public trace, winning operation identity."

    class ReadingModel(Reviewer):
        turns = 0
        forced = 0
        final_context = ""

        def messages(self, system, messages, tools, **kwargs):
            self.turns += 1
            assert_native_pairs(messages)
            if kwargs.get("tool_choice"):
                self.forced += 1
                assert kwargs["tool_choice"] == {"type": "tool", "name": "record_hypothesis"}
                assert kwargs["max_tokens"] == 2048
                forced_context = json.dumps(messages, ensure_ascii=False)
                assert "# Preserve the current finding" in forced_context
                assert "label unknowns explicitly" in forced_context
                block = {"type": "tool_use", "id": "remember", "name": "record_hypothesis", "input": {
                    "id": "durable-identity", "obligation": "An operation has one durable identity.",
                    "mechanism": mechanism, "evidence": [evidence], "status": "open"}}
            elif self.turns >= 16:
                self.final_context = json.dumps(messages, ensure_ascii=False)
                raise ModelError("script complete", code="script_complete")
            else:
                block = {"type": "tool_use", "id": "read-" + str(self.turns),
                         "name": "read_file", "input": {"path": "src/service.py"}}
            return ModelReply([block], "tool_use", {}, 0, 3)

    model = ReadingModel()
    repair_loop(controller, model)
    assert model.forced == 1 and controller.hypotheses["durable-identity"]["mechanism"] == mechanism
    assert mechanism in model.final_context and evidence in model.final_context
    assert controller.sources.candidate_hash in model.final_context
    events = log_events(capsys)
    remembered = next(index for index, row in enumerate(events) if row["event"] == "hypothesis")
    assert any(row["event"] == "context_compacted" for row in events[remembered + 1:])


def test_stalled_candidate_gets_actionable_review_before_more_model_work(controller):
    gap = "Replay original and candidate public seed 99 before accepting durable identity."

    class FeedbackModel(Reviewer):
        turns = 0
        review_calls = 0
        received_gap = False

        def messages(self, system, messages, tools, **kwargs):
            if is_review(tools):
                self.review_calls += 1
                return review_reply({"approve": self.review_calls > 1, "issues": [],
                                     "missing_evidence": [gap] if self.review_calls == 1 else []}, tools)
            self.turns += 1
            assert_native_pairs(messages)
            if self.turns == 1:
                blocks = [{"type": "tool_use", "id": "edit", "name": "apply_edits",
                           "input": final_patch(controller)}]
            elif self.turns <= 7:
                blocks = [{"type": "tool_use", "id": "read-" + str(self.turns),
                           "name": "read_file", "input": {"path": "src/service.py"}}]
            elif self.turns == 8:
                context = json.dumps(messages, ensure_ascii=False)
                self.received_gap = gap in context and controller.sources.candidate_hash in context
                assert self.review_calls == 1 and self.received_gap
                assert "tool_choice" not in kwargs, "validation should take priority over forcing a hypothesis"
                blocks = [{"type": "tool_use", "id": "original-repro", "name": "replay",
                           "input": {"source": "original", "seed": 99}},
                          {"type": "tool_use", "id": "candidate-repro", "name": "replay",
                           "input": {"source": "candidate", "seed": 99}}]
            elif self.turns == 9:
                blocks = [{"type": "tool_use", "id": "validate-with-new-evidence", "name": "validate",
                           "input": {**plan(), "reproductions": [{"scenario": "public", "seed": 99}]}}]
            else:
                return ModelReply([{"type": "text", "text": "The local repair is accepted."}], "end_turn", {}, 0, 3)
            return ModelReply(blocks, "tool_use", {}, 0, 3)

    model = FeedbackModel()
    repair_loop(controller, model)
    assert model.received_gap and model.review_calls == 2 and len(controller.accepted) == 1
    assert source_hash(controller.sources.live / "src") == controller.sources.candidate_hash
    assert controller.deadline.remaining(controller.final_reserve) > 30


@pytest.mark.parametrize("failure", ["budget", "incomplete"])
def test_stalled_hypothesis_keeps_forced_decision_after_recoverable_request_failure(controller, capsys, failure):
    class RecoveringModel(Reviewer):
        turns = 0
        forced_requests = 0

        def messages(self, system, messages, tools, **kwargs):
            self.turns += 1
            assert_native_pairs(messages)
            if kwargs.get("tool_choice"):
                self.forced_requests += 1
                assert kwargs["max_tokens"] == 2048
                assert kwargs["tool_choice"] == {"type": "tool", "name": "record_hypothesis"}
                assert "# Preserve the current finding" in json.dumps(messages)
                if self.forced_requests == 1:
                    if failure == "budget":
                        raise BudgetExhausted(.1, .09)
                    raise ModelError("response truncated", code="incomplete_response")
                return ModelReply([{"type": "tool_use", "id": "remember-after-retry", "name": "record_hypothesis",
                                    "input": {"id": "retry-identity", "obligation": "One durable identity.",
                                              "mechanism": "Retry must preserve the committed operation identity.",
                                              "evidence": ["src/service.py:8"], "status": "open"}}], "tool_use", {}, 0, 3)
            if self.forced_requests:
                return ModelReply([{"type": "text", "text": "No further supported findings."}], "end_turn", {}, 0, 3)
            return ModelReply([{"type": "tool_use", "id": "read-" + str(self.turns), "name": "read_file",
                                "input": {"path": "src/service.py"}}], "tool_use", {}, 0, 3)

    model = RecoveringModel()
    repair_loop(controller, model)
    assert model.forced_requests == 2
    assert controller.hypotheses["retry-identity"]["source_hash"] == controller.sources.candidate_hash
    stopped = [row for row in log_events(capsys) if row["event"] == "repair_loop_stopped"][-1]
    assert stopped["forced_hypothesis_attempts"] == 2 and stopped["forced_hypotheses"] == 1


def test_stalled_unchanged_rejection_is_never_automatically_retried(controller):
    controller.sources.apply_edits(**final_patch(controller))
    controller.record_hypothesis({"id": "identity", "obligation": "Stable retry identity.",
                                  "mechanism": "A retry must use its committed identity.",
                                  "evidence": ["src/service.py:8"], "status": "proposed"})
    original = controller.sources.best_hash

    class RejectedModel(Reviewer):
        review_calls = 0
        turns = 0

        def messages(self, system, messages, tools, **kwargs):
            if is_review(tools):
                self.review_calls += 1
                if self.review_calls > 1:
                    raise BudgetExhausted(.1, .02)
                return review_reply({"approve": False, "issues": ["Retry identity still changes."],
                                     "missing_evidence": []}, tools)
            self.turns += 1
            if self.turns > 12:
                return ModelReply([{"type": "text", "text": "The source has not changed."}], "end_turn", {}, 0, 3)
            return ModelReply([{"type": "tool_use", "id": str(self.turns), "name": "read_file",
                                "input": {"path": "src/service.py"}}], "tool_use", {}, 0, 3)

    model = RejectedModel()
    controller.model = model
    assert not controller.validate(plan())["accepted"]
    repair_loop(controller, model)
    assert model.review_calls == 1 and not controller.accepted
    assert validate_pending_candidate(controller) is None
    assert model.review_calls == 1 and source_hash(controller.sources.live / "src") == original


def rejected_probe_candidate(controller):
    from fixer.runtime import Deadline

    controller.deadline = controller.runner.deadline = Deadline(time.time() + 360)
    controller.sources.apply_edits(**final_patch(controller))
    controller.model = Reviewer(False)
    assert not controller.validate(plan())["accepted"]
    controller.record_hypothesis({"id": "recorded-but-unproven", "obligation": "One durable identity.",
                                  "mechanism": "The source explanation still needs a discriminating experiment.",
                                  "evidence": ["src/service.py:1"], "status": "proposed"})
    return controller.sources.candidate_hash, controller.sources.best_hash


def probe_reply(number=1, **arguments):
    args = {"name": "diagnostic_" + str(number), "base": "public", "overrides": {"checks": ["OWNERSHIP"]}}
    args.update(arguments)
    return ModelReply([{"type": "tool_use", "id": "probe-" + str(number),
                        "name": "compose_probe", "input": args}], "tool_use", {}, 0, 3)


class DiagnosticModel(Reviewer):
    def __init__(self, forced=None):
        super().__init__(False)
        self.turns = self.forced = 0
        self.force_callback = forced
        self.final_context = ""

    def messages(self, system, messages, tools, **kwargs):
        assert not is_review(tools), "diagnostic evidence automatically retried a rejected review"
        assert_native_pairs(messages)
        self.turns += 1
        if kwargs.get("tool_choice"):
            self.forced += 1
            assert kwargs["tool_choice"] == {"type": "tool", "name": "compose_probe"}
            assert [tool["name"] for tool in tools] == ["compose_probe"]
            assert kwargs["max_tokens"] == 2048
            assert "# Produce a measured diagnostic probe" in json.dumps(messages)
            return self.force_callback(self, system, messages, tools, kwargs) if self.force_callback else probe_reply(self.forced)
        if self.forced or self.turns > 8:
            self.final_context = json.dumps(messages, ensure_ascii=False)
            return ModelReply([{"type": "text", "text": "The rejected source has not changed."}], "end_turn", {}, 0, 3)
        return ModelReply([{"type": "tool_use", "id": "read-" + str(self.turns), "name": "read_file",
                            "input": {"path": "src/service.py"}}], "tool_use", {}, 0, 3)


def test_rejected_stalled_source_gets_measured_pair_with_native_wire_and_compaction(controller, monkeypatch):
    from fixer.main import MAX_CONTEXT_CHARS
    from fixer.model import ModelClient

    candidate, accepted = rejected_probe_candidate(controller)
    rejection = controller.status()["last_validation"]
    client = ModelClient(controller.runner, controller.deadline, 3, env={
        "ANTHROPIC_API_KEY": "offline-test-key", "ANTHROPIC_BASE_URL": "http://127.0.0.1:1",
        "FIXER_THINKING_TOKENS": "4096"})
    wire = []

    def http(method, url, body=None, **kwargs):
        if method == "GET":
            return {"status": 404, "body": {}}
        wire.append(body)
        assert body["max_tokens"] == 2048 and "thinking" not in body
        assert body["tool_choice"] == {"type": "tool", "name": "compose_probe"}
        assert [tool["name"] for tool in body["tools"]] == ["compose_probe"]
        return {"status": 200, "complete": True, "body": {"type": "message", "role": "assistant",
                "content": probe_reply().content, "stop_reason": "tool_use",
                "usage": {"input_tokens": 100, "output_tokens": 50}}}

    monkeypatch.setattr(client, "_http", http)
    model = DiagnosticModel(lambda _m, system, messages, tools, kwargs: client.messages(system, messages, tools, **kwargs))
    measured = []
    real_replay = controller.replay

    def replay(source, scenario, seed, **kwargs):
        measured.append((source, scenario, seed, kwargs["reserve"]))
        return real_replay(source, scenario, seed, **kwargs)

    monkeypatch.setattr(controller, "replay", replay)
    repair_loop(controller, model)
    assert model.forced == len(wire) == 1
    assert [row[0] for row in measured] == ["accepted", "candidate"]
    assert measured[0][1:3] == measured[1][1:3] == ("probe:diagnostic_1", controller.config.seeds[0])
    assert measured[0][3] > measured[1][3] == controller.final_reserve + 30
    pair = controller._last_probe_intervention
    assert pair["complete"] and pair["scope"] == "local_diagnostic"
    assert pair["before"]["source_hash"] == accepted and pair["before"]["status"] == "invariant_failure"
    assert pair["after"]["source_hash"] == candidate and pair["after"]["status"] == "pass"
    assert pair["before"]["scenario_hash"] == pair["after"]["scenario_hash"]
    assert controller.status()["last_validation"] == rejection
    assert not controller.accepted and validate_pending_candidate(controller) is None
    assert source_hash(controller.sources.live / "src") == accepted
    messages = [{"role": "user", "content": controller.initial_context}]
    for number in range(8):
        messages += [{"role": "assistant", "content": [{"type": "tool_use", "id": str(number), "name": "status", "input": {}}]},
                     {"role": "user", "content": [{"type": "tool_result", "tool_use_id": str(number), "content": "r" * 31000}]}]
    compacted = compact_messages(controller, messages, force=True)
    assert_native_pairs(compacted)
    context = json.dumps(compacted, ensure_ascii=False)
    assert len(context) <= MAX_CONTEXT_CHARS
    assert pair["before"]["run_id"] in context and pair["after"]["run_id"] in context
    assert candidate in context and accepted in context and rejection["review"]["issues"][0] in context
    assert "probe:diagnostic_1" in context and "OWNERSHIP" in context
    coverage = json.loads(prompt_status(controller))["control_coverage"]
    assert coverage["executed"] == controller.control_coverage["executed"]
    assert coverage["retained"] == controller.control_coverage["retained"]
    assert "cancelled" in coverage and "discovery_truncated" in coverage


@pytest.mark.parametrize("change", ["name", "expect", "seed"])
def test_diagnostic_declines_metadata_only_clones_and_bounds_attempts(controller, monkeypatch, change):
    candidate, accepted = rejected_probe_candidate(controller)
    monkeypatch.setattr(controller, "replay", lambda *a, **k: pytest.fail("duplicate diagnostic was replayed"))

    def force(model, *_args):
        overrides = {} if change == "name" else {change: {"completed": 100} if change == "expect" else 123456}
        return probe_reply(model.forced, overrides=overrides)

    model = DiagnosticModel(force)
    repair_loop(controller, model)
    assert model.forced == controller._probe_intervention_attempts[candidate] == 2
    assert not controller._last_probe_intervention["complete"]
    assert "existing effective configuration" in controller._last_probe_intervention["reason"]
    assert not controller.accepted and controller.sources.best_hash == accepted


def test_novelty_includes_measured_fault_override_with_memoized_reads(controller):
    from fixer.main import _measure_probe, _probe_fingerprints
    from fixer.tools import ToolDispatcher

    candidate, accepted = rejected_probe_candidate(controller)
    for root in (controller.sources.best, controller.sources.candidate):
        (root / "scenarios/drop.yaml").write_text("faults:\n  lose_reply: true\n")
    # This combination is existing evidence, though neither file alone has
    # the same effective input as the resulting composition.
    for seed in range(1, 21):
        controller.replay("accepted", "base", seed, "scenarios/drop.yaml")
    dispatcher = ToolDispatcher(controller)
    fingerprints, reader = _probe_fingerprints(dispatcher)
    assert reader.reads[0] == 3  # public/base/drop, independent of replay count
    result = dispatcher.execute("compose_probe", {"name": "same_override", "base": "base",
                                "fault_profiles": ["drop"], "overrides": {"expect": {"completed": 999}}})
    assert result["ok"]
    pair = _measure_probe(controller, Reviewer(), dispatcher, result, fingerprints, reader, candidate, accepted, 5)
    assert not pair["complete"] and "existing effective configuration" in pair["reason"]
    assert "before" not in pair


@pytest.mark.parametrize("failure", ["budget", "incomplete", "unexpected"])
def test_forced_diagnostic_retries_remain_narrow_and_cannot_edit_or_validate(controller, failure):
    candidate, accepted = rejected_probe_candidate(controller)

    def force(model, *_args):
        if model.forced == 1:
            if failure == "budget":
                raise BudgetExhausted(.1, .09)
            if failure == "incomplete":
                raise ModelError("response interrupted", code="incomplete_response")
            return ModelReply([{"type": "tool_use", "id": "unexpected-edit", "name": "apply_edits",
                                "input": {"base_hash": candidate, "edits": [{"path": "src/service.py",
                                          "old": "NORMAL_BROKEN = False", "new": "NORMAL_BROKEN = True"}]}},
                               {"type": "tool_use", "id": "unexpected-validation", "name": "validate", "input": plan()}],
                              "tool_use", {}, 0, 3)
        return probe_reply(model.forced)

    model = DiagnosticModel(force)
    repair_loop(controller, model)
    assert model.forced == 2 and controller._last_probe_intervention["complete"]
    assert controller.sources.candidate_hash == candidate and controller.sources.best_hash == accepted
    assert controller._probe_intervention_attempts[candidate] == 2
    assert not controller.accepted and validate_pending_candidate(controller) is None


@pytest.mark.parametrize("interruption", ["cancelled", "budget", "source", "deadline"])
def test_measured_pair_stops_between_snapshots_when_resources_or_source_change(controller, monkeypatch, interruption):
    from fixer.main import _measure_probe, _probe_fingerprints
    from fixer.runtime import Deadline
    from fixer.tools import ToolDispatcher

    candidate, accepted = rejected_probe_candidate(controller)
    dispatcher = ToolDispatcher(controller)
    fingerprints, reader = _probe_fingerprints(dispatcher)
    result = dispatcher.execute("compose_probe", probe_reply().content[0]["input"])
    model = Reviewer()
    sources = []
    real_replay = controller.replay

    def replay(source, *args, **kwargs):
        sources.append(source)
        run = real_replay(source, *args, **kwargs)
        if interruption == "cancelled":
            controller.deadline.stop_event.set()
        elif interruption == "budget":
            model.remaining_budget = 0
        elif interruption == "source":
            controller.sources.apply_edits(candidate, [{"path": "src/service.py", "old": "NORMAL_BROKEN = False",
                                                        "new": "NORMAL_BROKEN = False  # external revision"}])
        else:
            controller.deadline = controller.runner.deadline = Deadline(time.time() + controller.final_reserve + 29)
        return run

    monkeypatch.setattr(controller, "replay", replay)
    pair = _measure_probe(controller, model, dispatcher, result, fingerprints, reader, candidate, accepted, 5)
    assert sources == ["accepted"] and not pair["complete"] and "after" not in pair
    assert pair["before"]["source_hash"] == accepted and pair["source_hash"] == candidate
    assert controller.sources.best_hash == accepted and not controller.accepted
    compact = json.loads(prompt_status(controller))["probe_intervention"]
    assert compact["before"]["run_id"] == pair["before"]["run_id"] and not compact["complete"]


def test_full_novelty_inventory_leaves_separate_fresh_probe_verification_budget(controller):
    from fixer.main import _measure_probe, _probe_fingerprints
    from fixer.tools import ToolDispatcher

    candidate, accepted = rejected_probe_candidate(controller)
    for number in range(30):
        (controller.sources.candidate / "scenarios" / ("existing_" + str(number) + ".yaml")).write_text(
            "name: existing\nchecks: [NORMAL]\n")
    dispatcher = ToolDispatcher(controller)
    fingerprints, reader = _probe_fingerprints(dispatcher)
    assert reader.reads[0] == 32
    result = dispatcher.execute("compose_probe", probe_reply().content[0]["input"])
    pair = _measure_probe(controller, Reviewer(), dispatcher, result, fingerprints, reader, candidate, accepted, 5)
    assert pair["complete"] and reader.reads[0] == 32 and reader.fresh_reads[0] == 2


def test_compaction_leaves_room_for_another_tool_turn_without_reset(controller):
    from fixer.main import COMPACT_CONTEXT_CHARS, MAX_CONTEXT_CHARS

    controller.initial_context = "stable orientation\n" + "i" * 10000
    messages = [{"role": "user", "content": controller.initial_context}]
    for number in range(7):
        messages += [{"role": "assistant", "content": [{"type": "tool_use", "id": str(number),
                                                       "name": "read_file", "input": {"path": "src/service.py"}}]},
                     {"role": "user", "content": [{"type": "tool_result", "tool_use_id": str(number),
                                                   "content": "r" * 31000}]}]
    compacted = compact_messages(controller, messages)
    assert len(json.dumps(compacted, ensure_ascii=False)) <= COMPACT_CONTEXT_CHARS
    assert_native_pairs(compacted)
    assert any(message["role"] == "assistant" for message in compacted)
    grown = compacted + [{"role": "assistant", "content": [{"type": "tool_use", "id": "next",
                                                           "name": "read_file", "input": {"path": "src/service.py"}}]},
                         {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "next",
                                                       "content": "new source " + "r" * 31000}]}]
    assert len(json.dumps(grown, ensure_ascii=False)) < MAX_CONTEXT_CHARS
    assert compact_messages(controller, grown) is grown
    assert_native_pairs(grown)


def test_compaction_soft_target_has_finite_escape_safe_fallback(controller, monkeypatch):
    import threading
    import fixer.main as main

    ledger = {"source_hash": controller.sources.candidate_hash,
              "accepted_source_hash": controller.sources.best_hash,
              "hypotheses": [{"mechanism": '"' * 31000, "evidence": ["quote-heavy source fixture"]}]}
    text = json.dumps(ledger, ensure_ascii=False)
    assert len(text) < main.MAX_LEDGER_CHARS
    monkeypatch.setattr(main, "prompt_status", lambda _controller: text)
    controller.initial_context = '"' * 160000
    result = []
    worker = threading.Thread(target=lambda: result.append(compact_messages(
        controller, [{"role": "user", "content": controller.initial_context}], force=True)), daemon=True)
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive() and result, "compaction failed to terminate at its soft target"
    serialized = json.dumps(result[0], ensure_ascii=False)
    assert main.COMPACT_CONTEXT_CHARS < len(serialized) <= main.MAX_CONTEXT_CHARS
    preserved = json.loads(result[0][-1]["content"].split("# Current repair ledger\n", 1)[1])
    assert preserved == ledger


def test_compaction_observes_cancellation_during_adaptation(controller, monkeypatch, capsys):
    import fixer.main as main

    controller.initial_context = '"' * 160000
    ledger = json.dumps({"source_hash": controller.sources.candidate_hash,
                         "accepted_source_hash": controller.sources.best_hash})
    monkeypatch.setattr(main, "prompt_status", lambda _controller: ledger)
    original_dumps = json.dumps
    calls = 0

    def dumps(*args, **kwargs):
        nonlocal calls
        calls += 1
        value = original_dumps(*args, **kwargs)
        if calls == 3:
            controller.deadline.stop_event.set()
        return value

    monkeypatch.setattr(main.json, "dumps", dumps)
    messages = [{"role": "user", "content": controller.initial_context}]
    for number in range(4):
        messages += [{"role": "assistant", "content": [{"type": "tool_use", "id": str(number),
                                                       "name": "status", "input": {}}]},
                     {"role": "user", "content": [{"type": "tool_result", "tool_use_id": str(number),
                                                   "content": "r" * 31000}]}]
    compacted = compact_messages(controller, messages, force=True)
    assert controller.deadline.stop_event.is_set() and calls < 10
    assert len(original_dumps(compacted, ensure_ascii=False)) <= main.MAX_CONTEXT_CHARS
    assert_native_pairs(compacted)
    compacted_event = [row for row in log_events(capsys) if row["event"] == "context_compacted"][-1]
    assert compacted_event["cancelled"]


def test_turn_limit_telemetry_reports_remaining_resources(controller, monkeypatch, capsys):
    monkeypatch.setattr("fixer.main.MAX_TURNS", 3)

    class LimitedModel(Reviewer):
        remaining_budget = 2.5
        spent_usd = .5

        def messages(self, system, messages, tools, **kwargs):
            return ModelReply([{"type": "tool_use", "id": "status", "name": "status", "input": {}}],
                              "tool_use", {}, 0, self.remaining_budget)

    repair_loop(controller, LimitedModel())
    stopped = [row for row in log_events(capsys) if row["event"] == "repair_loop_stopped"][-1]
    assert stopped["reason"] == "turn_limit" and stopped["model_turns"] == stopped["attempted_turns"] == 3
    assert stopped["remaining_budget_usd"] == 2.5 and stopped["model_spent_usd"] == .5
    assert stopped["seconds_remaining"] > stopped["final_reserve"]


def test_proactive_validation_keeps_time_for_actionable_feedback(controller, monkeypatch):
    controller.sources.apply_edits(**final_patch(controller))
    monkeypatch.setattr(controller, "validate", lambda _plan: pytest.fail("validation consumed feedback reserve"))

    class ReadingModel(Reviewer):
        turns = 0

        def messages(self, system, messages, tools, **kwargs):
            self.turns += 1
            if self.turns == 6:
                monkeypatch.setattr(controller.deadline, "_end", time.monotonic() + controller.final_reserve + 10)
            if self.turns > 6:
                raise ModelError("script complete", code="script_complete")
            return ModelReply([{"type": "tool_use", "id": str(self.turns), "name": "read_file",
                                "input": {"path": "src/service.py"}}], "tool_use", {}, 0, 3)

    repair_loop(controller, ReadingModel())
    assert not controller.accepted


def test_verified_promotions_allow_more_than_three_partial_repair_endings(controller, monkeypatch):
    from types import SimpleNamespace
    from fixer.tools import ToolDispatcher

    class FourObligationController:
        initial_context = "Repair four supported local obligations."

        def __init__(self):
            self.deadline = controller.deadline
            self.final_reserve = controller.final_reserve
            self.test_seconds = controller.test_seconds
            self.replay_seconds = controller.replay_seconds
            self.sources = SimpleNamespace(candidate_hash="0" * 64, best_hash="0" * 64,
                                           diff=lambda **_kwargs: "one staged protocol correction")
            self.accepted = []

        def status(self):
            return {"source_hash": self.sources.candidate_hash, "accepted_source_hash": self.sources.best_hash,
                    "unaccepted_changes": self.sources.candidate_hash != self.sources.best_hash,
                    "seconds_remaining": self.deadline.remaining(), "remaining_budget_usd": 3,
                    "accepted_repairs": len(self.accepted), "hypotheses": [], "candidate_results": [],
                    "known_failures": [{"scenario": "public", "seed": 1, "status": "invariant_failure",
                                        "source_hash": self.sources.best_hash, "violations": [{"invariant": "PROTOCOL"}]}]
                    if len(self.accepted) < 4 else []}

        def validate(self, _plan):
            # This fake models the Controller contract: only a passing gate
            # advances best_hash. Other tests exercise the real gate itself.
            self.sources.best_hash = self.sources.candidate_hash
            self.accepted.append({"source_hash": self.sources.best_hash, "reason": "verified local obligation",
                                  "closed_invariants": ["PROTOCOL"]})
            return {"accepted": True, "source_hash": self.sources.best_hash}

    staged = FourObligationController()

    def execute(self, name, args):
        assert name == "apply_edits" and args["base_hash"] == self.controller.sources.candidate_hash
        self.controller.sources.candidate_hash = f"{len(self.controller.accepted) + 1:064x}"
        return {"ok": True, "result": {"source_hash": self.controller.sources.candidate_hash}}

    monkeypatch.setattr(ToolDispatcher, "execute", execute)

    class FourRepairModel(Reviewer):
        turns = 0

        def messages(self, system, messages, tools, **kwargs):
            self.turns += 1
            assert_native_pairs(messages)
            if self.turns % 2 and len(staged.accepted) < 4:
                block = {"type": "tool_use", "id": str(self.turns), "name": "apply_edits", "input": {
                    "base_hash": staged.sources.candidate_hash,
                    "edits": [{"path": "src/protocol.py", "content": "# coherent protocol correction"}]}}
                return ModelReply([block], "tool_use", {}, 0, 3)
            return ModelReply([{"type": "text", "text": "Validate this supported repair."}], "end_turn", {}, 0, 3)

    model = FourRepairModel()
    repair_loop(staged, model)
    assert len(staged.accepted) == 4 and model.turns == 9
    assert staged.sources.candidate_hash == staged.sources.best_hash
