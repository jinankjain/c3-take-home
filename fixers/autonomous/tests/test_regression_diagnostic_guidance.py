from copy import deepcopy
import json

import pytest

import fixer.main as main
from fixer.controller import _ledger_review
from controller_support import controller


def diagnostic_status(controller):
    controller.sources.apply_edits(
        base_hash=controller.sources.candidate_hash,
        edits=[{"path": "src/service.py", "old": "OWNERSHIP_BROKEN = True",
                "new": "OWNERSHIP_BROKEN = False"}])
    status = controller.status()
    current, best = status["source_hash"], status["accepted_source_hash"]
    references = [{
        "pair_id": f"{index + 1:064x}", "before_run_id": "4" * 64,
        "after_run_id": "5" * 64, "before_source_hash": best,
        "after_source_hash": current, "before_status": "pass",
        "after_status": "invariant_failure",
        "input": {"scenario_hash": "1" * 64, "faults_hash": "2" * 64, "seed": 7 + index},
    } for index in range(3)]
    verdict = _ledger_review({
        "approve": True, "source_hash": current, "issues": ["Inspect the state writer."] * 3,
        "missing_evidence": [], "limitations": ["Compare send and acknowledgement updates."],
        "issue_evidence": [{
            "kind": "regression", "source_path": "src/service.py",
            "detail": "The candidate loses a previously passing obligation.",
            "obligation": "OWNERSHIP", "experiments": references,
        }] * 3,
    })
    status["last_validation"] = {
        "source_hash": current, "accepted": False, "complete": True,
        "stage": "matrix", "reason": "previously passing scenario regressed",
        "regression_diagnostic": {
            "scope": "diagnostic_only", "status": "completed",
            "source_hash": current, "accepted_source_hash": best,
            "authorizes_promotion": False, "reason": "Independent causal feedback.",
            "review": verdict,
        },
    }
    return status, references


def test_primary_control_witness_survives_without_native_issue_references(
        controller, monkeypatch):
    status, references = diagnostic_status(controller)
    diagnostic = status["last_validation"]["regression_diagnostic"]
    diagnostic["primary_pair"] = {key: deepcopy(references[0][key]) for key in
                                  ("pair_id", "input", "before_run_id", "after_run_id",
                                   "before_source_hash", "after_source_hash",
                                   "before_status", "after_status")}
    diagnostic["review"] = _ledger_review({
        "approve": True, "issues": [], "missing_evidence": [],
        "limitations": ["Diagnosis was inconclusive."]})
    monkeypatch.setattr(controller, "status", lambda: deepcopy(status))
    for state in ("completed", "skipped", "failed"):
        diagnostic["status"] = state
        for text in (main.prompt_status(controller), main._minimal_prompt_status(status)):
            validation = json.loads(text)["last_validation"]
            assert validation["accepted"] is False
            retained = validation["regression_diagnostic"]
            assert retained["primary_pair"] == diagnostic["primary_pair"]
            assert retained["status"] == state
            assert retained["authorizes_promotion"] is False
            assert "approve" not in retained["review"]


@pytest.mark.parametrize("minimal", [False, True])
def test_regression_advice_survives_compaction_without_becoming_approval(
        controller, monkeypatch, minimal):
    status, references = diagnostic_status(controller)
    original = deepcopy(status)
    monkeypatch.setattr(controller, "status", lambda: deepcopy(status))
    if minimal:
        monkeypatch.setattr(main, "MAX_LEDGER_CHARS", 3500)
    controller.initial_context = "earlier investigation " * 10000
    compacted = main.compact_messages(
        controller, [{"role": "user", "content": controller.initial_context}], force=True)
    assert len(json.dumps(compacted, ensure_ascii=False)) <= main.MAX_CONTEXT_CHARS
    ledger = json.loads(compacted[-1]["content"].split("# Current repair ledger\n", 1)[1])
    validation = ledger["last_validation"]
    assert validation["accepted"] is False
    assert validation["reason"] == "previously passing scenario regressed"
    diagnostic = validation["regression_diagnostic"]
    assert diagnostic["scope"] == "diagnostic_only"
    assert diagnostic["authorizes_promotion"] is False
    assert "approve" not in diagnostic["review"]
    assert diagnostic["source_hash"] == status["source_hash"]
    assert diagnostic["accepted_source_hash"] == status["accepted_source_hash"]
    evidence = diagnostic["review"]["issue_evidence"][0]
    assert evidence["kind"] == "regression" and evidence["source_path"] == "src/service.py"
    assert evidence["experiments"][0] == references[0]
    assert evidence["experiments_omitted"] == (2 if minimal else 0)
    assert status == original


@pytest.mark.parametrize("stale_field", ["source_hash", "accepted_source_hash"])
def test_regression_advice_from_another_source_basis_is_omitted(
        controller, monkeypatch, stale_field):
    status, _ = diagnostic_status(controller)
    status["last_validation"]["regression_diagnostic"][stale_field] = "f" * 64
    monkeypatch.setattr(controller, "status", lambda: deepcopy(status))
    for text in (main.prompt_status(controller), main._minimal_prompt_status(status)):
        validation = json.loads(text)["last_validation"]
        assert validation["accepted"] is False
        assert validation["reason"] == "previously passing scenario regressed"
        assert "regression_diagnostic" not in validation
