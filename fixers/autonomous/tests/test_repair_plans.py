from __future__ import annotations

import json

import pytest

from controller_support import Reviewer, controller, plan, repair_ownership, review_reply
from fixer.main import _validate_stalled_candidate, compact_messages, prompt_status, repair_loop, validate_pending_candidate
from fixer.model import ModelReply
from fixer.patches import source_hash


REASON = "Preserve the current ownership check before applying a protected action; recovery remains a separate open obligation."


def stage_partial(instance, declared_plan=True):
    base = instance.sources.candidate_hash
    instance.record_hypothesis({"id": "ownership", "obligation": "Protected actions require current ownership.",
                                "mechanism": "The ownership transition lacks its guard; recovery remains open.",
                                "evidence": ["src/service.py:1"], "status": "proposed"})
    result = repair_ownership(instance)
    instance.remember_repair_plan(base, result["source_hash"], {**plan(), "reason": REASON} if declared_plan else None)
    return base, result["source_hash"]


@pytest.mark.parametrize("entrypoint", ["stalled", "completion", "final"])
def test_automatic_validation_preserves_the_stated_partial_repair(controller, entrypoint):
    base, candidate = stage_partial(controller)

    class ScopedReviewer(Reviewer):
        def messages(self, system, messages, tools, **kwargs):
            if any(tool.get("name") == "submit_review" for tool in tools):
                context = messages[0]["content"]
                assert REASON in context and candidate in context
                assert "recovery remains" in context
                self.reviews.append(messages)
                return review_reply({"approve": True, "issues": [], "missing_evidence": []}, tools)
            return ModelReply([{"type": "text", "text": "The partial repair is staged; recovery remains open."}],
                              "end_turn", {}, 0, 3)

    reviewer = ScopedReviewer()
    controller.model = reviewer
    if entrypoint == "stalled":
        result = _validate_stalled_candidate(controller, set())
        assert result["accepted"]
    elif entrypoint == "final":
        assert validate_pending_candidate(controller)["accepted"]
    else:
        repair_loop(controller, reviewer)

    outcome = controller.validation_history[candidate]
    assert outcome["proposal_reason"] == REASON
    assert outcome["accepted"] and outcome["closed_invariants"] == ["OWNERSHIP"]
    assert len(reviewer.reviews) == 1
    assert source_hash(controller.sources.live / "src") == candidate != base
    assert not controller.accepted[0]["full_local_pass"]


def test_staged_intent_survives_compaction_and_cannot_be_mutated_by_the_caller(controller):
    base, candidate = stage_partial(controller)
    copied = controller.staged_plan()
    copied["reason"] = "Pretend every obligation is complete."
    copied["target_invariants"].clear()
    controller.record_hypothesis({"id": "ownership", "obligation": "A later independent finding.",
                                  "mechanism": "Continue investigating recovery.", "evidence": ["src/service.py:2"]})
    ledger = json.loads(prompt_status(controller))
    assert ledger["staged_repair"]["source_hash"] == candidate
    assert ledger["staged_repair"]["base_source_hash"] == base
    assert ledger["staged_repair"]["reason"] == REASON
    assert ledger["staged_repair"]["target_invariants"] == ["OWNERSHIP"]
    compacted = compact_messages(controller, [{"role": "user", "content": "Earlier investigation."}], force=True)
    assert REASON in json.dumps(compacted)
    assert controller.staged_plan()["reason"] == REASON


def test_uncaptured_source_changes_and_reset_cannot_inherit_an_old_plan(controller):
    base, first = stage_partial(controller)
    controller.sources.apply_edits(first, [{"path": "src/service.py", "old": "RECOVERY_BROKEN = True",
                                          "new": "RECOVERY_BROKEN = False"}])
    second = controller.sources.candidate_hash
    uncaptured = controller.staged_plan()
    assert second != first and uncaptured["source_hash"] == second
    assert uncaptured["intent_basis"] == "uncaptured" and uncaptured["reason"] != REASON
    assert uncaptured["target_invariants"] == []
    assert controller.review_hypotheses(uncaptured) == []
    controller.sources.reset_candidate()
    assert controller.sources.candidate_hash == base
    assert controller.status()["staged_repair"] is None


def test_metadata_free_edit_captures_its_base_reasoning_without_claiming_verification(controller):
    base, candidate = stage_partial(controller, declared_plan=False)
    captured = controller.staged_plan()
    assert captured["intent_basis"] == "edit_base_hypotheses"
    assert captured["source_hash"] == candidate and captured["base_source_hash"] == base
    assert "ownership transition lacks its guard" in captured["reason"]
    findings = controller.review_hypotheses(captured)
    assert findings[0]["source_hash"] == base and findings[0]["reviewed_source_hash"] == candidate
    assert findings[0]["relation_to_candidate"] == "edit_basis"
    assert not controller.accepted


def test_review_keeps_the_edit_basis_when_new_candidate_findings_arrive(controller):
    stage_partial(controller)
    for number in range(8):
        controller.record_hypothesis({"id": "additional-" + str(number), "obligation": "Another supported question.",
                                      "mechanism": "Inspect this separate protocol transition.", "evidence": ["src/service.py:3"]})
    findings = controller.review_hypotheses(controller.staged_plan())
    assert any(item["id"] == "ownership" and item["relation_to_candidate"] == "edit_basis" for item in findings)
    assert any(item["id"] == "additional-7" for item in findings)
    assert len(findings) <= 6
