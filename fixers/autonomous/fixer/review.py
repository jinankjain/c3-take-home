"""Source-bound independent review with host-checked experiment references."""
from __future__ import annotations

import hashlib
from copy import deepcopy
import re
from pathlib import PurePosixPath
from typing import Any

from .model import ModelError


REVIEW_TOOL_NAME = "submit_review"
MAX_PAIRS = 256
MAX_ISSUES = 8
MAX_REFERENCES = 6
MAX_TEXT = 2000
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_STATUSES = {"pass", "invariant_failure", "unresolved", "runtime_error"}
_ISSUE_KINDS = {"regression", "source_defect", "unverified_repair"}


class ReviewEvidenceError(ModelError):
    """A review fact is invalid; it must never become unavailable-review approval."""

    def __init__(self, message: str, *, correctable: bool = False,
                 issues: list[str] | None = None, issue_evidence: list[dict] | None = None,
                 missing_evidence: list[str] | None = None):
        super().__init__(message, code="review_evidence")
        self.issues = list(issues or [])
        self.issue_evidence = deepcopy(issue_evidence or [])
        self.missing_evidence = list(missing_evidence or [])
        # A legitimate blocker cannot be discarded by correcting a different citation.
        self.correctable = correctable and not self.issues and not self.missing_evidence

    def feedback(self, source_hash: str) -> dict:
        return {"source_hash": source_hash, "approve": False, "issues": list(self.issues),
                "missing_evidence": [*self.missing_evidence, str(self)],
                "issue_evidence": deepcopy(self.issue_evidence),
                "evidence_error": {"code": self.code, "correctable": self.correctable,
                                   "detail": str(self)}}


def _seed_view(seed: int) -> int | str:
    return seed if seed.bit_length() <= 256 else f"[seed integer omitted: {seed.bit_length()} bits]"


def _identity(run: Any, expected_source: str) -> tuple[str, str | None, int]:
    if run.source_hash != expected_source:
        raise ReviewEvidenceError("Review evidence loaded the wrong source snapshot; rerun the matching snapshot.")
    if not isinstance(run.scenario_hash, str) or not _HASH.fullmatch(run.scenario_hash):
        raise ReviewEvidenceError("Review evidence lacks a valid effective scenario hash; obtain a complete local replay.")
    if run.faults_hash is not None and (not isinstance(run.faults_hash, str) or not _HASH.fullmatch(run.faults_hash)):
        raise ReviewEvidenceError("Review evidence has an invalid effective fault hash; obtain a complete local replay.")
    if not isinstance(run.seed, int) or isinstance(run.seed, bool):
        raise ReviewEvidenceError("Review evidence requires an integer seed.")
    if (not isinstance(run.run_id, str) or not run.run_id or len(run.run_id) > 64
            or not isinstance(run.status, str) or run.status not in _STATUSES):
        raise ReviewEvidenceError("Review evidence contains an incomplete local replay.")
    return run.scenario_hash, run.faults_hash, run.seed


def _pair_id(identity: tuple[str, str | None, int]) -> str:
    scenario, faults, seed = identity
    magnitude = abs(seed)
    # Hash exact integer bytes, not a potentially enormous decimal rendering.
    seed_bytes = magnitude.to_bytes(max(1, (magnitude.bit_length() + 7) // 8), "big")
    return hashlib.sha256(b"review-pair-v1\0" + scenario.encode() + b"\0"
                          + (faults or "embedded").encode() + b"\0"
                          + (b"-" if seed < 0 else b"+") + seed_bytes).hexdigest()


def _families(run: Any) -> set[str]:
    if (not isinstance(run.violations, list)
            or any(not isinstance(v, dict) or not isinstance(v.get("invariant"), str)
                   for v in run.violations)):
        raise ReviewEvidenceError("Review evidence has malformed checker results; obtain a complete local check.")
    return {v["invariant"] for v in run.violations}


def _run_view(run: Any, snapshot: str) -> dict:
    return {"run_id": run.run_id, "source_hash": run.source_hash, "snapshot": snapshot,
            "scenario": str(run.scenario)[:256], "scenario_hash": run.scenario_hash,
            "faults": None if run.faults is None else str(run.faults)[:256],
            "faults_hash": run.faults_hash, "seed": _seed_view(run.seed),
            "status": run.status, "ok": run.status == "pass",
            "violation_count": len(run.violations)}


def build_review_pairs(before: list, after: list, accepted_hash: str, candidate_hash: str,
                       obligations: list[dict] | tuple = ()) -> dict:
    """Match complete snapshots by immutable inputs, independently of list order."""
    if (not isinstance(accepted_hash, str) or not _HASH.fullmatch(accepted_hash)
            or not isinstance(candidate_hash, str) or not _HASH.fullmatch(candidate_hash)):
        raise ReviewEvidenceError("Review requires valid accepted and candidate source hashes.")
    if (not isinstance(before, list) or not isinstance(after, list) or not before
            or len(before) != len(after) or len(before) > MAX_PAIRS):
        raise ReviewEvidenceError("Review requires a bounded complete before/after experiment matrix.")
    old_by_key, new_by_key = {}, {}
    for runs, expected, mapping in ((before, accepted_hash, old_by_key), (after, candidate_hash, new_by_key)):
        for run in runs:
            key = _identity(run, expected)
            if key in mapping:
                raise ReviewEvidenceError("Review evidence contains duplicate effective experiment identities.")
            mapping[key] = run
    if old_by_key.keys() != new_by_key.keys():
        raise ReviewEvidenceError("Before and after review evidence used different scenario, fault or seed inputs.")
    if not isinstance(obligations, (list, tuple)) or len(obligations) > MAX_PAIRS * MAX_ISSUES:
        raise ReviewEvidenceError("Review requires bounded retained obligation metadata.")
    protected: dict[tuple, set[str]] = {}
    for obligation in obligations:
        if (not isinstance(obligation, dict) or not isinstance(obligation.get("seed"), int)
                or isinstance(obligation["seed"], bool)
                or not isinstance(obligation.get("scenario_hash"), str)
                or not _HASH.fullmatch(obligation["scenario_hash"])
                or not isinstance(obligation.get("invariants"), list)
                or any(not isinstance(name, str) for name in obligation["invariants"])):
            raise ReviewEvidenceError("A previously verified obligation lacks a valid retained experiment identity.")
        keys = [key for key in old_by_key
                if key[0] == obligation["scenario_hash"] and key[2] == obligation["seed"]
                and ("faults_hash" not in obligation or key[1] == obligation["faults_hash"])]
        if len(keys) != 1:
            raise ReviewEvidenceError("A previously verified obligation is missing from the matched review matrix.")
        protected.setdefault(keys[0], set()).update(obligation["invariants"])
    pairs = []
    for key, old in old_by_key.items():
        new = new_by_key[key]
        old_families, new_families = _families(old), _families(new)
        preserved = protected.get(key, set()) - new_families
        lost = protected.get(key, set()) & new_families
        facts = {"before_pass": old.status == "pass", "after_pass": new.status == "pass",
                 "passing_control_regressed": old.status == "pass" and new.status != "pass",
                 "failing_reproduction_passes": old.status != "pass" and new.status == "pass",
                 "protected_obligation_regressed": bool(lost)}
        for name, names in (("resolved_families", old_families - new_families),
                            ("remaining_families", old_families & new_families),
                            ("newly_observed_families", new_families - old_families),
                            ("preserved_obligation_families", preserved),
                            ("lost_obligation_families", lost)):
            facts[name] = [value[:100] for value in sorted(names)[:16]]
            facts[name + "_count"] = len(names)
        facts["families_truncated"] = any(facts[name + "_count"] > 16 for name in (
            "resolved_families", "remaining_families", "newly_observed_families",
            "preserved_obligation_families", "lost_obligation_families"))
        pairs.append({"pair_id": _pair_id(key),
                      "input": {"scenario_hash": key[0], "faults_hash": key[1], "seed": _seed_view(key[2])},
                      "before": _run_view(old, "accepted"), "after": _run_view(new, "candidate"),
                      "facts": facts})
    return {"accepted_source_hash": accepted_hash, "candidate_source_hash": candidate_hash,
            "pairs": pairs, "counts": {"pairs": len(pairs), "protected_experiments": len(protected)}}


def review_tool(source_hash: str, evidence: dict | None = None) -> dict:
    pair_ids = [pair["pair_id"] for pair in (evidence or {}).get("pairs", [])]
    refs = {"type": "array", "items": {"type": "string", **({"enum": pair_ids} if pair_ids else {})},
            "maxItems": MAX_REFERENCES if pair_ids else 0}
    strings = {"type": "array", "items": {"type": "string"}}
    issue = {"type": "object", "additionalProperties": False,
             "properties": {"kind": {"type": "string", "enum": sorted(_ISSUE_KINDS)},
                            "detail": {"type": "string", "maxLength": MAX_TEXT},
                            "obligation": {"type": "string", "maxLength": 500},
                            "source_path": {"type": "string", "maxLength": 256}, "pair_ids": refs},
             "required": ["kind", "detail", "obligation", "source_path", "pair_ids"]}
    return {"name": REVIEW_TOOL_NAME,
            "description": "Submit a source-bound verdict with typed blockers grounded in the supplied matched experiment pairs.",
            "input_schema": {"type": "object", "additionalProperties": False,
                             "properties": {"source_hash": {"type": "string", "enum": [source_hash]},
                                            "approve": {"type": "boolean"},
                                            "issues": {"type": "array", "items": issue, "maxItems": MAX_ISSUES},
                                            "missing_evidence": dict(strings), "limitations": dict(strings)},
                             "required": ["source_hash", "approve", "issues", "missing_evidence"]}}


def _native_input(content: list[dict]) -> dict:
    if not isinstance(content, list) or any(not isinstance(block, dict) for block in content):
        raise ModelError("review returned invalid content blocks", code="review_protocol")
    if any(not isinstance(block.get("type"), str)
           or block["type"] not in {"text", "thinking", "redacted_thinking", "tool_use"} for block in content):
        raise ModelError("review returned an unexpected content block", code="review_protocol")
    decisions = [block for block in content if block.get("type") == "tool_use"]
    if len(decisions) != 1:
        raise ModelError("review must return exactly one submit_review decision", code="review_protocol")
    call = decisions[0]
    if (call.get("name") != REVIEW_TOOL_NAME or not isinstance(call.get("id"), str)
            or not call["id"] or not isinstance(call.get("input"), dict)):
        raise ModelError("review returned an invalid submit_review call", code="review_protocol")
    verdict = call["input"]
    required = {"source_hash", "approve", "issues", "missing_evidence"}
    if not required.issubset(verdict) or set(verdict) - (required | {"limitations"}):
        raise ModelError("review returned invalid verdict fields", code="review_protocol")
    if not isinstance(verdict["source_hash"], str) or not isinstance(verdict["approve"], bool):
        raise ModelError("review returned invalid source or approval types", code="review_protocol")
    for key in ("missing_evidence", "limitations"):
        if key in verdict and (not isinstance(verdict[key], list)
                               or any(not isinstance(item, str) for item in verdict[key])):
            raise ModelError("review returned invalid evidence lists", code="review_protocol")
    if not isinstance(verdict["issues"], list):
        raise ModelError("review returned invalid blocker lists", code="review_protocol")
    if len(verdict["issues"]) > MAX_ISSUES:
        raise ReviewEvidenceError("Review returned too many blockers; provide a bounded verdict without dropping any blocking concern.")
    for item in verdict["issues"]:
        fields = {"kind", "detail", "obligation", "source_path", "pair_ids"}
        if (not isinstance(item, dict) or set(item) != fields or not isinstance(item.get("kind"), str)
                or item["kind"] not in _ISSUE_KINDS
                or any(not isinstance(item.get(key), str) or not item[key].strip()
                       for key in ("detail", "obligation"))
                or not isinstance(item.get("source_path"), str)
                or not isinstance(item.get("pair_ids"), list)
                or any(not isinstance(ref, str) for ref in item["pair_ids"])):
            raise ModelError("review returned an invalid typed blocker", code="review_protocol")
        if (len(item["detail"]) > MAX_TEXT or len(item["obligation"]) > 500
                or len(item["source_path"]) > 256 or len(item["pair_ids"]) > MAX_REFERENCES
                or any(len(ref) > 64 for ref in item["pair_ids"])):
            raise ReviewEvidenceError("Review blocker exceeds the evidence bounds; shorten its wording and references without dropping the concern.")
    return verdict


def _issue_metadata(item: dict, pairs: dict, refs: list[str]) -> dict:
    return {**item, "pair_ids": list(refs),
            "experiments": [{"pair_id": ref, "input": dict(pairs[ref]["input"]),
                             "before_run_id": pairs[ref]["before"]["run_id"],
                             "after_run_id": pairs[ref]["after"]["run_id"],
                             "before_source_hash": pairs[ref]["before"]["source_hash"],
                             "after_source_hash": pairs[ref]["after"]["source_hash"],
                             "before_status": pairs[ref]["before"]["status"],
                             "after_status": pairs[ref]["after"]["status"]} for ref in refs]}


def review_verdict(content: list[dict], source_hash: str, evidence: dict | None = None) -> dict:
    verdict = _native_input(content)
    if verdict["source_hash"] != source_hash:
        raise ReviewEvidenceError("Review decision cited the wrong candidate source hash; use the supplied candidate identity.",
                                  correctable=True)
    pairs = {pair["pair_id"]: pair for pair in (evidence or {}).get("pairs", [])}
    issues, metadata, invalid = [], [], []
    for item in verdict["issues"]:
        refs = item["pair_ids"]
        path = item["source_path"]
        description = f"[{item['kind']}] {path + ': ' if path else ''}{item['obligation']}: {item['detail']}"
        safe_path = (not path or not PurePosixPath(path).is_absolute() and ".." not in PurePosixPath(path).parts
                     and "\\" not in path and path.startswith("src/"))
        if not safe_path or item["kind"] == "source_defect" and not path:
            invalid.append("A source blocker must cite a relative src/ path inside the reviewed candidate.")
            continue
        if len(set(refs)) != len(refs) or any(ref not in pairs for ref in refs):
            invalid.append("A blocker cited an unknown or duplicate experiment pair; use only supplied pair_id values.")
            if item["kind"] == "source_defect":
                # A code/contract argument can be independently blocking even when an
                # additional run citation is wrong. Citation correction cannot erase it.
                known = list(dict.fromkeys(ref for ref in refs if ref in pairs))
                issues.append(description)
                metadata.append({**_issue_metadata(item, pairs, known), "invalid_pair_reference": True})
            continue
        if item["kind"] == "regression" and (not refs or any(
                not (pairs[ref]["facts"]["passing_control_regressed"]
                     or pairs[ref]["facts"]["protected_obligation_regressed"]) for ref in refs)):
            invalid.append("The cited matched pair does not show a passing-control or protected-obligation regression. "
                           "A newly observed failure on an already failing input needs a concrete source/dependency finding, "
                           "not a claim that an unrelated before run passed.")
            continue
        if item["kind"] == "unverified_repair" and not refs:
            invalid.append("An unverified-repair blocker must identify a supplied matched experiment pair.")
            continue
        issues.append(description)
        metadata.append(_issue_metadata(item, pairs, refs))
    if invalid:
        raise ReviewEvidenceError(" ".join(dict.fromkeys(invalid)), correctable=True,
                                  issues=issues, issue_evidence=metadata,
                                  missing_evidence=verdict["missing_evidence"])
    result = {"source_hash": source_hash, "approve": verdict["approve"],
              "issues": issues, "missing_evidence": list(verdict["missing_evidence"])}
    if metadata:
        result["issue_evidence"] = metadata
    if "limitations" in verdict:
        result["limitations"] = list(verdict["limitations"])
    return result
