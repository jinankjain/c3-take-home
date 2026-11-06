from __future__ import annotations

import json
import time

import pytest

from fixer.main import compact_messages, repair_loop, validate_pending_candidate
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
