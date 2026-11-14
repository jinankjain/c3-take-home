"""The tool-use loop. The orchestrator decides when it stops; the model only chooses tools."""
from __future__ import annotations

import json
import time

from .llm import BudgetExhausted, LLMError
from .tools import SCHEMAS

MAX_TOKENS = 8_000             # a tool-calling turn rarely needs more; long replies risk timeouts
THINKING = 4_000
CALL_TIMEOUT_S = 300           # overall per call; a stalled stream is caught sooner (llm.IDLE_TIMEOUT_S)
TRIM_ABOVE_CHARS = 450_000       # ~110K tokens of conversation history
KEEP_RECENT_RESULTS = 8
MAX_NUDGES = 2
MAX_CONSECUTIVE_ERRORS = 3


class Agent:
    def __init__(self, llm, tools, orch, instructions: str, context: str, log=print):
        self.llm, self.tools, self.orch, self.log = llm, tools, orch, log
        self.system = [{"type": "text", "text": instructions},
                       {"type": "text", "text": context, "cache_control": {"type": "ephemeral"}}]
        self.messages: list[dict] = []
        self.turns = 0

    # ------------------------------------------------------------------ public
    def run(self, user_text: str, max_turns: int | None = None, max_seconds: float | None = None) -> str:
        """Run until done(), a stop condition, or the model stops calling tools.
        Returns why it stopped: done | stopped | budget | error | idle | limit (max_turns/max_seconds)."""
        self.messages.append({"role": "user", "content": [{"type": "text", "text": user_text}]})
        nudges = errors = 0
        t_start, turns_start = time.time(), self.turns
        while True:
            capped = (max_turns is not None and self.turns - turns_start >= max_turns) or (
                max_seconds is not None and time.time() - t_start >= max_seconds)
            if capped and getattr(self.orch, "open_bug", None):
                if max_turns is not None:            # a real uncovered bug turned up: keep working
                    self.log(f"agent: phase cap lifted (open bug: {self.orch.open_bug})")
                max_turns = max_seconds = None
                capped = False
            if capped:
                self.log(f"agent: phase limit reached ({self.turns - turns_start} turns, "
                         f"{time.time() - t_start:.0f}s)")
                return "limit"
            reason = self.orch.should_stop()
            if reason:
                self.log(f"agent: stopping ({reason})")
                return "stopped"
            note = self.orch.maybe_safety_checkpoint()
            if note and self.messages[-1]["role"] == "user":
                self.messages[-1]["content"].append({"type": "text", "text": note})
            self._trim()
            self._mark_cache()
            t0 = time.time()
            try:
                resp = self.llm.create(system=self.system, messages=self.messages, tools=SCHEMAS,
                                       max_tokens=MAX_TOKENS, thinking_budget=THINKING,
                                       timeout=max(10, min(CALL_TIMEOUT_S, self.orch.time_left() - 30)))
            except BudgetExhausted as e:
                self.log(f"agent: model budget exhausted ({e})")
                return "budget"
            except LLMError as e:
                # One failed call must not end the case while time is left: pause and try again.
                errors += 1
                self.log(f"agent: model call failed ({errors} in a row): {e}")
                if errors >= MAX_CONSECUTIVE_ERRORS:
                    return "error"
                time.sleep(min(10.0, max(0.0, self.orch.time_left() - 60)))
                continue
            errors = 0
            self.turns += 1
            content = resp.get("content") or []
            self.messages.append({"role": "assistant", "content": content})
            u = resp.get("usage") or {}
            self.log(f"turn {self.turns}: {time.time() - t0:.1f}s, in={u.get('input_tokens', 0)} "
                     f"cache_r={u.get('cache_read_input_tokens', 0)} cache_w={u.get('cache_creation_input_tokens', 0)} "
                     f"out={u.get('output_tokens', 0)}, spent ${self.llm.spent:.3f}")
            for b in content:
                if b.get("type") == "text" and b.get("text", "").strip():
                    self.log("  says: " + b["text"].strip().replace("\n", " ")[:400])

            calls = [b for b in content if b.get("type") == "tool_use"]
            if not calls:
                if resp.get("stop_reason") == "max_tokens":
                    self.messages.append({"role": "user", "content": [{"type": "text", "text":
                        "Your reply was cut off (max tokens). Be briefer and continue with tool calls."}]})
                    continue
                if nudges >= MAX_NUDGES:
                    return "idle"
                nudges += 1
                self.messages.append({"role": "user", "content": [{"type": "text", "text":
                    "Continue working with the tools. Call done(summary) when the fix is checkpointed."}]})
                continue

            results = []
            for c in calls:
                t1 = time.time()
                out = self.tools.dispatch(c["name"], c.get("input") or {})
                self.log(f"  tool {c['name']}({_short(c.get('input'))}) -> {time.time() - t1:.1f}s, "
                         f"{out.splitlines()[0][:160] if out else ''}")
                results.append({"type": "tool_result", "tool_use_id": c["id"], "content": out})
            results.append({"type": "text", "text": self.orch.status_line()})
            self.messages.append({"role": "user", "content": results})
            if self.tools.done_summary is not None:
                self.log(f"agent: done: {self.tools.done_summary[:500]}")
                return "done"

    # ------------------------------------------------------------------ context management
    def _mark_cache(self):
        """Rolling cache breakpoint on the newest user turn (plus the static system prefix)."""
        for m in self.messages:
            for b in m["content"] if isinstance(m["content"], list) else []:
                b.pop("cache_control", None)
        last = self.messages[-1]
        if last["role"] == "user" and last["content"]:
            last["content"][-1]["cache_control"] = {"type": "ephemeral"}

    def _trim(self):
        size = len(json.dumps(self.messages))
        if size <= TRIM_ABOVE_CHARS:
            return
        results = [b for m in self.messages if m["role"] == "user" and isinstance(m["content"], list)
                   for b in m["content"] if b.get("type") == "tool_result"]
        cut = 0
        for b in results[:-KEEP_RECENT_RESULTS]:
            if isinstance(b.get("content"), str) and len(b["content"]) > 300:
                b["content"] = b["content"][:200] + " ... [trimmed old output]"
                cut += 1
        self.log(f"agent: trimmed {cut} old tool results (history was {size} chars)")


def _short(d) -> str:
    s = json.dumps(d or {}, ensure_ascii=False)
    return s if len(s) <= 120 else s[:117] + "..."
