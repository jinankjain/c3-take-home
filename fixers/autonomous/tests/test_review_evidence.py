from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest

from fixer.model import ModelError
from fixer.review import ReviewEvidenceError, build_review_pairs, review_tool, review_verdict


ACCEPTED = "a" * 64
CANDIDATE = "b" * 64


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def run(label, snapshot, *, scenario="stream", faults=None, seed=7, status="pass", families=()):
    return SimpleNamespace(run_id=label, source_hash=snapshot, scenario=scenario,
                           scenario_hash=digest(scenario + "\0" + str(faults)),
                           faults=faults, faults_hash=digest(faults) if faults else None,
                           seed=seed, status=status, violations=[{"invariant": f} for f in families])


def decision(issues=(), **fields):
    return [{"type": "tool_use", "id": "review", "name": "submit_review",
             "input": {"source_hash": CANDIDATE, "approve": not issues,
                       "issues": list(issues), "missing_evidence": [], **fields}}]


def issue(kind, pair_ids=(), *, detail="The observed transition violates the declared obligation.", path="src/service.py"):
    return {"kind": kind, "detail": detail, "obligation": "Durable handoff and bounded retry",
            "source_path": path, "pair_ids": list(pair_ids)}


def mixed_evidence():
    # Same seed, different workload: a healthy control is not the failing stream's baseline.
    old_failure = run("old-stream", ACCEPTED, status="invariant_failure", families=("HANDOFF",))
    old_control = run("old-control", ACCEPTED, scenario="quiet")
    new_failure = run("new-stream", CANDIDATE, status="invariant_failure", families=("DEDUP",))
    new_control = run("new-control", CANDIDATE, scenario="quiet")
    return build_review_pairs([old_failure, old_control], [new_control, new_failure], ACCEPTED, CANDIDATE)


def test_shared_seed_control_cannot_supply_the_failing_workloads_before_result():
    evidence = mixed_evidence()
    failing, control = evidence["pairs"]
    assert failing["before"]["run_id"] == "old-stream"
    assert failing["after"]["run_id"] == "new-stream"
    assert failing["facts"]["resolved_families"] == ["HANDOFF"]
    assert failing["facts"]["newly_observed_families"] == ["DEDUP"]
    assert not failing["facts"]["passing_control_regressed"]
    for pair in (failing, control):
        with pytest.raises(ReviewEvidenceError) as error:
            review_verdict(decision([issue("regression", [pair["pair_id"]])]), CANDIDATE, evidence)
        feedback = error.value.feedback(CANDIDATE)
        assert not feedback["approve"] and feedback["missing_evidence"]
        assert error.value.correctable and "skipped" not in feedback


def test_source_dependency_can_still_veto_a_partial_repair_on_the_matched_failing_input():
    evidence = mixed_evidence()
    pair = evidence["pairs"][0]
    blocker = issue("source_defect", [pair["pair_id"]], detail="Recovery can publish an acknowledged item before its durable owner exists.")
    verdict = review_verdict(decision([blocker]), CANDIDATE, evidence)
    assert not verdict["approve"] and isinstance(verdict["issues"][0], str)
    refs = verdict["issue_evidence"][0]["experiments"][0]
    assert refs["before_run_id"] == "old-stream" and refs["after_run_id"] == "new-stream"
    assert refs["before_source_hash"] == ACCEPTED and refs["after_source_hash"] == CANDIDATE
    blocker["pair_ids"].clear()
    assert verdict["issue_evidence"][0]["pair_ids"] == [pair["pair_id"]]


def test_a_real_control_regression_has_a_valid_native_reference_but_still_blocks():
    old = run("old", ACCEPTED)
    new = run("new", CANDIDATE, status="invariant_failure", families=("OWNER",))
    evidence = build_review_pairs([old], [new], ACCEPTED, CANDIDATE)
    verdict = review_verdict(decision([issue("regression", [evidence["pairs"][0]["pair_id"]])], approve=True),
                             CANDIDATE, evidence)
    assert verdict["approve"] and verdict["issues"]  # Existing acceptance gate must reject this contradiction.


def test_retained_obligation_is_grounded_even_when_both_runs_still_fail_elsewhere():
    old = run("old", ACCEPTED, status="invariant_failure", families=("RECOVERY",))
    new = run("new", CANDIDATE, status="invariant_failure", families=("RECOVERY", "OWNER"))
    obligation = {"scenario_hash": old.scenario_hash, "faults_hash": old.faults_hash,
                  "seed": old.seed, "invariants": ["OWNER"]}
    evidence = build_review_pairs([old], [new], ACCEPTED, CANDIDATE, [obligation])
    pair = evidence["pairs"][0]
    assert pair["facts"]["protected_obligation_regressed"]
    assert pair["facts"]["lost_obligation_families"] == ["OWNER"]
    verdict = review_verdict(decision([issue("regression", [pair["pair_id"]])]), CANDIDATE, evidence)
    assert verdict["issues"] and not verdict["approve"]


@pytest.mark.parametrize("change", ["source", "faults", "seed", "missing", "duplicate", "incomplete"])
def test_review_refuses_unmatched_or_unusable_host_evidence(change):
    before = [run("old", ACCEPTED)]
    after = [run("new", CANDIDATE)]
    if change == "source": after[0].source_hash = ACCEPTED
    if change == "faults": after[0] = run("new", CANDIDATE, faults="loss.yaml")
    if change == "seed": after[0].seed += 1
    if change == "missing": after.clear()
    if change == "duplicate": before *= 2; after *= 2
    if change == "incomplete": after[0].status = "timed_out"
    with pytest.raises(ReviewEvidenceError) as error:
        build_review_pairs(before, after, ACCEPTED, CANDIDATE)
    assert not error.value.correctable and not error.value.feedback(CANDIDATE)["approve"]


def test_changed_or_missing_retained_inputs_cannot_disappear_from_review():
    old = run("old", ACCEPTED, faults="loss.yaml")
    new = run("new", CANDIDATE, faults="loss.yaml")
    obligation = {"scenario_hash": old.scenario_hash, "faults_hash": digest("restart.yaml"),
                  "seed": old.seed, "invariants": ["OWNER"]}
    with pytest.raises(ReviewEvidenceError):
        build_review_pairs([old], [new], ACCEPTED, CANDIDATE, [obligation])


def test_alias_labels_do_not_change_pair_identity_but_effective_faults_do():
    old, new = run("old", ACCEPTED), run("new", CANDIDATE)
    new.scenario = "a-display-alias"
    pair = build_review_pairs([old], [new], ACCEPTED, CANDIDATE)["pairs"][0]
    ordinary = build_review_pairs([old], [run("new", CANDIDATE)], ACCEPTED, CANDIDATE)["pairs"][0]
    assert pair["pair_id"] == ordinary["pair_id"]
    fault_pair = build_review_pairs([run("old", ACCEPTED, faults="loss.yaml")],
                                   [run("new", CANDIDATE, faults="loss.yaml")], ACCEPTED, CANDIDATE)["pairs"][0]
    assert fault_pair["pair_id"] != pair["pair_id"]


def test_invalid_citation_correction_cannot_remove_a_legitimate_source_blocker():
    evidence = mixed_evidence()
    source = issue("source_defect", detail="A restart loses committed intent.")
    false_regression = issue("regression", [evidence["pairs"][0]["pair_id"]])
    with pytest.raises(ReviewEvidenceError) as error:
        review_verdict(decision([false_regression, source]), CANDIDATE, evidence)
    assert not error.value.correctable
    feedback = error.value.feedback(CANDIDATE)
    assert feedback["issues"] and not feedback["approve"]
    assert feedback["issue_evidence"][0]["kind"] == "source_defect"


def test_source_argument_remains_blocking_when_its_additional_run_reference_is_invalid():
    evidence = mixed_evidence()
    source = issue("source_defect", ["c" * 64], detail="The modified recovery path deletes durable intent before publishing its replacement.")
    with pytest.raises(ReviewEvidenceError) as error:
        review_verdict(decision([source]), CANDIDATE, evidence)
    feedback = error.value.feedback(CANDIDATE)
    assert not error.value.correctable and feedback["issues"] and not feedback["approve"]
    assert feedback["issue_evidence"][0]["invalid_pair_reference"]
    assert not feedback["issue_evidence"][0]["experiments"]


@pytest.mark.parametrize("kind", [[], {}, True])
def test_malformed_typed_blocker_has_a_fixed_protocol_error(kind):
    supplied = issue("source_defect"); supplied["kind"] = kind
    with pytest.raises(ModelError) as error:
        review_verdict(decision([supplied]), CANDIDATE, mixed_evidence())
    assert error.value.code == "review_protocol"


def test_wrong_candidate_hash_is_blocking_evidence_not_unavailable_review():
    with pytest.raises(ReviewEvidenceError) as error:
        review_verdict(decision(source_hash=ACCEPTED), CANDIDATE, mixed_evidence())
    feedback = error.value.feedback(CANDIDATE)
    assert feedback["source_hash"] == CANDIDATE and not feedback["approve"]
    assert "skipped" not in feedback and error.value.correctable


def test_tool_enumerates_only_host_bound_pair_references():
    evidence = mixed_evidence()
    schema = review_tool(CANDIDATE, evidence)["input_schema"]
    assert schema["properties"]["source_hash"]["enum"] == [CANDIDATE]
    issue_schema = schema["properties"]["issues"]["items"]
    assert issue_schema["properties"]["pair_ids"]["items"]["enum"] == [p["pair_id"] for p in evidence["pairs"]]
    assert not schema["additionalProperties"] and not issue_schema["additionalProperties"]


def test_large_seeds_are_exactly_distinguished_without_unbounded_decimal_json():
    seed = 1 << 100_000
    evidence = build_review_pairs([run("old", ACCEPTED, seed=seed)],
                                  [run("new", CANDIDATE, seed=seed)], ACCEPTED, CANDIDATE)
    next_evidence = build_review_pairs([run("old", ACCEPTED, seed=seed + 1)],
                                       [run("new", CANDIDATE, seed=seed + 1)], ACCEPTED, CANDIDATE)
    assert evidence["pairs"][0]["pair_id"] != next_evidence["pairs"][0]["pair_id"]
    assert len(json.dumps(evidence)) < 3000


def test_local_evidence_requests_remain_authoritative_despite_display_limits():
    gaps = [f"required witness {i}: " + "x" * 2000 for i in range(20)]
    verdict = review_verdict(decision(approve=False, missing_evidence=gaps), CANDIDATE, mixed_evidence())
    assert verdict["missing_evidence"] == gaps and not verdict["approve"]


def test_oversized_blocker_is_not_an_unavailable_review_approval():
    with pytest.raises(ReviewEvidenceError) as error:
        review_verdict(decision([issue("source_defect", detail="x" * 3000)]), CANDIDATE, mixed_evidence())
    assert not error.value.correctable and not error.value.feedback(CANDIDATE)["approve"]
