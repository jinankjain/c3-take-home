from __future__ import annotations

from copy import deepcopy
import json

import pytest

from controller_support import Reviewer, controller, plan, repair_ownership, review_reply
from fixer.controller import _review_tool, _review_verdict
from fixer.model import ModelError, ModelReply
from fixer.tools import TOOL_DEFINITIONS


SOURCE = "1" * 64
OTHER_SOURCE = "2" * 64
DUMMY = "dummy-review-token-never-real"


def verdict():
    return {"source_hash": SOURCE, "approve": True, "issues": [], "missing_evidence": []}


def decision(payload=None, *, name="submit_review", identifier="review-1"):
    return {"type": "tool_use", "id": identifier, "name": name,
            "input": verdict() if payload is None else payload}


def test_native_decision_uses_source_bound_input_and_ignores_adjacent_text():
    supplied = verdict()
    supplied["limitations"] = ["Hidden grading results are unavailable"]
    content = [{"type": "text", "text": json.dumps({**verdict(), "approve": False})},
               {"type": "thinking", "thinking": "check the protocol", "signature": "signed"},
               decision(supplied)]
    parsed = _review_verdict(content, SOURCE)
    assert parsed == supplied and parsed["approve"]
    supplied["issues"].append("later mutation")
    assert not parsed["issues"]


@pytest.mark.parametrize("content", [
    [],
    [{"type": "text", "text": json.dumps(verdict())}],
    [decision(), decision(identifier="review-2")],
    [decision(), decision(name="apply_edits", identifier="other-tool")],
    [decision(name=DUMMY)],
    [decision(identifier="")],
    [{"type": "server_tool_use", "name": "submit_review", "input": verdict()}],
    [{"type": [DUMMY]}, decision()],
    [{"type": {DUMMY: True}}, decision()],
    [DUMMY],
])
def test_missing_duplicate_unexpected_or_malformed_native_decisions_are_rejected_safely(content):
    with pytest.raises(ModelError) as error:
        _review_verdict(content, SOURCE)
    assert error.value.code == "review_protocol"
    assert DUMMY not in str(error.value)


@pytest.mark.parametrize("key,value", [
    ("source_hash", OTHER_SOURCE),
    ("source_hash", 1),
    ("approve", 1),
    ("approve", "true"),
    ("issues", {"detail": DUMMY}),
    ("issues", [1]),
    ("missing_evidence", [None]),
    ("limitations", "hidden results"),
    ("limitations", [False]),
    ("event", DUMMY),
    ("cost_usd", DUMMY),
])
def test_invalid_types_source_binding_and_unknown_fields_cannot_supply_a_verdict(key, value):
    payload = {**verdict(), key: value}
    with pytest.raises(ModelError) as error:
        _review_verdict([decision(payload)], SOURCE)
    expected_code = "review_evidence" if key == "source_hash" and isinstance(value, str) else "review_protocol"
    assert error.value.code == expected_code and DUMMY not in str(error.value)


@pytest.mark.parametrize("key", ["source_hash", "approve", "issues", "missing_evidence"])
def test_native_decision_requires_every_mandatory_field(key):
    payload = verdict()
    del payload[key]
    with pytest.raises(ModelError) as error:
        _review_verdict([decision(payload)], SOURCE)
    assert error.value.code == "review_protocol"


def test_review_tool_is_local_and_binds_the_single_candidate_hash():
    tool = _review_tool(SOURCE)
    assert tool["name"] == "submit_review"
    schema = tool["input_schema"]
    assert schema["properties"]["source_hash"] == {"type": "string", "enum": [SOURCE]}
    assert not schema["additionalProperties"]
    assert set(schema["required"]) == {"source_hash", "approve", "issues", "missing_evidence"}
    assert schema["properties"]["limitations"] == {"type": "array", "items": {"type": "string"}}
    assert all(item["name"] != "submit_review" for item in TOOL_DEFINITIONS)


class NativeReviewer(Reviewer):
    def __init__(self, supplied=None):
        super().__init__()
        self.supplied = supplied or {"approve": True, "issues": [], "missing_evidence": []}
        self.calls = []

    def messages(self, system, messages, tools, **kwargs):
        self.calls.append((system, deepcopy(messages), deepcopy(tools), kwargs))
        return review_reply(self.supplied, tools)


def test_controller_forces_native_review_and_supplies_exact_hash_in_context(controller):
    expected = repair_ownership(controller)["source_hash"]
    reviewer = controller.model = NativeReviewer()
    after = [controller.replay("candidate", r.scenario, r.seed, r.faults) for r in controller.baseline]
    result = controller._review(plan(), controller.baseline, after, full_pass=False)
    assert result["approve"] and result["source_hash"] == expected
    _, messages, tools, kwargs = reviewer.calls[-1]
    assert kwargs["tool_choice"] == {"type": "tool", "name": "submit_review"}
    assert kwargs["max_tokens"] == 2048
    assert tools[0]["input_schema"]["properties"]["source_hash"]["enum"] == [expected]
    assert json.dumps({"source_hash": expected}) in messages[0]["content"]


def test_valid_native_partial_review_can_accept_with_nonblocking_limitations(controller):
    candidate = repair_ownership(controller)["source_hash"]
    limitations = ["A separate recovery obligation remains", "Hidden grading results are unavailable"]
    controller.model = NativeReviewer({"approve": True, "issues": [], "missing_evidence": [],
                                       "limitations": limitations})
    result = controller.validate(plan())
    assert result["accepted"] and not result["full_local_pass"]
    assert result["review"]["source_hash"] == candidate
    assert result["review"]["limitations"] == limitations
    recorded = result["status"]["last_validation"]["review"]
    assert recorded["source_hash"] == candidate and recorded["limitations"] == limitations


@pytest.mark.parametrize("fields", [
    {"approve": False},
    {"approve": True, "issues": ["A concrete protocol regression"]},
    {"approve": True, "missing_evidence": ["A specific local restart reproduction"]},
])
def test_explicit_native_rejection_and_blockers_veto_a_full_passing_matrix(controller, fields):
    original = controller.sources.best_hash
    controller.sources.apply_edits(controller.sources.candidate_hash, [
        {"path": "src/service.py", "old": "OWNERSHIP_BROKEN = True", "new": "OWNERSHIP_BROKEN = False"},
        {"path": "src/service.py", "old": "RECOVERY_BROKEN = True", "new": "RECOVERY_BROKEN = False"},
    ])
    controller.model = NativeReviewer({"approve": True, "issues": [], "missing_evidence": [], **fields})
    result = controller.validate(plan())
    assert not result["accepted"] and controller.sources.best_hash == original
    assert controller.status()["last_validation"]["matrix"]["full_pass"]
    assert "skipped" not in result["review"]


class TextReviewer(Reviewer):
    def messages(self, *_args, **_kwargs):
        return ModelReply([{"type": "text", "text": json.dumps(verdict())}], "end_turn", {}, 0, 3)


@pytest.mark.parametrize("full_pass", [False, True])
def test_unavailable_native_review_keeps_existing_full_matrix_fallback_policy(controller, full_pass):
    controller.model = TextReviewer()
    after = [controller.replay("candidate", r.scenario, r.seed, r.faults) for r in controller.baseline]
    result = controller._review(plan(), controller.baseline, after, full_pass=full_pass)
    assert result["approve"] is full_pass and result["skipped"]
    assert not result["issues"] and not result["missing_evidence"]


def test_wrong_source_native_review_cannot_promote_partial_repair(controller):
    original = controller.sources.best_hash
    repair_ownership(controller)
    class WrongSourceReviewer(NativeReviewer):
        def messages(self, system, messages, tools, **kwargs):
            reply = super().messages(system, messages, tools, **kwargs)
            reply.content[0]["input"]["source_hash"] = DUMMY
            return reply
    controller.model = WrongSourceReviewer()
    result = controller.validate(plan())
    assert not result["accepted"] and controller.sources.best_hash == original
    assert not result["review"]["approve"] and result["review"]["evidence_error"]
    assert "skipped" not in result["review"]
    assert DUMMY not in json.dumps(result["review"])


class CitationReviewer(NativeReviewer):
    def __init__(self, *, repeat=False, legitimate_blocker=False, correction_error=None):
        super().__init__()
        self.repeat = repeat
        self.legitimate_blocker = legitimate_blocker
        self.correction_error = correction_error

    def messages(self, system, messages, tools, **kwargs):
        self.calls.append((system, deepcopy(messages), deepcopy(tools), kwargs))
        if len(self.calls) > 1 and self.correction_error is not None:
            raise self.correction_error
        evidence = json.loads(messages[0]["content"].rsplit("# Matched experiment pairs and host facts\n", 1)[1])
        pair = next(p for p in evidence["pairs"] if p["before"]["scenario"] == "public")
        # The accepted public input fails; a same-seed healthy base control must not supply its baseline.
        assert not pair["facts"]["before_pass"]
        if len(self.calls) == 1 or self.repeat:
            issues = [{"kind": "regression", "detail": "This public workload passed before and now fails.",
                       "obligation": "Preserve passing controls", "source_path": "src/service.py",
                       "pair_ids": [pair["pair_id"]]}]
            if self.legitimate_blocker:
                issues.append({"kind": "source_defect", "detail": "Recovery discards durable ownership.",
                               "obligation": "Durable recovery", "source_path": "src/service.py", "pair_ids": []})
            return review_reply({"approve": False, "issues": issues, "missing_evidence": []}, tools)
        assert messages[-1]["content"][0]["type"] == "tool_result"
        assert messages[-1]["content"][0]["is_error"]
        return review_reply({"approve": True, "issues": [], "missing_evidence": [],
                             "limitations": ["A separate recovery obligation remains"]}, tools)


def test_actual_partial_validation_can_correct_false_passing_baseline_without_waiving_review(controller):
    candidate = repair_ownership(controller)["source_hash"]
    reviewer = controller.model = CitationReviewer()
    result = controller.validate(plan())
    assert result["accepted"] and result["closed_invariants"] == ["OWNERSHIP"]
    assert controller.sources.best_hash == candidate and result["review"]["correction_attempted"]
    assert len(reviewer.calls) == 2
    first_context = reviewer.calls[0][1][0]["content"]
    assert "# Current source and unchanged consumers\n" in first_context
    pairs = json.loads(first_context.rsplit("# Matched experiment pairs and host facts\n", 1)[1])["pairs"]
    assert any(p["before"]["scenario"] == "base" and p["facts"]["before_pass"] for p in pairs)
    assert all(p["before"]["scenario_hash"] == p["after"]["scenario_hash"] for p in pairs)
    assert all(p["before"]["source_hash"] != p["after"]["source_hash"] for p in pairs)


@pytest.mark.parametrize("correction_error", [None, ModelError("review budget unavailable", code="budget")])
def test_invalid_factual_reference_never_becomes_full_matrix_unavailable_approval(controller, correction_error):
    original = controller.sources.best_hash
    controller.sources.apply_edits(controller.sources.candidate_hash, [
        {"path": "src/service.py", "old": "OWNERSHIP_BROKEN = True", "new": "OWNERSHIP_BROKEN = False"},
        {"path": "src/service.py", "old": "RECOVERY_BROKEN = True", "new": "RECOVERY_BROKEN = False"}])
    reviewer = controller.model = CitationReviewer(repeat=True, correction_error=correction_error)
    result = controller.validate(plan())
    assert not result["accepted"] and controller.sources.best_hash == original
    assert controller.status()["last_validation"]["matrix"]["full_pass"]
    assert result["review"]["evidence_error"] and "skipped" not in result["review"]
    assert len(reviewer.calls) == 2


def test_citation_correction_preserves_a_legitimate_source_blocker_without_another_review(controller):
    repair_ownership(controller)
    reviewer = controller.model = CitationReviewer(legitimate_blocker=True)
    result = controller.validate(plan())
    assert not result["accepted"] and len(reviewer.calls) == 1
    assert result["review"]["issues"] and "durable ownership" in result["review"]["issues"][0]
    assert not result["review"]["evidence_error"]["correctable"]


def test_wrong_host_input_pairing_blocks_even_full_pass_without_a_model_call(controller):
    candidate = repair_ownership(controller)["source_hash"]
    after = [controller.replay("candidate", "base", r.seed) for r in controller.baseline]
    reviewer = controller.model = NativeReviewer()
    verdict = controller._review(plan(), controller.baseline, after, full_pass=True)
    assert not verdict["approve"] and verdict["evidence_error"] and not reviewer.calls
    assert controller.sources.best_hash != candidate


def test_review_cancelled_during_native_request_cannot_promote(controller):
    original = controller.sources.best_hash
    repair_ownership(controller)
    class CancellingReviewer(NativeReviewer):
        def messages(self, *args, **kwargs):
            reply = super().messages(*args, **kwargs)
            controller.deadline.stop_event.set()
            return reply
    controller.model = CancellingReviewer()
    result = controller.validate(plan())
    assert not result["accepted"] and controller.sources.best_hash == original
    assert not result["review"]["approve"] and "skipped" not in result["review"]
