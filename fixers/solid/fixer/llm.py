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


IDLE_TIMEOUT_S = 60.0        # no bytes at all for this long: the call is stuck


def parse_stream(lines, deadline: float) -> dict:
    """Rebuild an Anthropic Messages response from its SSE stream (an iterable of byte lines).

    Keeps every content block intact, including thinking blocks and their signatures, which must
    be sent back unchanged on the next turn when thinking is on with tool use.
    """
    msg: dict = {}
    usage: dict = {}
    blocks: dict[int, dict] = {}
    stop_reason = None
    done = False
    for raw in lines:
        if time.time() > deadline:
            raise LLMError("call exceeded its overall time limit while streaming")
        line = raw.strip()
        if not line.startswith(b"data:"):
            continue
        try:
            ev = json.loads(line[5:])
        except json.JSONDecodeError:
            continue
        t = ev.get("type")
        if t == "message_start":
            msg = ev.get("message") or {}
            usage.update(msg.get("usage") or {})
        elif t == "content_block_start":
            b = dict(ev.get("content_block") or {})
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
                        resp = parse_stream(r, deadline)
                    else:                                    # server ignored stream: plain JSON
                        resp = json.loads(r.read())
                break
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
                stalled = isinstance(e, TimeoutError) or "timed out" in str(e)
                # A stalled call is retried once: repeating a stuck call can eat the whole case.
                if attempt < (1 if stalled else 3):
                    self.log(f"llm: {'stalled (no data for %.0fs)' % IDLE_TIMEOUT_S if stalled else e}, "
                             f"retrying in {delay:.0f}s")
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise LLMError(str(e))
        self.calls += 1
        self.spent += self.cost(resp.get("usage") or {})
        return resp


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
