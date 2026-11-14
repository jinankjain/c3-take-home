"""Offline tests for the streaming parser in fixer/llm.py.  Run: python -m pytest -q fixers/solid/testdata"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fixer.llm import LLMError, RetryableStreamError, parse_stream  # noqa: E402


def sse(*events):
    out = []
    for ev in events:
        out += [f"event: {ev['type']}\n".encode(), f"data: {json.dumps(ev)}\n".encode(), b"\n"]
    return out


START = {"type": "message_start", "message": {"id": "msg_1", "model": "m", "usage": {
    "input_tokens": 10, "cache_read_input_tokens": 500, "cache_creation_input_tokens": 0, "output_tokens": 1}}}
STOP = [{"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 42}},
        {"type": "message_stop"}]


def full_stream():
    return sse(
        START,
        {"type": "ping"},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Let me "}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "think."}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig=="}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Reading."}},
        {"type": "content_block_stop", "index": 1},
        {"type": "content_block_start", "index": 2,
         "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {}}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"path": "src/'}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": 'a.py", "start": 3}'}},
        {"type": "content_block_stop", "index": 2},
        {"type": "content_block_start", "index": 3,
         "content_block": {"type": "tool_use", "id": "toolu_2", "name": "run_tests", "input": {}}},
        {"type": "content_block_stop", "index": 3},
        *STOP)


def test_full_reply_is_rebuilt():
    r = parse_stream(full_stream(), time.time() + 60)
    assert r["stop_reason"] == "tool_use"
    assert r["content"][0] == {"type": "thinking", "thinking": "Let me think.", "signature": "sig=="}
    assert r["content"][1] == {"type": "text", "text": "Reading."}
    assert r["content"][2] == {"type": "tool_use", "id": "toolu_1", "name": "read_file",
                               "input": {"path": "src/a.py", "start": 3}}
    assert r["content"][3] == {"type": "tool_use", "id": "toolu_2", "name": "run_tests", "input": {}}
    assert r["usage"]["output_tokens"] == 42 and r["usage"]["cache_read_input_tokens"] == 500


def test_truncated_stream_is_retryable():
    with pytest.raises(RetryableStreamError):
        parse_stream(full_stream()[:-6], time.time() + 60)


def test_overload_mid_stream_is_retryable():
    lines = sse(START, {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}})
    with pytest.raises(RetryableStreamError):
        parse_stream(lines, time.time() + 60)


def test_overall_deadline():
    with pytest.raises(LLMError, match="overall time limit"):
        parse_stream(full_stream(), time.time() - 1)


# ---------------------------------------------------------------- idle timeout vs slow-but-progressing
import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

import fixer.llm as llm_mod  # noqa: E402

REAL_SLEEP = time.sleep      # the client's retry backoff is patched out below; servers must really wait


def _server(behaviour):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            behaviour(self)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _client(monkeypatch, srv, idle):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{srv.server_address[1]}")
    monkeypatch.setattr(llm_mod, "IDLE_TIMEOUT_S", idle)
    monkeypatch.setattr(llm_mod, "STALL_TIMEOUT_S", idle)
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: None)      # skip retry backoff
    return llm_mod.ProxyLLM(log=lambda m: None)


def _write(h, lines):
    for ln in lines:
        h.wfile.write(ln)
    h.wfile.flush()


def test_stalled_stream_hits_idle_timeout(monkeypatch):
    srv = _server(lambda h: REAL_SLEEP(3))                          # headers, then nothing
    llm = _client(monkeypatch, srv, idle=0.5)
    t0 = time.time()
    with pytest.raises(LLMError, match="timed out"):
        llm.create(system="s", messages=[], tools=[], max_tokens=10, timeout=30)
    assert 0.9 < time.time() - t0 < 2.5                             # 2 tries x 0.5 s idle, not 30 s
    srv.shutdown()


def test_slow_but_progressing_stream_succeeds(monkeypatch):
    def slow(h):
        lines = full_stream()
        _write(h, lines[:12])                                       # message_start, ping, thinking start
        for _ in range(8):                                          # 1.6 s of real progress > idle 0.5 s
            _write(h, sse({"type": "content_block_delta", "index": 0,
                           "delta": {"type": "thinking_delta", "thinking": "."}}))
            REAL_SLEEP(0.2)
        _write(h, lines[12:])
    srv = _server(slow)
    llm = _client(monkeypatch, srv, idle=0.5)
    t0 = time.time()
    r = llm.create(system="s", messages=[], tools=[], max_tokens=10, timeout=30)
    assert time.time() - t0 > 1.5                                   # really was slower than the idle limit
    assert r["stop_reason"] == "tool_use" and r["content"][2]["input"]["path"] == "src/a.py"
    srv.shutdown()


# ---------------------------------------------------------------- thinking past its budget
def _thinking_stream(n_chars):
    return sse(START,
               {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
               *[{"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "x" * 500}}
                 for _ in range(n_chars // 500)])


def _recording_server(responses):
    """Serves the given SSE line lists in order and records each request body."""
    seen = []

    def behaviour(h):
        pass

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            seen.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            _write(self, responses[min(len(seen), len(responses)) - 1])

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


def test_overthinking_is_cut_and_retried_once_with_a_nudge(monkeypatch):
    srv, seen = _recording_server([_thinking_stream(30_000), full_stream()])
    llm = _client(monkeypatch, srv, idle=5)
    msgs = [{"role": "user", "content": [{"type": "text", "text": "go"}]}]
    r = llm.create(system="s", messages=msgs, tools=[], max_tokens=8000, thinking_budget=4000, timeout=30)
    assert r["stop_reason"] == "tool_use"                         # the retry's reply came back
    assert len(seen) == 2
    assert "think briefly" in json.dumps(seen[1]["messages"][-1]).lower() # nudge added to the last user turn
    assert "think briefly" not in json.dumps(seen[0]["messages"]).lower() and msgs[0]["content"][-1]["text"] == "go"
    assert seen[1]["thinking"] == seen[0]["thinking"]             # thinking stays on: no mid-turn toggle
    assert llm.spent > 0                                          # the aborted thinking is still counted
    srv.shutdown()


def test_retry_is_never_cut_a_second_time(monkeypatch):
    long_then_done = _thinking_stream(30_000) + full_stream()[3:]                # long thinking, then a reply
    srv, seen = _recording_server([_thinking_stream(30_000), long_then_done])
    llm = _client(monkeypatch, srv, idle=5)
    r = llm.create(system="s", messages=[{"role": "user", "content": "go"}], tools=[], max_tokens=8000,
                   thinking_budget=4000, timeout=30)
    assert len(seen) == 2 and r["stop_reason"] == "tool_use"
    srv.shutdown()



def test_stream_with_only_pings_is_treated_as_stalled(monkeypatch):
    def pings_forever(h):
        _write(h, sse(START))
        for _ in range(40):                                         # 8 s of keep-alive, no content
            _write(h, sse({"type": "ping"}))
            REAL_SLEEP(0.2)
    srv = _server(pings_forever)
    llm = _client(monkeypatch, srv, idle=0.5)
    t0 = time.time()
    with pytest.raises(LLMError, match="no new content"):
        llm.create(system="s", messages=[], tools=[], max_tokens=10, timeout=30)
    assert time.time() - t0 < 3.0                                   # 2 tries x ~0.7 s, not 8 s or 30 s
    srv.shutdown()


def test_long_thinking_is_cut_by_time_even_if_the_streamed_summary_is_small(monkeypatch):
    """Claude 4 models stream a summary of their thinking: a runaway turn sends few characters, so the
    cut must also work by time."""
    def slow_small_thinking(h):
        _write(h, sse(START, {"type": "content_block_start", "index": 0,
                              "content_block": {"type": "thinking", "thinking": ""}}))
        for _ in range(20):                                         # 4 s of tiny summary deltas
            _write(h, sse({"type": "content_block_delta", "index": 0,
                           "delta": {"type": "thinking_delta", "thinking": "."}}))
            REAL_SLEEP(0.2)
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            seen.append(json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                (slow_small_thinking if len(seen) == 1 else lambda h: _write(h, full_stream()))(self)
            except OSError:
                pass                                                # client hung up: the cut working

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    llm = _client(monkeypatch, srv, idle=5)
    monkeypatch.setattr(llm_mod, "THINK_TIME_CAP_S", 0.6)
    t0 = time.time()
    r = llm.create(system="s", messages=[{"role": "user", "content": "go"}], tools=[], max_tokens=8000,
                   thinking_budget=4000, timeout=30)
    assert time.time() - t0 < 2.5                                   # cut at ~0.6 s, not after 4 s
    assert len(seen) == 2 and "think briefly" in json.dumps(seen[1]["messages"][-1]).lower()
    assert r["stop_reason"] == "tool_use"
    srv.shutdown()


def test_retry_carries_the_cut_thinking_so_it_is_not_redone(monkeypatch):
    """B2 run: two turns were cut at 75 s and each retry redid the same analysis from scratch."""
    cut = sse(START, {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
              {"type": "content_block_delta", "index": 0,
               "delta": {"type": "thinking_delta", "thinking": "Bug 2 is the min() in _send_batch. "}},
              *[{"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "x" * 500}}
                for _ in range(60)])
    srv, seen = _recording_server([cut, full_stream()])
    llm = _client(monkeypatch, srv, idle=5)
    r = llm.create(system="s", messages=[{"role": "user", "content": "go"}], tools=[], max_tokens=8000,
                   thinking_budget=4000, timeout=30)
    assert r["stop_reason"] == "tool_use" and len(seen) == 2
    nudge = seen[1]["messages"][-1]["content"][-1]["text"]
    assert "<previous_reasoning>" in nudge and "x" * 100 in nudge
    assert len(nudge) < llm_mod.CARRY_MAX_CHARS + 1000                  # long thinking is trimmed
    assert llm_mod.overthink_nudge("") == llm_mod.OVERTHINK_NUDGE
    srv.shutdown()
