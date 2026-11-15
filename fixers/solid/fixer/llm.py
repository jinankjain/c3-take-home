"""Model clients: the real one (Anthropic format through the C3 proxy) and a scripted one
for offline testing. Both return an Anthropic Messages API response dict."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

# $ per million tokens, Sonnet 4.6 (used to track spend; the proxy is the authority).
PRICE = {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75}


class BudgetExhausted(Exception):
    pass


class LLMError(Exception):
    pass


class RetryableStreamError(LLMError):
    """The stream broke off or the server reported overload mid-stream."""


class StreamStalled(RetryableStreamError):
    """The stream stayed open (pings) but no content arrived for too long."""


class ThinkingOverflow(LLMError):
    """Thinking ran past its budget: the budget is not a hard limit, and a turn that thinks until
    max_tokens produces no action at all."""

    def __init__(self, chars: int, seconds: float = 0.0, text: str = ""):
        super().__init__(f"thinking passed its budget ({seconds:.0f}s, {chars} chars streamed)")
        self.chars, self.seconds, self.text = chars, seconds, text


OVERTHINK_NUDGE = ("[orchestrator] Your previous attempt at this reply spent its whole budget thinking. "
                   "Think briefly (a few sentences), then act now with tool calls; investigate further "
                   "with tools rather than in your head.")
CARRY_MAX_CHARS = 6000       # how much of the cut thinking (its streamed summary) the retry gets back


def overthink_nudge(partial: str) -> str:
    """The retry's nudge, carrying what the cut attempt had worked out so it is not redone."""
    partial = partial.strip()
    if not partial:
        return OVERTHINK_NUDGE
    if len(partial) > CARRY_MAX_CHARS:
        partial = "..." + partial[-CARRY_MAX_CHARS:]
    return (OVERTHINK_NUDGE + " Summary of the reasoning you had done before it was cut (build on it, "
            "do not redo it):\n<previous_reasoning>\n" + partial + "\n</previous_reasoning>")


IDLE_TIMEOUT_S = 60.0        # no bytes at all for this long: the call is stuck
STALL_TIMEOUT_S = 60.0       # bytes (pings) but no new content for this long: also stuck
THINK_TIME_CAP_S = 50.0      # still only thinking this long after it began: over budget. The stream
                             # carries a summary of the thinking, so its size can't be the measure.


def parse_stream(lines, deadline: float, thinking_cap_chars: int = 0, stall_s: float = 0,
                 thinking_cap_s: float = 0) -> dict:
    """Rebuild an Anthropic Messages response from its SSE stream (an iterable of byte lines).

    Keeps every content block intact, including thinking blocks and their signatures, which must
    be sent back unchanged on the next turn when thinking is on with tool use. With
    `thinking_cap_chars`, raises ThinkingOverflow as soon as the thinking passes that size. With
    `stall_s`, raises StreamStalled when no content event (pings do not count) arrives for that long.
    With `thinking_cap_s`, raises ThinkingOverflow when the reply is still in its thinking block that
    long after thinking began (checked on every event, pings included).
    """
    msg: dict = {}
    usage: dict = {}
    blocks: dict[int, dict] = {}
    stop_reason = None
    done = False
    thinking_chars = 0
    thinking_since = None          # set while the reply is in a thinking block, cleared after it
    last_progress = time.time()
    for raw in lines:
        now = time.time()
        if now > deadline:
            raise LLMError("call exceeded its overall time limit while streaming")
        if stall_s and now - last_progress > stall_s:
            raise StreamStalled(f"no new content for {now - last_progress:.0f}s (only keep-alive pings)")
        if thinking_cap_s and thinking_since is not None and now - thinking_since > thinking_cap_s:
            raise ThinkingOverflow(thinking_chars, now - thinking_since, _thinking_text(blocks))
        line = raw.strip()
        if not line.startswith(b"data:"):
            continue
        try:
            ev = json.loads(line[5:])
        except json.JSONDecodeError:
            continue
        t = ev.get("type")
        if t in ("message_start", "content_block_start", "content_block_delta", "content_block_stop",
                 "message_delta", "message_stop"):
            last_progress = now
        if t == "message_start":
            msg = ev.get("message") or {}
            usage.update(msg.get("usage") or {})
        elif t == "content_block_start":
            b = dict(ev.get("content_block") or {})
            if b.get("type") in ("thinking", "redacted_thinking"):
                thinking_since = thinking_since or now
            else:
                thinking_since = None             # text or a tool call started: thinking is over
            if b.get("type") == "tool_use":
                b["input"], b["_json"] = {}, ""
            blocks[int(ev["index"])] = b
        elif t == "content_block_delta":
            b = blocks.setdefault(int(ev["index"]), {"type": "text", "text": ""})
            d = ev.get("delta") or {}
            dt = d.get("type")
            if dt == "text_delta":
                b["text"] = b.get("text", "") + d.get("text", "")
            elif dt == "thinking_delta":
                b["thinking"] = b.get("thinking", "") + d.get("thinking", "")
                thinking_chars += len(d.get("thinking", ""))
                if thinking_cap_chars and thinking_chars > thinking_cap_chars:
                    raise ThinkingOverflow(thinking_chars, now - (thinking_since or now), _thinking_text(blocks))
            elif dt == "signature_delta":
                b["signature"] = b.get("signature", "") + d.get("signature", "")
            elif dt == "input_json_delta":
                b["_json"] = b.get("_json", "") + d.get("partial_json", "")
        elif t == "content_block_stop":
            b = blocks.get(int(ev["index"]))
            if b is not None and b.get("type") == "tool_use":
                raw_json = b.pop("_json", "")
                try:
                    b["input"] = json.loads(raw_json) if raw_json.strip() else {}
                except json.JSONDecodeError:
                    b["input"] = {}
        elif t == "message_delta":
            stop_reason = (ev.get("delta") or {}).get("stop_reason") or stop_reason
            usage.update({k: v for k, v in (ev.get("usage") or {}).items() if v is not None})
        elif t == "error":
            err = ev.get("error") or {}
            if err.get("type") in ("overloaded_error", "api_error", "rate_limit_error"):
                raise RetryableStreamError(f"stream error: {err}")
            raise LLMError(f"stream error: {err}")
        elif t == "message_stop":
            done = True
            break
    if not done:
        raise RetryableStreamError("stream ended before message_stop")
    content = []
    for i in sorted(blocks):
        b = blocks[i]
        b.pop("_json", None)
        content.append(b)
    return {"id": msg.get("id"), "type": "message", "role": "assistant", "model": msg.get("model"),
            "content": content, "stop_reason": stop_reason, "usage": usage}


class ProxyLLM:
    def __init__(self, log=print):
        self.url = os.environ["ANTHROPIC_BASE_URL"].rstrip("/") + "/v1/messages"
        self.key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = os.environ.get("ANTHROPIC_MODEL", "anthropic/claude-sonnet-4.6")
        self.spent = 0.0
        self.calls = 0
        self.log = log

    def cost(self, usage: dict) -> float:
        return (usage.get("input_tokens", 0) * PRICE["input"]
                + usage.get("output_tokens", 0) * PRICE["output"]
                + usage.get("cache_read_input_tokens", 0) * PRICE["cache_read"]
                + usage.get("cache_creation_input_tokens", 0) * PRICE["cache_write"]) / 1e6

    def create(self, *, system, messages, tools, max_tokens, thinking_budget=0, timeout=300) -> dict:
        """One model call, streamed. `timeout` is the overall limit for the call; separately, a
        call that sends nothing for IDLE_TIMEOUT_S is treated as stuck. A slow call that keeps
        producing tokens is therefore not thrown away."""
        body = {"model": self.model, "max_tokens": max_tokens, "system": system,
                "messages": messages, "tools": tools, "stream": True}
        if thinking_budget >= 1024:
            body["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}
        data = json.dumps(body).encode()
        deadline = time.time() + timeout
        delay = 2.0
        # ~4 chars per token: stop a reply whose thinking runs past its budget (once per call).
        think_cap = thinking_budget * 4 if thinking_budget >= 1024 else 0
        for attempt in range(4):
            left = deadline - time.time()
            if left < 5:
                raise LLMError("no time left for the call")
            req = urllib.request.Request(self.url, data=data, headers={
                "x-api-key": self.key, "anthropic-version": "2023-06-01",
                "content-type": "application/json", "accept": "text/event-stream"})
            try:
                with urllib.request.urlopen(req, timeout=min(IDLE_TIMEOUT_S, left)) as r:
                    if "text/event-stream" in (r.headers.get("Content-Type") or ""):
                        resp = parse_stream(r, deadline, think_cap, STALL_TIMEOUT_S,
                                            THINK_TIME_CAP_S if think_cap else 0)
                    else:                                    # server ignored stream: plain JSON
                        resp = json.loads(r.read())
                break
            except ThinkingOverflow as e:
                # Charged anyway (the proxy bills what was generated); count it so the budget is right.
                # streamed thinking is a summary: estimate the real tokens from the time (~50 tok/s)
                self.spent += max(e.chars / 4, e.seconds * 50) * PRICE["output"] / 1e6
                self.log(f"llm: {e}; retrying with a nudge to think briefly and act")
                retry = dict(body)
                retry["messages"] = _with_nudge(body["messages"], overthink_nudge(e.text))
                data = json.dumps(retry).encode()
                think_cap = 0                            # never abort the same turn twice
                continue
            except urllib.error.HTTPError as e:
                msg = e.read()[:300].decode(errors="replace")
                if e.code == 402:
                    raise BudgetExhausted(msg)
                if e.code in (429, 500, 502, 503, 529) and attempt < 3:
                    self.log(f"llm: HTTP {e.code}, retrying in {delay:.0f}s")
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise LLMError(f"HTTP {e.code}: {msg}")
            except (urllib.error.URLError, TimeoutError, ConnectionError, RetryableStreamError) as e:
                stalled = isinstance(e, (TimeoutError, StreamStalled)) or "timed out" in str(e)
                # A stalled call is retried once: repeating a stuck call can eat the whole case.
                if attempt < (1 if stalled else 3):
                    self.log(f"llm: {('stalled: %s' % e) if stalled else e}, "
                             f"retrying in {delay:.0f}s")
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise LLMError(str(e))
        self.calls += 1
        self.spent += self.cost(resp.get("usage") or {})
        return resp


def _thinking_text(blocks: dict) -> str:
    return "\n".join(b.get("thinking", "") for _, b in sorted(blocks.items()) if b.get("type") == "thinking")


def _with_nudge(messages: list, text: str) -> list:
    """A copy of `messages` with `text` appended to the last user message (the prefix, and so the
    prompt cache, is unchanged)."""
    out = list(messages)
    if out and out[-1].get("role") == "user":
        last = dict(out[-1])
        content = last["content"] if isinstance(last["content"], list) else [{"type": "text", "text": last["content"]}]
        last["content"] = content + [{"type": "text", "text": text}]
        out[-1] = last
    else:
        out.append({"role": "user", "content": [{"type": "text", "text": text}]})
    return out


class ScriptedLLM:
    """Replays a JSON script, for testing the loop without a model.

    The script is a list of turns. Each turn is a list of tool calls ({"name", "input"}),
    or {"text": "..."} for a plain reply. When the script runs out it replies end_turn.
    """

    def __init__(self, path: str | Path, log=print):
        self.turns = json.loads(Path(path).read_text())
        self.i = 0
        self.spent = 0.0
        self.calls = 0
        self.log = log

    def create(self, *, system, messages, tools, max_tokens, thinking_budget=0, timeout=300) -> dict:
        self.calls += 1
        if self.i >= len(self.turns):
            return {"content": [{"type": "text", "text": "(script finished)"}], "stop_reason": "end_turn",
                    "usage": {}}
        turn = self.turns[self.i]
        self.i += 1
        if isinstance(turn, dict) and "text" in turn:
            return {"content": [{"type": "text", "text": turn["text"]}], "stop_reason": "end_turn",
                    "usage": {}}
        if isinstance(turn, dict) and "http_error" in turn:      # simulate proxy failures
            if turn["http_error"] == 402:
                raise BudgetExhausted("scripted 402")
            raise LLMError(f"scripted HTTP {turn['http_error']}")
        content = [{"type": "tool_use", "id": f"toolu_{self.i}_{k}", "name": c["name"],
                    "input": c.get("input", {})} for k, c in enumerate(turn)]
        return {"content": content, "stop_reason": "tool_use", "usage": {}}


def make_llm(log=print):
    spec = os.environ.get("FIXER_LLM", "")
    if spec.startswith("scripted:"):
        return ScriptedLLM(spec.split(":", 1)[1], log=log)
    return ProxyLLM(log=log)
