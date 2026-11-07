"""No-argument entrypoint used by local and image grading."""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import signal

from .controller import Controller
from .model import BudgetExhausted, ModelClient, ModelError
from .prompts import SYSTEM
from .runtime import CaseConfig, ConfigurationError, TimeLimitError, emit
from .tools import TOOL_DEFINITIONS, ToolDispatcher


MAX_TURNS = 128
MAX_CONTEXT_CHARS = 180_000
COMPACT_CONTEXT_CHARS = 120_000
MAX_TOOL_RESULT_CHARS = 32_000
MAX_LEDGER_CHARS = 64_000
INFORMATION_TURN_LIMIT = 6
MAX_HYPOTHESIS_INTERVENTIONS = 4
MAX_PROACTIVE_VALIDATIONS = 4
MAX_PROBE_ATTEMPTS = 4
MAX_PROBE_ATTEMPTS_PER_HASH = 2
MAX_PROBE_INVENTORY_ENTRIES = 512
MAX_PROBE_COMBINATIONS = 64
MIN_FEEDBACK_SECONDS = 30
INFORMATION_TOOLS = frozenset({"status", "list_files", "read_file", "search", "trace_slice"})


def _ledger_text(value, limit: int) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:max(0, limit - 1)] + "…"


def _ledger_scalar(value):
    # Display unusually large case metadata without changing the values used by
    # simulator tools. Integers can be thousands of digits even in valid JSON.
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if value.bit_length() <= 256 else f"[integer omitted: {value.bit_length()} bits]"
    if isinstance(value, float):
        return value if math.isfinite(value) else "[non-finite number omitted]"
    if isinstance(value, str):
        return _ledger_text(value, 64)
    return "[non-scalar value omitted]"


def _ledger_strings(value, *, count: int, chars: int) -> list[str]:
    if not isinstance(value, list):
        return []
    items = [item for item in value if isinstance(item, str)]
    if len(items) > count:
        head = (count + 1) // 2
        tail = count - head
        items = items[:head] + (items[-tail:] if tail else [])
    return [_ledger_text(item, chars) for item in items]


def _prompt_hypotheses(status: dict, source: str | None) -> list[dict]:
    items = [item for item in status.get("hypotheses", []) if isinstance(item, dict)]
    current = [item for item in items if item.get("source_hash") == source][-6:]
    others = [item for item in items if item.get("source_hash") != source]
    slots = 6 - len(current)
    return (others[-slots:] if slots else []) + current


def _ledger_replays(value, *, snapshot: str, limit: int, chars: int,
                    source_hash: str | None = None) -> list[dict]:
    if not isinstance(value, list):
        return []
    output = []
    for item in value[-limit:]:
        if not isinstance(item, dict):
            continue
        row_hash = item.get("source_hash", source_hash)
        row = {
            "snapshot": snapshot, "source_hash": _ledger_text(row_hash or "", 64),
            "run_id": _ledger_text(item.get("run_id", ""), 64),
            "scenario": _ledger_text(item.get("scenario", ""), min(chars, 256)),
            "seed": _ledger_scalar(item.get("seed")), "status": _ledger_text(item.get("status", ""), 32),
            "invariants": sorted({
                _ledger_text(v.get("invariant", "?"), min(chars, 80))
                for v in item.get("violations", []) if isinstance(v, dict)
            })[:8],
        }
        if "scenario_hash" in item:
            row["scenario_hash"] = _ledger_text(item["scenario_hash"] or "", 64)
        if "faults" in item:
            row["faults"] = _ledger_text(item["faults"], min(chars, 256)) if item["faults"] is not None else None
        if item.get("error"):
            row["error"] = _ledger_text(item["error"], min(chars, 300))
        output.append(row)
    return output


def _ledger_recommendations(value) -> list[dict]:
    output = []
    for item in value[:4] if isinstance(value, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("arguments"), dict):
            continue
        arguments = {}
        for key, argument in item["arguments"].items():
            if key == "entities":
                arguments[key] = _ledger_strings(argument, count=4, chars=512)
            elif key in {"scenario", "faults"}:
                arguments[key] = _ledger_text(argument, 256) if argument is not None else None
            elif key in {"source", "run_id"}:
                arguments[key] = _ledger_text(argument, 64)
            elif key in {"seed", "limit", "at", "window_s", "context_lines"}:
                arguments[key] = _ledger_scalar(argument)
        output.append({"tool": _ledger_text(item.get("tool", ""), 32),
                       "source_hash": _ledger_text(item.get("source_hash", ""), 64),
                       "estimated_seconds": _ledger_scalar(item.get("estimated_seconds")),
                       "arguments": arguments})
    return output


def _ledger_control_coverage(value) -> dict:
    fields = ("budget_s", "elapsed_s", "planned", "executed", "retained", "failed",
              "executed_omitted", "planned_omitted", "errors", "budget_exhausted",
              "cancelled", "discovery_truncated")
    return {key: _ledger_scalar(value[key]) for key in fields if key in value} if isinstance(value, dict) else {}


def _ledger_probe_pair(value, *, chars: int = 400, current=None, best=None) -> dict | None:
    if not isinstance(value, dict):
        return None
    output = {key: _ledger_scalar(value[key]) for key in
              ("scope", "mode", "repair_comparison", "source_hash", "accepted_source_hash", "probe", "probe_hash",
               "configuration_hash", "seed", "complete", "estimated_pair_seconds", "estimated_replay_seconds", "stage")
              if key in value}
    # Source labels and run identities survive even the minimal ledger fallback.
    if "probe" in value:
        output["probe"] = _ledger_text(value["probe"], 70)
    output["reason"] = _ledger_text(value.get("reason", ""), chars)
    output["current_source_matches"] = value.get("source_hash") == current
    output["accepted_source_matches"] = value.get("accepted_source_hash") == best
    for key, snapshot in (("before", "accepted"), ("after", "candidate"), ("observation", "accepted")):
        row = value.get(key)
        if isinstance(row, dict):
            output[key] = _ledger_replays([row], snapshot=snapshot, limit=1, chars=max(80, chars))[0]
            output[key]["violation_count"] = _ledger_scalar(row.get("violation_count"))
    return output


def _status_for_prompt(controller: Controller) -> dict:
    status = controller.status()
    status["probe_intervention"] = getattr(controller, "_last_probe_intervention", None)
    return status


def _minimal_prompt_status(status: dict) -> str:
    compact = {key: _ledger_scalar(status.get(key)) for key in
               ("source_hash", "accepted_source_hash", "unaccepted_changes", "seconds_remaining",
                "remaining_budget_usd", "accepted_repairs")}
    compact.update(details_truncated=True, known_failures_source="accepted",
                   known_failures_source_hash=_ledger_scalar(status.get("accepted_source_hash")))
    validation = status.get("last_validation")
    if isinstance(validation, dict) and validation.get("source_hash") == status.get("source_hash"):
        compact["last_validation"] = {key: _ledger_scalar(validation.get(key))
                                      for key in ("source_hash", "accepted", "complete", "stage")}
        compact["last_validation"]["reason"] = _ledger_text(validation.get("reason", ""), 400)
        review = validation.get("review")
        if isinstance(review, dict):
            compact["last_validation"]["review"] = {
                "approve": _ledger_scalar(review.get("approve")),
                "issues": _ledger_strings(review.get("issues"), count=3, chars=200),
                "missing_evidence": _ledger_strings(review.get("missing_evidence"), count=3, chars=200),
            }
    compact["control_coverage"] = _ledger_control_coverage(status.get("control_coverage"))
    compact["probe_intervention"] = _ledger_probe_pair(
        status.get("probe_intervention"), chars=200,
        current=status.get("source_hash"), best=status.get("accepted_source_hash"))
    return json.dumps(compact, ensure_ascii=False)


def prompt_status(controller: Controller) -> str:
    status = _status_for_prompt(controller)
    if controller.deadline.stop_event.is_set() or controller.deadline.remaining() <= 0:
        return _minimal_prompt_status(status)
    current = status.get("source_hash")
    best = status.get("accepted_source_hash")
    # The ledger survives history compaction. Keep the diagnosis, patch and
    # source-bound verdict, rather than requiring another investigation of an
    # already repaired candidate when older tool results are discarded.
    pending_diff = ""
    if status.get("unaccepted_changes"):
        cached = getattr(controller, "_prompt_pending_diff", None)
        if cached is None or cached[:2] != (current, best):
            pending_diff = controller.sources.diff(max_chars=4000)
            controller._prompt_pending_diff = (current, best, pending_diff)
        else:
            pending_diff = cached[2]
    # Reduce details only for exceptionally large/escaped metadata. Each pass
    # retains source identity and the latest verdict; the ledger itself must
    # fit even when all native message pairs have to be removed.
    for detail_chars in (800, 400, 200, 100, 50, 25):
        if controller.deadline.stop_event.is_set() or controller.deadline.remaining() <= 0:
            return _minimal_prompt_status(status)
        fields = ("source_hash", "accepted_source_hash", "unaccepted_changes", "seconds_remaining",
                  "final_reserve", "remaining_budget_usd", "accepted_repairs")
        compact = {key: _ledger_scalar(status[key]) for key in fields if key in status}
        seeds = status.get("public_seeds", [])[:16]
        compact["public_seeds"] = [_ledger_scalar(seed) for seed in seeds]
        compact["details_truncated"] = detail_chars < 800 or compact["public_seeds"] != seeds
        compact["retained_experiment_count"] = _ledger_scalar(status.get("retained_experiment_count", 0))
        compact["experiment_recommendations"] = _ledger_recommendations(status.get("experiment_recommendations"))
        compact["experiment_metrics"] = {key: _ledger_scalar(value) for key, value in
                                         list(status.get("experiment_metrics", {}).items())[:4]}
        compact["control_coverage"] = _ledger_control_coverage(status.get("control_coverage"))
        compact["probe_intervention"] = _ledger_probe_pair(
            status.get("probe_intervention"), chars=detail_chars, current=current, best=best)
        compact["hypotheses"] = [{
            "id": _ledger_text(item.get("id", ""), 64),
            "status": _ledger_text(item.get("status", ""), 20),
            "source_hash": _ledger_text(item.get("source_hash", ""), 64),
            "obligation": _ledger_text(item.get("obligation", ""), min(detail_chars, 400)),
            "mechanism": _ledger_text(item.get("mechanism", ""), detail_chars),
            "evidence": _ledger_strings(item.get("evidence"), count=4, chars=min(detail_chars, 400)),
        } for item in _prompt_hypotheses(status, current)]
        compact["known_failures_source"] = "accepted"
        compact["known_failures_source_hash"] = _ledger_text(status.get("known_failures_source_hash", best) or "", 64)
        compact["known_failures"] = _ledger_replays(
            status.get("known_failures"), snapshot="accepted", limit=6, chars=detail_chars,
            source_hash=status.get("known_failures_source_hash", best))
        candidate_results = [item for item in status.get("candidate_results", [])
                             if isinstance(item, dict) and item.get("source_hash") == current]
        compact["candidate_results"] = _ledger_replays(
            candidate_results, snapshot="candidate", limit=8, chars=detail_chars)
        compact["last_validation"] = None
        validation = status.get("last_validation")
        if isinstance(validation, dict) and validation.get("source_hash") == current:
            latest = {key: _ledger_scalar(validation[key]) for key in
                      ("source_hash", "accepted_source_hash", "accepted", "complete", "attempt", "witness_count")
                      if key in validation}
            latest["closed_invariants"] = _ledger_strings(validation.get("closed_invariants"), count=8,
                                                         chars=min(detail_chars, 80))
            latest.update(stage=_ledger_text(validation.get("stage", ""), 64),
                          reason=_ledger_text(validation.get("reason", ""), detail_chars),
                          proposal_reason=_ledger_text(validation.get("proposal_reason", ""), detail_chars))
            for key, fields in (("tests", ("ok", "returncode", "timed_out", "cancelled", "duration")),
                                ("determinism", ("complete", "ok", "expected", "completed"))):
                section = validation.get(key)
                if isinstance(section, dict):
                    latest[key] = {field: _ledger_scalar(section[field]) for field in fields if field in section}
                    if key == "determinism" and isinstance(section.get("failed_experiment"), dict):
                        failed = section["failed_experiment"]
                        latest[key]["failed_experiment"] = {
                            "source_hash": _ledger_scalar(failed.get("source_hash")),
                            "scenario": _ledger_text(failed.get("scenario", ""), min(detail_chars, 256)),
                            "seed": _ledger_scalar(failed.get("seed")),
                            "faults": _ledger_text(failed["faults"], min(detail_chars, 256)) if failed.get("faults") is not None else None,
                            "ok": _ledger_scalar(failed.get("ok")),
                            "run_ids": _ledger_strings(failed.get("run_ids"), count=4, chars=64),
                        }
            matrix = validation.get("matrix")
            if isinstance(matrix, dict):
                latest["matrix"] = {key: _ledger_scalar(matrix[key]) for key in ("complete", "full_pass") if key in matrix}
                latest["matrix"]["before"] = _ledger_replays(
                    matrix.get("before"), snapshot="previous_accepted", limit=4, chars=detail_chars)
                latest["matrix"]["after"] = _ledger_replays(
                    matrix.get("after"), snapshot="candidate", limit=8, chars=detail_chars)
                counts = matrix.get("counts")
                if isinstance(counts, dict):
                    latest["matrix"]["counts"] = {_ledger_text(key, 64): _ledger_scalar(value) for key, value in list(counts.items())[:8]
                                                   if isinstance(value, (bool, int, float))}
                selection = matrix.get("selection")
                if isinstance(selection, dict):
                    latest["matrix"]["selection"] = {key: _ledger_scalar(value)
                                                      for key, value in list(selection.items())[:6]}
            latest["witnesses"] = _ledger_replays(
                validation.get("witnesses"), snapshot="candidate", limit=4, chars=detail_chars)
            review = validation.get("review")
            if isinstance(review, dict):
                latest["review"] = {"approve": _ledger_scalar(review.get("approve")),
                                    "issues": _ledger_strings(review.get("issues"), count=6, chars=detail_chars),
                                    "missing_evidence": _ledger_strings(review.get("missing_evidence"), count=6, chars=detail_chars)}
                latest["review"].update({key: _ledger_scalar(review[key]) for key in ("issues_count", "missing_evidence_count", "truncated")
                                         if key in review})
                if "skipped" in review:
                    latest["review"]["skipped"] = _ledger_text(review["skipped"], detail_chars)
            compact["last_validation"] = latest
        if pending_diff:
            compact["pending_diff"] = _ledger_text(pending_diff, detail_chars * 5)
        compact["accepted_repair_summaries"] = [{
            "source_hash": _ledger_text(item.get("source_hash", ""), 64),
            "reason": _ledger_text(item.get("reason", ""), min(detail_chars, 400)),
            "closed_invariants": _ledger_strings(item.get("closed_invariants"), count=8, chars=min(detail_chars, 80)),
        } for item in getattr(controller, "accepted", [])[-2:] if isinstance(item, dict)]
        text = json.dumps(compact, ensure_ascii=False)
        if len(text) <= MAX_LEDGER_CHARS:
            return text
    return _minimal_prompt_status(status)


def tool_text(value: dict, limit: int = MAX_TOOL_RESULT_CHARS) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return text
    # Tool results are text blocks; retain explicit truncation and the leading
    # verdict/provenance. Full traces remain accessible by run_id.
    return text[:limit] + "\n[tool result truncated; request a focused trace slice or file range]"


def compact_messages(controller: Controller, messages: list[dict], *, force: bool = False,
                     extra_text: str = "") -> list[dict]:
    expanded = list(messages)
    if extra_text:
        _append_feedback(expanded, extra_text)
    size = len(json.dumps(expanded, ensure_ascii=False))
    if size <= MAX_CONTEXT_CHARS and not force:
        return expanded if extra_text else messages
    # Retain only complete assistant/user pairs, including every native tool
    # result for a retained tool_use block. Static context can shrink to keep
    # the latest finding and its complete native response together.
    pairs = []
    cursor = len(messages) - 1
    while cursor >= 1 and len(pairs) < (2 if force else 4):
        if messages[cursor]["role"] != "user" or messages[cursor - 1]["role"] != "assistant":
            break
        pairs.insert(0, [messages[cursor - 1], messages[cursor]])
        cursor -= 2
    ledger = "# Current repair ledger\n" + prompt_status(controller)
    initial_limit = min(90_000, len(controller.initial_context))
    cancelled = False

    def stopped() -> bool:
        return controller.deadline.stop_event.is_set() or controller.deadline.remaining() <= 0

    def compose(limit: int) -> tuple[list[dict], int]:
        initial = controller.initial_context[:limit]
        if limit < len(controller.initial_context):
            initial += "\n[initial context truncated; request needed files/contracts with tools]"
        result = [{"role": "user", "content": initial}]
        for pair in pairs:
            result.extend(dict(message) for message in pair)
        if extra_text:
            _append_feedback(result, extra_text)
        last = result[-1]
        content = last["content"]
        last["content"] = [*content, {"type": "text", "text": ledger}] if isinstance(content, list) else content + "\n" + ledger
        return result, len(json.dumps(result, ensure_ascii=False))

    def finish(result: list[dict], final_size: int) -> list[dict]:
        emit("context_compacted", previous_chars=size, final_chars=final_size, retained_pairs=len(pairs),
             target_chars=COMPACT_CONTEXT_CHARS, target_met=final_size <= COMPACT_CONTEXT_CHARS,
             initial_chars=initial_limit, cancelled=cancelled)
        return result

    # Drop older pairs for headroom, then shrink static orientation before
    # considering the latest pair. Fresh reasoning must survive compaction.
    for _ in range(len(pairs) + 1):
        if stopped():
            cancelled = True
            pairs.clear()
            initial_limit = 0
        result, final_size = compose(initial_limit)
        if final_size <= COMPACT_CONTEXT_CHARS or len(pairs) <= 1:
            break
        pairs.pop(0)
    if final_size <= COMPACT_CONTEXT_CHARS:
        return finish(result, final_size)

    # The latest complete pair and source ledger may need more than the soft
    # target. Preserve them whenever their minimum envelope fits the hard cap.
    # Two bounded passes handle an oversized last pair and unusual escaping.
    for _ in range(2):
        _, minimum_size = compose(0)
        target = COMPACT_CONTEXT_CHARS if minimum_size <= COMPACT_CONTEXT_CHARS else MAX_CONTEXT_CHARS
        for initial_limit in (90_000, 64_000, 48_000, 32_000, 16_000, 8_000, 4_000, 1_000, 0):
            initial_limit = min(initial_limit, len(controller.initial_context))
            if stopped():
                cancelled = True
                pairs.clear()
                initial_limit = 0
            result, final_size = compose(initial_limit)
            if final_size <= target:
                return finish(result, final_size)
        if not pairs:
            break
        pairs.clear()  # Only a pair that cannot fit the absolute cap is lost.

    # The normal ledger is bounded before outer serialization. Keep a final
    # source/verdict fallback for unusual escaped metadata or changed limits.
    ledger = "# Current repair ledger\n" + _minimal_prompt_status(_status_for_prompt(controller))
    initial_limit = 0
    result, final_size = compose(initial_limit)
    if final_size > MAX_CONTEXT_CHARS:
        raise TimeLimitError("repair ledger exceeds the absolute context limit")
    return finish(result, final_size)


def _append_feedback(messages: list[dict], text: str) -> None:
    """Append local-controller feedback without splitting native tool pairs."""
    if messages and messages[-1]["role"] == "user":
        message = dict(messages[-1])
        content = message["content"]
        message["content"] = [*content, {"type": "text", "text": text}] if isinstance(content, list) else content + "\n" + text
        messages[-1] = message
    else:
        messages.append({"role": "user", "content": text})


def _validation_seen(status: dict, source: str, validated_hashes: set[str]) -> bool:
    latest = status.get("last_validation")
    if isinstance(latest, dict) and latest.get("source_hash") == source and latest.get("complete") is True:
        validated_hashes.add(source)
    return source in validated_hashes


def _durable_hypothesis(status: dict, source: str) -> bool:
    return any(isinstance(item, dict) and item.get("source_hash") == source
               and isinstance(item.get("mechanism"), str) and item["mechanism"].strip()
               and isinstance(item.get("evidence"), list)
               and any(isinstance(value, str) and value.strip() for value in item["evidence"])
               for item in _prompt_hypotheses(status, source))


def _can_validate_with_feedback(controller: Controller) -> bool:
    installation = controller.test_seconds * 2 + controller.replay_seconds * 3 + 15
    return (not controller.deadline.stop_event.is_set()
            and controller.deadline.remaining() >= max(controller.final_reserve, installation) + MIN_FEEDBACK_SECONDS)


def _validate_stalled_candidate(controller: Controller, validated_hashes: set[str]) -> dict:
    candidate = controller.sources.candidate_hash
    # Mark before entering expensive gates. An interrupted attempt is bounded,
    # and completed rejected versions never get an automatic second review.
    validated_hashes.add(candidate)
    emit("progress_intervention", kind="validate_candidate", source_hash=candidate)
    try:
        result = controller.validate({"reason": "validate unchanged staged repair and identify remaining local evidence gaps",
                                      "target_invariants": [], "reproductions": []})
    except (TimeLimitError, ModelError, ValueError, OSError) as error:
        result = {"accepted": False, "reason": str(error)[:2000]}
    emit("progress_validation", source_hash=candidate, accepted=result.get("accepted", False),
         reason=result.get("reason"), seconds_remaining=round(controller.deadline.remaining(), 2))
    return result


def _completed_rejection(status: dict, source: str) -> dict | None:
    latest = status.get("last_validation")
    if (isinstance(latest, dict) and latest.get("source_hash") == source
            and latest.get("complete") is True and latest.get("accepted") is False):
        return latest
    return None


def _probe_slot_seconds(controller: Controller) -> float:
    measured = [item.get("worst_seconds") for item in getattr(controller, "_experiment_timings", {}).values()
                if isinstance(item, dict) and item.get("count", 0) > 0]
    measured = [value for value in measured if isinstance(value, (int, float))
                and not isinstance(value, bool) and math.isfinite(value) and value > 0]
    if not measured:
        return 60.0
    # New compositions may cost more than the existing traces. The estimate
    # controls reservations, while replay's absolute 60s limit still applies.
    return min(60.0, max(5.0, max(max(measured), controller.replay_seconds) * 2 + 2))


def _probe_resources(controller: Controller, model, seconds: float) -> bool:
    balance = getattr(model, "remaining_budget", controller.config.budget_usd)
    return (not controller.deadline.stop_event.is_set()
            and isinstance(balance, (int, float)) and not isinstance(balance, bool)
            and math.isfinite(balance) and balance > 0
            and controller.deadline.remaining() >= controller.final_reserve + MIN_FEEDBACK_SECONDS + seconds + 2)


class _ProbeConfigReader(ToolDispatcher):
    """Memoize protected input reads; refresh the newly created probe itself."""
    def __init__(self, controller):
        super().__init__(controller)
        self.reads = [0]
        self.fresh_reads = [0]
        self.fresh_paths = set()
        self.documents = {}

    def _composition_document(self, path, reference, reads):
        if path not in self.documents:
            if path in self.fresh_paths and self.fresh_reads[0] >= 2:
                raise ValueError("fresh probe verification exceeds its read limit")
            budget = self.fresh_reads if path in self.fresh_paths else self.reads
            self.documents[path] = super()._composition_document(path, reference, budget)
        document, metadata = deepcopy(self.documents[path])
        if any(key in document for key in ("include", "extends", "$include")):
            raise ValueError("effective configuration includes are unsupported")
        metadata["reference"] = reference
        return document, metadata


def _probe_fingerprint(reader: _ProbeConfigReader, path: Path, *, expected_hash=None, faults_path=None) -> str:
    controller = reader.controller
    if controller.deadline.stop_event.is_set():
        raise TimeLimitError("cancelled before reading probe inputs")
    if expected_hash is not None:
        reader.documents.pop(path, None)
        reader.fresh_paths.add(path)
    document, metadata = reader._composition_document(path, path.name, reader.reads)
    if expected_hash is not None and metadata["sha256"] != expected_hash:
        raise ValueError("immutable probe bytes changed after composition")
    if any(key in document for key in ("include", "extends", "$include")):
        raise ValueError("effective configuration includes are unsupported")
    if faults_path is not None:
        profile, _ = reader._composition_document(faults_path, faults_path.name, reader.reads)
        document["faults"] = reader._inline_composition_faults(
            {"faults": profile.get("faults", profile)}, faults_path, reader.reads, [])["faults"]
    document = reader._inline_composition_faults(document, path, reader.reads, [])
    document.pop("name", None)
    document.pop("expect", None)
    # Both paired replays supply an explicit CLI seed; the document default
    # cannot make a renamed experiment novel under that execution policy.
    document.pop("seed", None)
    if document.get("faults") in (None, {}):
        document.pop("faults", None)
    if controller.deadline.stop_event.is_set():
        raise TimeLimitError("cancelled while reading probe inputs")
    payload = json.dumps(document, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _probe_fingerprints(dispatcher: ToolDispatcher) -> tuple[set[str], _ProbeConfigReader]:
    """Compare complete bounded inventories; decline instead of skipping inputs.

    Compose's parser limits protected reads, including referenced faults, to
    32 files of 64KiB. Two fresh probe verification reads have their own budget.
    JSON structure is bounded and no service source is imported here.
    """
    controller = dispatcher.controller
    pending = [controller.sources.candidate / "scenarios", dispatcher._probe_root()]
    paths = []
    entries_seen = 0
    while pending:
        if controller.deadline.stop_event.is_set() or controller.deadline.remaining() <= 0:
            raise TimeLimitError("deadline prevents inspecting probe inventory")
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                entries_seen += 1
                if entries_seen > MAX_PROBE_INVENTORY_ENTRIES:
                    raise ValueError("probe novelty inventory exceeds its entry limit")
                if entry.is_symlink():
                    raise ValueError("probe novelty inventory contains a symlink")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif Path(entry.name).suffix.lower() in {".yaml", ".yml", ".json"}:
                    paths.append(Path(entry.path))
                    if len(paths) > 32:
                        raise ValueError("probe novelty inventory exceeds its file limit")
    reader = _ProbeConfigReader(controller)
    fingerprints = {_probe_fingerprint(reader, path) for path in sorted(paths)}
    # Healthy controls and previous probes may have paired a scenario with an
    # external override. Comparing their files independently misses that exact
    # effective combination, even when the original experiment was retained.
    experiments = [*controller.runs.values(), *getattr(controller, "_cache", {}).values()]
    combinations = {(item.scenario, item.faults) for item in experiments if item.faults is not None}
    combinations.update((item["scenario"], item["faults"]) for item in
                        getattr(controller, "retained_experiments", {}).values() if item.get("faults") is not None)
    if len(combinations) > MAX_PROBE_COMBINATIONS:
        raise ValueError("probe novelty combinations exceed their limit")
    for scenario, faults in sorted(combinations):
        path, profile = reader._composition_path(scenario), reader._composition_path(faults)
        fingerprints.add(_probe_fingerprint(reader, path, faults_path=profile))
    return fingerprints, reader


def _probe_run_row(result) -> dict:
    return {
        "run_id": _ledger_text(result.run_id, 64), "source_hash": _ledger_text(result.source_hash, 64),
        "scenario": _ledger_text(result.scenario, 70), "scenario_hash": _ledger_text(result.scenario_hash, 64),
        "seed": _ledger_scalar(result.seed), "status": _ledger_text(result.status, 32),
        "violation_count": len(result.violations), "error": _ledger_text(result.error or "", 300),
        "violations": [{"invariant": _ledger_text(row.get("invariant", "?"), 80),
                        "detail": _ledger_text(row.get("detail", ""), 200), "t": _ledger_scalar(row.get("t"))}
                       for row in result.violations[:4] if isinstance(row, dict)],
    }


def _measure_probe(controller: Controller, model, dispatcher: ToolDispatcher, result: dict,
                   fingerprints: set[str], reader: _ProbeConfigReader, source: str, best: str,
                   slot: float) -> dict:
    pair = {"scope": "local_diagnostic", "source_hash": source, "accepted_source_hash": best,
            "complete": False, "stage": "composition", "estimated_pair_seconds": round(slot * 2, 3)}
    controller._last_probe_intervention = pair
    try:
        if not result.get("ok"):
            raise ValueError(result.get("error", {}).get("message", "probe composition failed"))
        probe = result["result"]
        pair.update(probe=probe["probe"], probe_hash=probe["probe_hash"], seed=controller.config.seeds[0])
        path = dispatcher._composition_path(probe["probe"])
        pair["configuration_hash"] = _probe_fingerprint(reader, path, expected_hash=probe["probe_hash"])
        if pair["configuration_hash"] in fingerprints:
            raise ValueError("probe repeats an existing effective configuration; name/expect changes add no runtime evidence")

        def ready(seconds: float) -> None:
            if controller.sources.candidate_hash != source or controller.sources.best_hash != best:
                raise ValueError("source snapshots changed before completing the diagnostic pair")
            if not _probe_resources(controller, model, seconds):
                raise TimeLimitError("budget, cancellation or deadline prevents completing the diagnostic pair with feedback")

        ready(slot * 2)
        pair["stage"] = "accepted_replay"
        before = controller.replay("accepted", probe["probe"], controller.config.seeds[0],
                                   reserve=controller.final_reserve + MIN_FEEDBACK_SECONDS + slot)
        pair["before"] = _probe_run_row(before)
        if (before.source_hash != best or before.scenario_hash is None
                or before.status not in {"pass", "invariant_failure", "unresolved"}):
            raise ValueError("accepted diagnostic replay is incomplete or has mismatched provenance")
        # A slow first replay updates the conservative estimate. Keep the
        # completed first result if time or resources prevent the second.
        ready(_probe_slot_seconds(controller))
        pair["stage"] = "candidate_replay"
        after = controller.replay("candidate", probe["probe"], controller.config.seeds[0],
                                  reserve=controller.final_reserve + MIN_FEEDBACK_SECONDS)
        pair["after"] = _probe_run_row(after)
        if (before.source_hash != best or after.source_hash != source
                or before.scenario_hash is None or before.scenario_hash != after.scenario_hash
                or before.faults_hash != after.faults_hash or before.seed != after.seed
                or _probe_fingerprint(reader, path, expected_hash=probe["probe_hash"]) != pair["configuration_hash"]):
            raise ValueError("diagnostic source or effective input provenance does not match")
        if before.status not in {"pass", "invariant_failure", "unresolved"} or after.status not in {
                "pass", "invariant_failure", "unresolved"}:
            raise ValueError("diagnostic pair contains an incomplete or invalid replay")
        pair.update(complete=True, stage="measured", reason="new local diagnostic evidence; prior validation rejection remains recorded")
    except (TimeLimitError, ModelError, ValueError, OSError, RuntimeError, KeyError) as error:
        pair["reason"] = _ledger_text(str(error), 800)
    emit("progress_probe_pair", **_ledger_probe_pair(pair, current=source, best=best))
    return pair


def _measure_single_probe(controller: Controller, model, dispatcher: ToolDispatcher, result: dict,
                          fingerprints: set[str], reader: _ProbeConfigReader, source: str, slot: float) -> dict:
    observation = {"scope": "local_single_source_diagnostic", "mode": "single_source", "repair_comparison": False,
                   "source_hash": source, "accepted_source_hash": source, "complete": False,
                   "stage": "composition", "estimated_replay_seconds": round(slot, 3)}
    controller._last_probe_intervention = observation
    try:
        if not result.get("ok"):
            raise ValueError(result.get("error", {}).get("message", "probe composition failed"))
        probe = result["result"]
        seed = controller.config.seeds[0]
        observation.update(probe=probe["probe"], probe_hash=probe["probe_hash"], seed=seed)
        path = dispatcher._composition_path(probe["probe"])
        observation["configuration_hash"] = _probe_fingerprint(reader, path, expected_hash=probe["probe_hash"])
        if observation["configuration_hash"] in fingerprints:
            raise ValueError("probe repeats an existing effective configuration; name/expect changes add no runtime evidence")

        def ready(seconds: float) -> None:
            if controller.sources.candidate_hash != source or controller.sources.best_hash != source:
                raise ValueError("source snapshots changed before completing the single-source diagnostic")
            if not _probe_resources(controller, model, seconds):
                raise TimeLimitError("budget, cancellation or deadline prevents completing the single-source diagnostic with feedback")

        # Simulator's input identity also binds effective faults. The raw
        # advertised bytes are checked separately before and after replay;
        # compose_probe has already inlined faults and computed this identity.
        expected_input = (probe.get("scenario_hash"), None)
        if not isinstance(expected_input[0], str) or len(expected_input[0]) != 64:
            raise ValueError("single-source diagnostic input identity is unavailable")
        ready(slot)
        observation["stage"] = "accepted_replay"
        run = controller.replay("accepted", probe["probe"], seed,
                                reserve=controller.final_reserve + MIN_FEEDBACK_SECONDS)
        observation["observation"] = _probe_run_row(run)
        ready(0)
        if (run.source_hash != source or (run.scenario_hash, run.faults_hash) != expected_input or run.seed != seed
                or _probe_fingerprint(reader, path, expected_hash=probe["probe_hash"]) != observation["configuration_hash"]):
            raise ValueError("single-source diagnostic source or effective input provenance does not match")
        if run.status not in {"pass", "invariant_failure", "unresolved"}:
            raise ValueError("single-source diagnostic replay is incomplete or invalid")
        observation.update(complete=True, stage="measured",
                           reason="new local observation of accepted source only; no repair comparison, validation or promotion")
    except (TimeLimitError, ModelError, ValueError, OSError, RuntimeError, KeyError, TypeError) as error:
        observation["reason"] = _ledger_text(str(error), 800)
    emit("progress_single_probe", **_ledger_probe_pair(observation, current=source, best=source))
    return observation


def repair_loop(controller: Controller, model: ModelClient) -> None:
    controller.model = model
    validated_hashes = getattr(controller, "_validated_candidate_hashes", set())
    controller._validated_candidate_hashes = validated_hashes
    dispatcher = ToolDispatcher(controller)
    messages = [{"role": "user", "content": controller.initial_context}]
    output_tokens = 4096
    budget_recoveries = 0
    incomplete_recoveries = 0
    completion_attempts = 0
    attempted_turns = 0
    model_turns = 0
    information_turns = 0
    observed_source = controller.sources.candidate_hash
    observed_accepted = controller.sources.best_hash
    forced_hypothesis_hashes: set[str] = set()
    forced_hypothesis_attempts = 0
    probe_attempts = getattr(controller, "_probe_intervention_attempts", {})
    completed_probes = getattr(controller, "_completed_probe_interventions", set())
    completed_single_probes = getattr(controller, "_completed_single_probe_interventions", set())
    declined_probes = getattr(controller, "_declined_probe_interventions", set())
    controller._probe_intervention_attempts = probe_attempts
    controller._completed_probe_interventions = completed_probes
    controller._completed_single_probe_interventions = completed_single_probes
    controller._declined_probe_interventions = declined_probes
    proactive_validations = 0
    remaining_budget = getattr(model, "remaining_budget", None)

    def stop(reason: str, *, code: str | None = None) -> None:
        balance = getattr(model, "remaining_budget", remaining_budget)
        emit("repair_loop_stopped", reason=reason, code=code, attempted_turns=attempted_turns,
             model_turns=model_turns, seconds_remaining=round(controller.deadline.remaining(), 2),
             final_reserve=controller.final_reserve, remaining_budget_usd=_ledger_scalar(balance),
             model_spent_usd=_ledger_scalar(getattr(model, "spent_usd", 0)),
             forced_hypotheses=len(forced_hypothesis_hashes), forced_hypothesis_attempts=forced_hypothesis_attempts,
             proactive_validations=proactive_validations, forced_probe_attempts=sum(probe_attempts.values()),
             completed_probe_pairs=len(completed_probes - completed_single_probes),
             completed_single_diagnostics=len(completed_single_probes))

    def accepted_progress() -> None:
        nonlocal completion_attempts, observed_accepted
        accepted = controller.sources.best_hash
        if accepted != observed_accepted:
            # Only the controller's verified promotion resets unchanged endings.
            # Staging edits or recording a hypothesis does not restart the bound.
            completion_attempts = 0
            observed_accepted = accepted

    for turn in range(MAX_TURNS):
        if controller.deadline.stop_event.is_set():
            stop("cancelled")
            return
        if controller.deadline.remaining(controller.final_reserve) < 2:
            stop("final_reserve")
            return
        accepted_progress()
        current = controller.sources.candidate_hash
        if current != observed_source:
            observed_source = current
            information_turns = 0
        forced_hypothesis = False
        forced_probe = False
        single_probe = False
        forced_instruction = ""
        probe_fingerprints: set[str] = set()
        probe_reads = [0]
        probe_slot = _probe_slot_seconds(controller)
        probe_best = controller.sources.best_hash
        if information_turns >= INFORMATION_TURN_LIMIT:
            status = controller.status()
            seen = _validation_seen(status, current, validated_hashes)
            rejection = _completed_rejection(status, current)
            if (current != controller.sources.best_hash and not seen
                    and proactive_validations < MAX_PROACTIVE_VALIDATIONS
                    and _can_validate_with_feedback(controller)):
                proactive_validations += 1
                outcome = _validate_stalled_candidate(controller, validated_hashes)
                _append_feedback(messages, "# Local candidate validation feedback\n" + tool_text({
                    "source_hash": current, "scope": "local_controller_validation", "validation": outcome,
                }) + "\n# Resource status\n" + prompt_status(controller))
                information_turns = 0
                observed_source = controller.sources.candidate_hash
            elif (current != probe_best and rejection is not None and current not in completed_probes
                    and current not in declined_probes
                    and probe_attempts.get(current, 0) < MAX_PROBE_ATTEMPTS_PER_HASH
                    and sum(probe_attempts.values()) < MAX_PROBE_ATTEMPTS
                    and _probe_resources(controller, model, probe_slot * 2)):
                try:
                    probe_fingerprints, probe_reads = _probe_fingerprints(dispatcher)
                    forced_probe = True
                    gap = rejection.get("review") or {}
                    forced_instruction = (
                        "# Produce a measured diagnostic probe\n"
                        "Source " + current + " has a completed rejection and six information-only turns. "
                        "Use exactly one compose_probe call to test the recorded evidence gap using the documented schema. "
                        "Choose a shipped/scratch base and one small discriminating configuration change. "
                        "A renamed copy or an expect-only change is not a new experiment. "
                        "The controller will replay identical immutable inputs on accepted source " + probe_best
                        + " and this candidate and return measured evidence. No repair is accepted by this experiment. "
                        "Recorded rejection: " + _ledger_text(rejection.get("reason", ""), 400)
                        + "\nIssues/gaps: " + json.dumps({
                            "issues": _ledger_strings(gap.get("issues"), count=3, chars=300),
                            "missing_evidence": _ledger_strings(gap.get("missing_evidence"), count=3, chars=300),
                        }, ensure_ascii=False))
                    emit("progress_intervention", kind="compose_probe", source_hash=current,
                         information_turns=information_turns, estimated_pair_seconds=round(probe_slot * 2, 3))
                except (TimeLimitError, ValueError, OSError, RuntimeError) as error:
                    declined_probes.add(current)
                    _append_feedback(messages, "# Diagnostic intervention declined\n" + _ledger_text(str(error), 800))
                    emit("progress_probe_declined", source_hash=current, reason=_ledger_text(str(error), 800))
            elif (current == probe_best and _durable_hypothesis(status, current)
                    and any(isinstance(row, dict) and row.get("source_hash") == current
                            for row in status.get("known_failures", []))
                    and current not in completed_probes and current not in declined_probes
                    and probe_attempts.get(current, 0) < MAX_PROBE_ATTEMPTS_PER_HASH
                    and sum(probe_attempts.values()) < MAX_PROBE_ATTEMPTS
                    and _probe_resources(controller, model, probe_slot)):
                try:
                    probe_fingerprints, probe_reads = _probe_fingerprints(dispatcher)
                    forced_probe = single_probe = True
                    findings = [{"id": _ledger_text(item.get("id", ""), 64),
                                 "mechanism": _ledger_text(item.get("mechanism", ""), 600),
                                 "evidence": _ledger_strings(item.get("evidence"), count=3, chars=250)}
                                for item in _prompt_hypotheses(status, current)[:3]]
                    forced_instruction = (
                        "# Produce a measured diagnostic probe\n# Single-source diagnostic\n"
                        "Accepted source " + current + " still has known failures and six additional information-only turns "
                        "after a durable finding. Use exactly one compose_probe call with the documented schema to "
                        "discriminate a supported hypothesis or falsify an uncertain claim. Choose a shipped/scratch base "
                        "and one small runtime configuration change. Name/expect/embedded-seed changes alone add no evidence. "
                        "The controller will replay this immutable input ONCE on accepted source only. "
                        "This is a single-source observation with no repair comparison, validation or promotion. "
                        "After receiving the observation, update the diagnostic hypothesis with run_id/source_hash evidence, "
                        "including falsified claims and remaining uncertainty. Preserve verified repair facts.\n"
                        "Current findings: " + json.dumps(findings, ensure_ascii=False)
                        + "\nAccepted-source failures: " + json.dumps(_ledger_replays(
                            status.get("known_failures"), snapshot="accepted", limit=3, chars=250, source_hash=current),
                            ensure_ascii=False))
                    emit("progress_intervention", kind="compose_probe", mode="single_source", source_hash=current,
                         information_turns=information_turns, estimated_replay_seconds=round(probe_slot, 3))
                except (TimeLimitError, ValueError, OSError, RuntimeError) as error:
                    declined_probes.add(current)
                    _append_feedback(messages, "# Single-source diagnostic intervention declined\n" + _ledger_text(str(error), 800))
                    emit("progress_probe_declined", mode="single_source", source_hash=current, reason=_ledger_text(str(error), 800))
            elif (not _durable_hypothesis(status, current)
                    and current not in forced_hypothesis_hashes
                    and forced_hypothesis_attempts < MAX_HYPOTHESIS_INTERVENTIONS
                    and controller.deadline.remaining(controller.final_reserve) >= MIN_FEEDBACK_SECONDS):
                forced_hypothesis = True
                forced_instruction = (
                    "# Preserve the current finding\n"
                    "Use record_hypothesis once before more retrieval. Record one concise hypothesis for source_hash="
                    + current + ". State the current contract obligation, supported or uncertain causal mechanism, "
                    "and decisive file/line or run_id/source_hash evidence. Include any falsified suspicion and "
                    "the next unresolved experiment; label unknowns explicitly. Keep obligation under 400 characters, "
                    "mechanism under 800, and at most 4 short evidence items. Update an existing id where useful. "
                    "Do not invent evidence or claim a repair from aggregate violation counts.")
                emit("progress_intervention", kind="record_hypothesis", source_hash=current,
                     information_turns=information_turns)
        attempted_turns = turn + 1
        try:
            messages = compact_messages(controller, messages, extra_text=forced_instruction)
            if controller.deadline.stop_event.is_set():
                stop("cancelled")
                return
            kwargs = {"max_tokens": 2048 if forced_hypothesis or forced_probe else output_tokens,
                      "reserve": controller.final_reserve + (
                          MIN_FEEDBACK_SECONDS + probe_slot * (1 if single_probe else 2) if forced_probe else 0)}
            definitions = TOOL_DEFINITIONS
            if forced_hypothesis:
                kwargs["tool_choice"] = {"type": "tool", "name": "record_hypothesis"}
                # A failed request leaves the finding pending for the existing
                # bounded recovery. Count attempts independently so paid retries
                # cannot exceed the case intervention budget.
                forced_hypothesis_attempts += 1
            elif forced_probe:
                if not _probe_resources(controller, model, probe_slot * (1 if single_probe else 2)):
                    stop("diagnostic_resources")
                    return
                kwargs["tool_choice"] = {"type": "tool", "name": "compose_probe"}
                definitions = [tool for tool in TOOL_DEFINITIONS if tool["name"] == "compose_probe"]
                probe_attempts[current] = probe_attempts.get(current, 0) + 1
            reply = model.messages(SYSTEM, messages, definitions, **kwargs)
        except BudgetExhausted as error:
            emit("model_budget_limit", error=str(error))
            if budget_recoveries >= 1:
                stop("model_budget_limit")
                return
            budget_recoveries += 1
            messages = compact_messages(controller, messages, force=True)
            output_tokens = 2048
            continue
        except TimeLimitError:
            stop("deadline")
            return
        except ModelError as error:
            emit("model_error", code=error.code, status=error.status, error=str(error))
            if error.code == "incomplete_response" and incomplete_recoveries < 2:
                incomplete_recoveries += 1
                if not forced_hypothesis and not forced_probe:
                    output_tokens = min(8192, output_tokens * 2)
                continue
            stop("model_error", code=error.code)
            return
        if forced_hypothesis:
            forced_hypothesis_hashes.add(current)
        model_turns += 1
        remaining_budget = reply.remaining_usd
        emit("model_turn", turn=turn + 1, cost_usd=reply.cost_usd,
             remaining_budget_usd=reply.remaining_usd, stop_reason=reply.stop_reason,
             forced_tool="compose_probe" if forced_probe else "record_hypothesis" if forced_hypothesis else None)
        if controller.deadline.stop_event.is_set():
            stop("cancelled")
            return
        messages.append({"role": "assistant", "content": reply.content})
        calls = [block for block in reply.content if block["type"] == "tool_use"]
        for block in reply.content:
            if block["type"] == "text":
                emit("agent_note", text=block["text"][:4000])
        if forced_probe:
            # A forced diagnostic cannot execute source edits, validation, or
            # unrelated calls, even if the provider returns an unexpected block.
            valid = len(calls) == 1 and calls[0]["name"] == "compose_probe"
            results = []
            if valid:
                call = calls[0]
                if (controller.sources.candidate_hash != current or controller.sources.best_hash != probe_best
                        or not _probe_resources(controller, model, probe_slot * (1 if single_probe else 2))):
                    result = {"ok": False, "error": {"code": "diagnostic_resources",
                              "message": "source, budget, cancellation or deadline prevents composing a diagnostic"}}
                else:
                    result = dispatcher.execute("compose_probe", call["input"])
                emit("tool", name="compose_probe", ok=result.get("ok"), source_hash=current)
                results.append({"type": "tool_result", "tool_use_id": call["id"],
                                "content": tool_text(result), "is_error": not result.get("ok", False)})
                pair = (_measure_single_probe(controller, model, dispatcher, result, probe_fingerprints,
                                              probe_reads, current, probe_slot) if single_probe else
                        _measure_probe(controller, model, dispatcher, result, probe_fingerprints,
                                       probe_reads, current, probe_best, probe_slot))
                if pair["complete"]:
                    completed_probes.add(current)
                    if single_probe:
                        completed_single_probes.add(current)
                feedback = ("# Measured local single-source observation\n" if single_probe else
                            "# Measured local diagnostic pair\n") + tool_text(_ledger_probe_pair(
                    pair, current=controller.sources.candidate_hash, best=controller.sources.best_hash))
                if single_probe:
                    feedback += ("\nUpdate the current diagnostic hypothesis with this run/source evidence and any falsified claims. "
                                 "This observation does not compare repair versions or authorize source promotion.")
                information_turns = 0 if pair["complete"] else INFORMATION_TURN_LIMIT
            else:
                error = {"ok": False, "error": {"code": "unexpected_diagnostic_call",
                         "message": "forced diagnostic requires exactly one compose_probe call; no calls were executed"}}
                results = [{"type": "tool_result", "tool_use_id": call["id"],
                            "content": tool_text(error), "is_error": True} for call in calls]
                feedback = "# Diagnostic request rejected\n" + tool_text(error)
                information_turns = INFORMATION_TURN_LIMIT
                emit("progress_probe_declined", source_hash=current, reason="unexpected diagnostic calls")
            results.append({"type": "text", "text": feedback + "\n# Resource status\n" + prompt_status(controller)})
            messages.append({"role": "user", "content": results})
            observed_source = controller.sources.candidate_hash
            continue
        if calls:
            results = []
            information_only = True
            for call in calls:
                if controller.deadline.remaining() <= 0 or controller.deadline.stop_event.is_set():
                    stop("cancelled" if controller.deadline.stop_event.is_set() else "deadline")
                    return
                validating_hash = controller.sources.candidate_hash if call["name"] == "validate" else None
                recorded_runs = set(controller.runs) if call["name"] == "replay" else None
                result = dispatcher.execute(call["name"], call["input"])
                # Re-reading a cached trace is retrieval, even when the model
                # requests another alias for the same input. A real replay or
                # explicit repeat registers a fresh controller-owned run ID.
                fresh_replay = recorded_runs is not None and any(run_id not in recorded_runs for run_id in controller.runs)
                information_only &= (call["name"] in INFORMATION_TOOLS
                                     or call["name"] == "replay" and not fresh_replay)
                if validating_hash is not None and result.get("ok"):
                    validated_hashes.add(validating_hash)
                emit("tool", name=call["name"], ok=result.get("ok"),
                     source_hash=controller.sources.candidate_hash,
                     **({"fresh_replay": fresh_replay} if recorded_runs is not None else {}))
                results.append({"type": "tool_result", "tool_use_id": call["id"],
                                "content": tool_text(result), "is_error": not result.get("ok", False)})
            after_source = controller.sources.candidate_hash
            if information_only and after_source == observed_source:
                information_turns += 1
            else:
                information_turns = 0
            observed_source = after_source
            results.append({"type": "text", "text": "# Resource status\n" + prompt_status(controller)})
            messages.append({"role": "user", "content": results})
            continue
        information_turns = 0
        # The controller verifies a pending patch even if the model ends its
        # conversation without explicitly invoking the validation tool.
        pending = controller.sources.candidate_hash != controller.sources.best_hash
        validation = None
        if pending:
            validating_hash = controller.sources.candidate_hash
            if _validation_seen(controller.status(), validating_hash, validated_hashes):
                validation = controller.status().get("last_validation")
                if not isinstance(validation, dict) or validation.get("source_hash") != validating_hash:
                    validation = {"accepted": False, "source_hash": validating_hash,
                                  "reason": "unchanged candidate was already validated; request explicit validation after new evidence"}
                emit("automatic_validation_skipped", source_hash=validating_hash,
                     reason="source version already validated")
            else:
                try:
                    validation = controller.validate({"reason": "finalize proposed repair", "target_invariants": []})
                    validated_hashes.add(validating_hash)
                except (ValueError, OSError, TimeLimitError) as error:
                    validation = {"accepted": False, "error": str(error)}
        status = controller.status()
        accepted_progress()
        if completion_attempts >= 2:
            stop("completion_limit")
            return
        if not pending and not status["known_failures"]:
            stop("complete")
            return
        completion_attempts += 1
        messages.append({"role": "user", "content": "Continue diagnosing the remaining supported failures/ticket items. "
                         "Read current source where earlier excerpts have changed. Build a targeted probe if the current evidence "
                         "does not establish a repair.\n" + tool_text({"validation": validation}) + "\n" + prompt_status(controller)})
    stop("turn_limit")


def validate_pending_candidate(controller: Controller) -> dict | None:
    """Give one unvalidated source version a final controller-gated opportunity.

    The repair loop may stop between staging and validation. The controller owns
    the local evidence, review policy, deadline limits, and recoverable promotion.
    Previously rejected versions are not retried when review becomes unavailable.
    """
    if controller.deadline.stop_event.is_set():
        emit("final_candidate_validation_skipped", reason="cancelled")
        return None
    candidate = controller.sources.candidate_hash
    if candidate == controller.sources.best_hash:
        return None
    validated_hashes = getattr(controller, "_validated_candidate_hashes", set())
    controller._validated_candidate_hashes = validated_hashes
    if _validation_seen(controller.status(), candidate, validated_hashes):
        emit("final_candidate_validation_skipped", reason="source version already validated",
             source_hash=candidate)
        return None
    required = controller.test_seconds * 2 + controller.replay_seconds * 3 + 15
    remaining = controller.deadline.remaining()
    if remaining < required:
        emit("final_candidate_validation_skipped", reason="insufficient installation reserve",
             seconds_remaining=round(remaining, 2), required_seconds=round(required, 2))
        return None
    # Record the attempt before entering any expensive gate: this helper must
    # not repeat model review for the same version if a caller invokes it again.
    validated_hashes.add(candidate)
    emit("final_candidate_validation_started", source_hash=candidate)
    try:
        result = controller.validate({"reason": "validate final staged repair",
                                      "target_invariants": [], "reproductions": []})
    except (TimeLimitError, ModelError, ValueError, OSError) as error:
        result = {"accepted": False, "reason": str(error)[:2000]}
    emit("final_candidate_validation", accepted=result.get("accepted", False),
         reason=result.get("reason"), source_hash=controller.sources.best_hash)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Repair the case in C3_WORKSPACE")
    parser.add_argument("--diagnose", action="store_true", help="collect baseline evidence without model requests")
    args = parser.parse_args(argv)
    controller = None
    old_handlers = {}
    try:
        config = CaseConfig.from_env()
        os.chdir(config.workspace)
        # A nonraising handler is essential during promotion. Local tools and
        # HTTP workers observe the event and terminate through ProcessRunner.
        for signum in (signal.SIGTERM, signal.SIGINT):
            old_handlers[signum] = signal.signal(signum, lambda _signum, _frame: config.deadline.stop_event.set())
        if config.deadline.remaining() < 3:
            emit("deadline_reached")
            return 0
        controller = Controller(config)
        controller.bootstrap()
        if not args.diagnose:
            model = ModelClient(controller.runner, controller.deadline, config.budget_usd)
            repair_loop(controller, model)
            validate_pending_candidate(controller)
        try:
            final = controller.finalize()
        except TimeLimitError:
            final = {"final_validation": "deadline reached; installed source was verified at promotion"}
        emit("finished", **final, seconds_remaining=round(config.deadline.remaining(), 2),
             model_spent_usd=controller.model.spent_usd if controller.model is not None else 0)
        return 0
    except ConfigurationError as error:
        emit("configuration_error", error=str(error))
        return 2
    except (TimeLimitError, ModelError) as error:
        emit("resource_limit", error=str(error))
        return 0
    except Exception as error:
        emit("fixer_error", error_type=type(error).__name__, error=str(error)[:2000])
        return 1
    finally:
        if controller is not None:
            controller.runner.close()
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
