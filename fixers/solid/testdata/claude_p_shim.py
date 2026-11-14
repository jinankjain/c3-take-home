#!/usr/bin/env python3
"""Local test upstream for the pack proxy, backed by the `claude -p` CLI instead of OpenRouter.

    python fixers/solid/testdata/claude_p_shim.py --port 9902
    OPENROUTER_API_KEY=local python proxy/server.py --upstream http://127.0.0.1:9902/api

The proxy forwards Anthropic-format requests to POST /api/v1/messages. Each one becomes a single
`claude -p` call with no Claude Code tools: the static system prompt and tool list go in a system
prompt file, the conversation is rendered as text on stdin, and the model is asked to write tool
calls as <tool_call>{json}</tool_call>, which are parsed back into tool_use blocks.

Fidelity (for testing only): the model is the real Sonnet 4.6, but there is no native tool use,
no thinking budget, and caching is whatever claude -p does. Usage is reported back so the
proxy charges the case budget as it would for real.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MODEL_MAP = {"anthropic/claude-sonnet-4.6": "claude-sonnet-4-6", "anthropic/claude-sonnet-4-6": "claude-sonnet-4-6"}
WORKDIR = Path(tempfile.mkdtemp(prefix="claude-p-shim-"))
LOCK = threading.Lock()
STATS = {"calls": 0, "errors": 0}

PROTOCOL = """

# TOOL PROTOCOL (this replaces native tool calling)
You cannot call tools natively. To call tools, end your reply with one or more lines of the form
<tool_call>{"name": "<tool name>", "input": {...}}</tool_call>
Each must contain valid JSON on one line (escape newlines inside strings as \\n). You may write a
short plain-text note before the calls. Then STOP: results come back in the next USER turn as
<tool_result name=...>...</tool_result>. Never write <tool_result> yourself or guess what a tool
returns; anything after your tool calls is discarded. If you call no tool, your reply ends the turn.
Available tools (JSON Schema for each input):
"""


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if b.get("type") == "text":
            out.append(b["text"])
    return "\n".join(out)


def render_system(body: dict) -> str:
    system = body.get("system") or ""
    sys_text = system if isinstance(system, str) else "\n\n".join(text_of([b]) for b in system)
    tools = body.get("tools") or []
    tool_text = "\n".join(f"- {t['name']}: {t.get('description', '')}\n  input schema: "
                          f"{json.dumps(t.get('input_schema', {}))}" for t in tools)
    return sys_text + (PROTOCOL + tool_text if tools else "")


def render_messages(messages: list) -> str:
    names = {}
    parts = []
    for m in messages:
        role = m["role"].upper()
        content = m["content"]
        if isinstance(content, str):
            parts.append(f"=== {role} ===\n{content}")
            continue
        chunk = []
        for b in content:
            t = b.get("type")
            if t == "text":
                chunk.append(b["text"])
            elif t == "tool_use":
                names[b["id"]] = b["name"]
                chunk.append("<tool_call>" + json.dumps({"name": b["name"], "input": b.get("input", {})})
                             + "</tool_call>")
            elif t == "tool_result":
                c = b.get("content")
                c = c if isinstance(c, str) else text_of(c)
                chunk.append(f"<tool_result name={names.get(b.get('tool_use_id'), '?')}>\n{c}\n</tool_result>")
        parts.append(f"=== {role} ===\n" + "\n".join(chunk))
    parts.append("=== ASSISTANT === (your reply; follow the tool protocol)")
    return "\n\n".join(parts)


# Emulated stop sequences (the model inventing a tool result or the next transcript turn).
STOP_STRINGS = ("<tool_result", "=== USER", "=== ASSISTANT")
CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)


def parse_reply(text: str) -> tuple[list, str]:
    # Native tool use stops at the calls; here the model may keep going and invent results.
    cut = text.find("<tool_result")
    if cut != -1:
        text = text[:cut]
    blocks = []
    calls = []
    for m in CALL_RE.finditer(text):
        try:
            d = json.loads(m.group(1))
            if isinstance(d, dict) and isinstance(d.get("name"), str):
                calls.append({"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:20],
                              "name": d["name"], "input": d.get("input") or {}})
        except json.JSONDecodeError:
            continue
    prose = CALL_RE.sub("", text).strip()
    if prose or not calls:
        blocks.append({"type": "text", "text": prose or text.strip() or "(empty reply)"})
    blocks += calls
    return blocks, ("tool_use" if calls else "end_turn")


class ThinkingOverflow(Exception):
    def __init__(self, chars, usage):
        super().__init__(f"thinking ran past the budget ({chars} chars)")
        self.chars, self.usage = chars, usage


def _kill(p):
    """claude is a wrapper around a native child: kill the whole process group, not just the wrapper."""
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _attempt(cmd, stdin_text, budget, max_chars, timeout, on_progress=None):
    """One claude -p run. Returns (text, usage, served_by, final, cut, thinking_chars, ttft)."""
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_EFFORT", "MAX_THINKING_TOKENS")}
    env["MAX_THINKING_TOKENS"] = str(budget)
    think_cap = int(budget * 4 * 1.25) if budget else 0        # ~chars per token, 25% slack
    t_start = time.time()
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         text=True, cwd=WORKDIR, env=env, start_new_session=True)
    p.stdin.write(stdin_text)
    p.stdin.close()
    killer = threading.Timer(timeout, _kill, args=(p,))       # hard wall-clock limit for one run
    killer.start()
    text, usage, served_by, final, cut = "", {}, None, None, None
    thinking_chars, t_first = 0, None
    try:
        for ln in p.stdout:
            if not ln.startswith("{"):
                continue
            d = json.loads(ln)
            t = d.get("type")
            if t == "stream_event":
                ev = d["event"]
                if ev["type"] == "message_start":
                    served_by = ev["message"].get("model")
                    usage = dict(ev["message"].get("usage") or {})
                elif ev["type"] == "content_block_delta" and ev["delta"].get("type") == "thinking_delta":
                    thinking_chars += len(ev["delta"].get("thinking", ""))
                    t_first = t_first or time.time()
                    if on_progress:
                        on_progress()
                    if think_cap and thinking_chars > think_cap:
                        _kill(p)
                        raise ThinkingOverflow(thinking_chars, usage)
                elif ev["type"] == "content_block_delta" and ev["delta"].get("type") == "text_delta":
                    t_first = t_first or time.time()
                    text += ev["delta"]["text"]
                    if on_progress:
                        on_progress()
                    # Emulated stop sequences: the model started inventing results or the next
                    # turn, or ran past max_tokens. Native tool use would have stopped here.
                    hit = min((i for i in (text.find(s) for s in STOP_STRINGS) if i != -1), default=-1)
                    if hit != -1 or len(text) > max_chars:
                        cut = "stop_string" if hit != -1 else "max_tokens"
                        text = text[:hit] if hit != -1 else text
                        _kill(p)
                        break
                elif ev["type"] == "message_delta" and ev.get("usage"):
                    usage.update(ev["usage"])
            elif t == "result":
                final = d
    finally:
        killer.cancel()
        _kill(p)
        p.wait()
    ttft = (t_first - t_start) if t_first else -1.0
    return text, usage, served_by, final, cut, thinking_chars, ttft


def call_claude(body: dict, timeout: float, on_progress=None) -> dict:
    model = MODEL_MAP.get(body.get("model"), "claude-sonnet-4-6")
    system = render_system(body)
    sp = WORKDIR / f"system-{hashlib.sha1(system.encode()).hexdigest()[:16]}.txt"
    if not sp.exists():
        sp.write_text(system)
    cmd = ["claude", "-p", "--model", model, "--tools", "", "--system-prompt-file", str(sp),
           "--output-format", "stream-json", "--verbose", "--include-partial-messages",
           "--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands"]
    max_chars = int(body.get("max_tokens") or 8000) * 4
    # Match the request's thinking budget (real grading) instead of this machine's Claude Code
    # settings. claude -p does not enforce it, so the shim does: past the budget, the thinking is
    # cut and the request is re-asked with thinking off (the real API would stop thinking and answer).
    th = body.get("thinking") or {}
    budget = int(th.get("budget_tokens") or 0) if th.get("type") == "enabled" else 0
    stdin_text = render_messages(body["messages"])
    t0 = time.time()
    extra_out, note = 0, ""
    try:
        r = _attempt(cmd, stdin_text, budget, max_chars, timeout, on_progress)
    except ThinkingOverflow as e:
        extra_out, note = e.chars // 4, f" thinking_overflow={e.chars}chars->rerun_without_thinking"
        r = _attempt(cmd, stdin_text, 0, max_chars, max(10.0, timeout - (time.time() - t0)), on_progress)
    text, usage, served_by, final, cut, thinking_chars, ttft = r
    if served_by and served_by != model:
        raise RuntimeError(f"expected {model}, but claude -p used {served_by}")
    if final is not None:
        if final.get("is_error"):
            raise RuntimeError(f"claude -p error: {str(final.get('result'))[:400]}")
        used = sorted(final.get("modelUsage") or {})
        if used != [model]:
            raise RuntimeError(f"expected only {model}, but claude -p used {used}")
        usage = final.get("usage") or usage
        text = final.get("result") or text
    elif cut is None:
        raise RuntimeError(f"claude -p ended without a result (timeout {timeout:.0f}s)")
    else:
        usage["output_tokens"] = max(int(usage.get("output_tokens") or 0), len(text) // 4)
    usage["output_tokens"] = int(usage.get("output_tokens") or 0) + extra_out
    blocks, stop = parse_reply(text)
    if cut == "max_tokens" and stop == "end_turn":
        stop = "max_tokens"
    return {"id": "msg_" + uuid.uuid4().hex[:24], "type": "message", "role": "assistant",
            "model": body.get("model"), "content": blocks, "stop_reason": stop, "_cut": cut,
            "_diag": f"ttft={ttft:.1f}s think_chars={thinking_chars} budget={budget}{note}",
            "usage": {"input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0),
                      "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
                      "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0)}}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout_s = 170.0          # just above the fixer's own per-call timeout

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, obj: dict):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(200, {"ok": True, **STATS})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path.split("?")[0] != "/api/v1/messages":
            return self._send(404, {"error": {"message": "only /api/v1/messages is served"}})
        stream = bool(body.get("stream"))
        on_progress = None
        if stream:
            # Like the real API: send the stream headers at once, keep the connection visibly alive
            # (ping) only while the model is producing tokens, then the reply as standard events.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            last = [0.0]

            def on_progress():
                if time.time() - last[0] >= 2.0:
                    last[0] = time.time()
                    self._event({"type": "ping"})
        t0 = time.time()
        try:
            resp = call_claude(body, self.timeout_s, on_progress)
        except Exception as e:  # noqa: BLE001
            with LOCK:
                STATS["errors"] += 1
            print(f"[shim] error after {time.time() - t0:.1f}s: {e}", flush=True)
            err = {"type": "error", "error": {"type": "api_error", "message": str(e)[:500]}}
            if stream:
                return self._event(err)
            return self._send(502, err)
        with LOCK:
            STATS["calls"] += 1
        u = resp["usage"]
        print(f"[shim] {time.time() - t0:5.1f}s in={u['input_tokens']} cache_r={u['cache_read_input_tokens']} "
              f"cache_w={u['cache_creation_input_tokens']} out={u['output_tokens']} stop={resp['stop_reason']} "
              f"tools={[b['name'] for b in resp['content'] if b['type'] == 'tool_use']}"
              + (f" cut={resp['_cut']}" if resp.get('_cut') else "") + f" {resp.get('_diag', '')}", flush=True)
        resp.pop("_cut", None)
        resp.pop("_diag", None)
        if not stream:
            return self._send(200, resp)
        self._event({"type": "message_start", "message": {
            "id": resp["id"], "type": "message", "role": "assistant", "model": resp["model"], "content": [],
            "stop_reason": None, "usage": {**u, "output_tokens": 1}}})
        for i, b in enumerate(resp["content"]):
            if b["type"] == "text":
                self._event({"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}})
                self._event({"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": b["text"]}})
            else:
                self._event({"type": "content_block_start", "index": i, "content_block": {
                    "type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}})
                self._event({"type": "content_block_delta", "index": i, "delta": {
                    "type": "input_json_delta", "partial_json": json.dumps(b["input"])}})
            self._event({"type": "content_block_stop", "index": i})
        self._event({"type": "message_delta", "delta": {"stop_reason": resp["stop_reason"]},
                     "usage": {"output_tokens": u["output_tokens"]}})
        self._event({"type": "message_stop"})

    def _event(self, obj: dict):
        try:
            self.wfile.write(f"event: {obj['type']}\ndata: {json.dumps(obj)}\n\n".encode())
            self.wfile.flush()
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9902)
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    print(f"[shim] claude -p upstream on http://127.0.0.1:{a.port}/api (workdir {WORKDIR})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
