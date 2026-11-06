"""No-argument entrypoint used by local and image grading."""

from __future__ import annotations

import argparse
import json
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


def prompt_status(controller: Controller) -> str:
    status = controller.status()
    fields = ("source_hash", "accepted_source_hash", "unaccepted_changes", "seconds_remaining",
              "final_reserve", "remaining_budget_usd", "accepted_repairs")
    compact = {key: status[key][:1000] if isinstance(status[key], str) else status[key] for key in fields if key in status}
    compact["public_seeds"] = status.get("public_seeds", [])[:16]
    compact["hypotheses"] = [{"id": str(item.get("id", ""))[:64], "status": str(item.get("status", ""))[:20],
                               "obligation": str(item.get("obligation", ""))[:300]}
                              for item in status.get("hypotheses", [])[-6:]]
    compact["known_failures"] = [
        {"run_id": item.get("run_id"), "scenario": str(item.get("scenario", ""))[:256],
         "seed": item.get("seed"), "status": item.get("status"),
         "invariants": sorted({str(v.get("invariant", "?"))[:100] for v in item.get("violations", [])})[:8]}
        for item in status.get("known_failures", [])[-6:]]
    return json.dumps(compact, ensure_ascii=False)


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
                result = dispatcher.execute(call["name"], call["input"])
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
            try:
                validation = controller.validate({"reason": "finalize proposed repair", "target_invariants": []})
            except (ValueError, OSError, TimeLimitError) as error:
                validation = {"accepted": False, "error": str(error)}
        status = controller.status()
        if completion_attempts >= 2 or (not pending and not status["known_failures"]):
            break
        completion_attempts += 1
        messages.append({"role": "user", "content": "Continue diagnosing the remaining supported failures/ticket items. "
                         "Read current source where earlier excerpts have changed. Build a targeted probe if the current evidence "
                         "does not establish a repair.\n" + tool_text({"validation": validation}) + "\n" + prompt_status(controller)})


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
