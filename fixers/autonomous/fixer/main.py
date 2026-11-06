"""No-argument entrypoint used by local and image grading."""

from __future__ import annotations

import argparse
import json
import math
import os
import signal

from .controller import Controller
from .model import BudgetExhausted, ModelClient, ModelError
from .prompts import SYSTEM
from .runtime import CaseConfig, ConfigurationError, TimeLimitError, emit
from .tools import TOOL_DEFINITIONS, ToolDispatcher


MAX_TURNS = 32
MAX_CONTEXT_CHARS = 180_000
MAX_TOOL_RESULT_CHARS = 32_000
MAX_LEDGER_CHARS = 64_000


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
    return json.dumps(compact, ensure_ascii=False)


def prompt_status(controller: Controller) -> str:
    status = controller.status()
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
        compact["hypotheses"] = [{
            "id": _ledger_text(item.get("id", ""), 64),
            "status": _ledger_text(item.get("status", ""), 20),
            "source_hash": _ledger_text(item.get("source_hash", ""), 64),
            "obligation": _ledger_text(item.get("obligation", ""), min(detail_chars, 400)),
            "mechanism": _ledger_text(item.get("mechanism", ""), detail_chars),
            "evidence": _ledger_strings(item.get("evidence"), count=4, chars=min(detail_chars, 400)),
        } for item in status.get("hypotheses", [])[-6:] if isinstance(item, dict)]
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


def compact_messages(controller: Controller, messages: list[dict], *, force: bool = False) -> list[dict]:
    size = len(json.dumps(messages, ensure_ascii=False))
    if size <= MAX_CONTEXT_CHARS and not force:
        return messages
    # Retain only complete assistant/user pairs, including every native tool
    # result for a retained tool_use block. Initial context remains stable.
    pairs = []
    cursor = len(messages) - 1
    while cursor >= 1 and len(pairs) < (2 if force else 4):
        if messages[cursor]["role"] != "user" or messages[cursor - 1]["role"] != "assistant":
            break
        pairs.insert(0, [messages[cursor - 1], messages[cursor]])
        cursor -= 2
    ledger = "# Current repair ledger\n" + prompt_status(controller)
    initial = controller.initial_context[:90_000]
    if len(initial) < len(controller.initial_context):
        initial += "\n[initial context truncated; request needed files/contracts with tools]"
    while True:
        result = [{"role": "user", "content": initial}]
        for pair in pairs:
            result.extend(dict(message) for message in pair)
        last = result[-1]
        content = last["content"]
        last["content"] = [*content, {"type": "text", "text": ledger}] if isinstance(content, list) else content + "\n" + ledger
        final_size = len(json.dumps(result, ensure_ascii=False))
        if final_size <= MAX_CONTEXT_CHARS:
            emit("context_compacted", previous_chars=size, final_chars=final_size, retained_pairs=len(pairs))
            return result
        if pairs:
            pairs.pop(0)
        else:
            initial = initial[:max(1000, int(len(initial) * 0.75))]


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
    for turn in range(MAX_TURNS):
        if controller.deadline.remaining(controller.final_reserve) < 2:
            break
        messages = compact_messages(controller, messages)
        try:
            reply = model.messages(SYSTEM, messages, TOOL_DEFINITIONS, max_tokens=output_tokens,
                                   reserve=controller.final_reserve)
        except BudgetExhausted as error:
            emit("model_budget_limit", error=str(error))
            if budget_recoveries >= 1:
                break
            budget_recoveries += 1
            messages = compact_messages(controller, messages, force=True)
            output_tokens = 2048
            continue
        except ModelError as error:
            emit("model_error", code=error.code, status=error.status, error=str(error))
            if error.code == "incomplete_response" and incomplete_recoveries < 2:
                incomplete_recoveries += 1
                output_tokens = min(8192, output_tokens * 2)
                continue
            break
        emit("model_turn", turn=turn + 1, cost_usd=reply.cost_usd,
             remaining_budget_usd=reply.remaining_usd, stop_reason=reply.stop_reason)
        messages.append({"role": "assistant", "content": reply.content})
        calls = [block for block in reply.content if block["type"] == "tool_use"]
        for block in reply.content:
            if block["type"] == "text":
                emit("agent_note", text=block["text"][:4000])
        if calls:
            results = []
            for call in calls:
                if controller.deadline.remaining() <= 0 or controller.deadline.stop_event.is_set():
                    return
                validating_hash = controller.sources.candidate_hash if call["name"] == "validate" else None
                result = dispatcher.execute(call["name"], call["input"])
                if validating_hash is not None and result.get("ok"):
                    validated_hashes.add(validating_hash)
                emit("tool", name=call["name"], ok=result.get("ok"),
                     source_hash=controller.sources.candidate_hash)
                results.append({"type": "tool_result", "tool_use_id": call["id"],
                                "content": tool_text(result), "is_error": not result.get("ok", False)})
            results.append({"type": "text", "text": "# Resource status\n" + prompt_status(controller)})
            messages.append({"role": "user", "content": results})
            continue
        # The controller verifies a pending patch even if the model ends its
        # conversation without explicitly invoking the validation tool.
        pending = controller.sources.candidate_hash != controller.sources.best_hash
        validation = None
        if pending:
            validating_hash = controller.sources.candidate_hash
            if validating_hash in validated_hashes:
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
        if completion_attempts >= 2 or (not pending and not status["known_failures"]):
            break
        completion_attempts += 1
        messages.append({"role": "user", "content": "Continue diagnosing the remaining supported failures/ticket items. "
                         "Read current source where earlier excerpts have changed. Build a targeted probe if the current evidence "
                         "does not establish a repair.\n" + tool_text({"validation": validation}) + "\n" + prompt_status(controller)})


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
    if candidate in validated_hashes:
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
