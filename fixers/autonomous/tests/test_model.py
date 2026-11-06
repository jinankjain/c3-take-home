import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import fixer.model as model
from fixer.model import BudgetExhausted, CANONICAL_MODEL, ModelClient, ModelError
from fixer.runtime import Deadline, ProcessRunner


TOKEN = "practice-proxy-token-not-a-real-key"


def message(*, content=None, stop_reason="end_turn", usage=None):
    return {"id": "msg-test", "type": "message", "role": "assistant",
            "content": content or [{"type": "text", "text": "done"}],
            "stop_reason": stop_reason,
            "usage": usage if usage is not None else {"input_tokens": 1000, "output_tokens": 200}}


class FakeProxy:
    def __init__(self):
        self.responses = []
        self.requests = []
        self.budget_status = 200
        self.budgets = [{"budget_usd": 3, "remaining_usd": 3}]
        self.get_count = 0
        self.delay = 0
        self.post_started = threading.Event()
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def send(self, status, body, headers=None):
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                extra = headers or {}
                self.send_header("content-length", str(extra.get("declared_length", len(raw))))
                for key, value in extra.items():
                    if key != "declared_length":
                        self.send_header(key, value)
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self):
                state.get_count += 1
                state.requests.append((self.path, None, dict(self.headers)))
                body = state.budgets.pop(0) if len(state.budgets) > 1 else state.budgets[0]
                self.send(state.budget_status, body)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                state.requests.append((self.path, body, dict(self.headers)))
                state.post_started.set()
                status, response, headers = state.responses.pop(0) if state.responses else (200, message(), {})
                if state.delay:
                    time.sleep(state.delay)
                self.send(status, response, headers)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": .02}, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_port

    @property
    def posts(self):
        return [(path, body, headers) for path, body, headers in self.requests if body is not None]

    def client(self, *, budget=3, deadline=None):
        deadline = deadline or Deadline(time.time() + 20)
        return ModelClient(ProcessRunner(deadline), deadline, budget,
                           env={"ANTHROPIC_BASE_URL": self.base,
                                "OPENAI_BASE_URL": self.base + "/v1",
                                "ANTHROPIC_API_KEY": TOKEN,
                                "ANTHROPIC_MODEL": CANONICAL_MODEL})

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)


@pytest.fixture
def proxy():
    proxy = FakeProxy()
    try:
        yield proxy
    finally:
        proxy.close()


def test_native_tools_preserve_blocks_and_account_for_cached_usage(proxy):
    blocks = [{"type": "thinking", "thinking": "inspect the transition", "signature": "signed"},
              {"type": "tool_use", "id": "call-1", "name": "read_file",
               "input": {"path": "src/service.py"}}]
    usage = {"input_tokens": 1000, "output_tokens": 200,
             "cache_read_input_tokens": 20_000, "cache_creation_input_tokens": 400}
    proxy.responses = [(200, message(content=blocks, stop_reason="tool_use", usage=usage), {})]
    client = proxy.client()
    tools = [{"name": "read_file", "description": "Read case source",
              "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}}]
    history = [{"role": "user", "content": "inspect source"}]
    reply = client.messages("stable system", history, tools, reserve=0)
    expected = (1000 * 3 + 200 * 15 + 20_000 * .3 + 400 * 3.75) / 1_000_000
    assert reply.content == blocks and reply.stop_reason == "tool_use"
    assert reply.cost_usd == pytest.approx(expected)
    assert reply.remaining == pytest.approx(3 - expected)
    assert reply.as_dict()["usage"] == usage
    assert client.ledger[-1]["cost_source"] == "computed"
    path, request, headers = proxy.posts[0]
    assert path == "/v1/messages" and request["model"] == CANONICAL_MODEL
    assert request["tools"] == tools and request["system"] == "stable system"
    assert headers["X-Api-Key"] == TOKEN
    history += [{"role": "assistant", "content": reply.content},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-1",
                                                "content": "class Service: pass"}]}]
    client.messages("stable system", history, tools, reserve=0)
    assert proxy.posts[1][1]["messages"][1]["content"] == blocks


def test_reservation_counts_full_input_even_when_cache_hits_are_expected():
    body = {"system": "契約" * 200, "messages": [{"role": "user", "content": "x" * 1000}],
            "tools": [{"name": "read", "input_schema": {"type": "object"}}], "max_tokens": 4096}
    chars = len(json.dumps([body["system"], body["messages"], body["tools"]], ensure_ascii=False))
    assert model.estimate_reservation(body) == pytest.approx(
        (math.ceil(chars / 4) * 3.75 + 4096 * 15) / 1_000_000)


def test_local_budget_rejection_makes_no_paid_request(proxy):
    client = proxy.client(budget=.0001)
    with pytest.raises(BudgetExhausted) as error:
        client.messages("system", [{"role": "user", "content": "repair"}], [], reserve=0)
    assert error.value.reservation_usd > error.value.remaining_usd
    assert proxy.posts == [] and client.spent_usd == 0


@pytest.mark.parametrize("remaining", [.02, -.02])
def test_proxy_402_is_not_retried_and_refreshes_remaining_budget(proxy, remaining):
    proxy.budgets = [{"budget_usd": 3, "remaining_usd": 3},
                    {"budget_usd": 3, "remaining_usd": remaining}]
    proxy.responses = [(402, {"error": {"message": "rejected " + TOKEN}}, {})]
    client = proxy.client()
    with pytest.raises(BudgetExhausted) as error:
        client.messages("system", [{"role": "user", "content": "repair"}], [], reserve=0)
    assert error.value.remaining_usd == pytest.approx(max(0, remaining))
    assert len(proxy.posts) == 1 and proxy.get_count == 2
    assert client.ledger[-1]["cost_usd"] == 0
    assert TOKEN not in str(error.value)


def test_transient_http_error_retries_once_with_bounded_accounting(proxy):
    proxy.responses = [(503, {"error": {"message": "temporary"}},
                        {"retry-after": "Fri, 31 Dec 2099 23:59:59 GMT"}),
                       (200, message(usage={"cost": .04}), {})]
    client = proxy.client()
    reply = client.messages("system", [{"role": "user", "content": "repair"}], [], reserve=0)
    assert len(proxy.posts) == 2 and len(client.ledger) == 2
    assert client.ledger[0]["cost_usd"] == 0
    assert reply.cost == .04 and client.spent_usd == .04


def test_repeated_transient_errors_stop_after_second_attempt(proxy):
    proxy.responses = [(503, {"error": {}}, {}), (503, {"error": {}}, {}),
                       (200, message(), {})]
    client = proxy.client()
    with pytest.raises(ModelError) as error:
        client.messages("system", [{"role": "user", "content": "repair"}], [], reserve=0)
    assert error.value.status == 503 and error.value.retryable
    assert len(proxy.posts) == 2


@pytest.mark.parametrize("reason", ["max_tokens", "model_context_window_exceeded", None])
def test_incomplete_logical_response_never_exposes_tool_arguments(proxy, reason):
    tool = [{"type": "tool_use", "id": "patch-1", "name": "stage_patch",
             "input": {"files": {"src/service.py": "broken candidate"}}}]
    proxy.responses = [(200, message(content=tool, stop_reason=reason), {})]
    client = proxy.client()
    with pytest.raises(ModelError) as error:
        client.messages("system", [{"role": "user", "content": "repair"}], [], reserve=0)
    assert error.value.code == "incomplete_response"
    assert client.spent_usd == pytest.approx(.006)
    assert len(proxy.posts) == 1


def test_duplicate_tool_ids_are_rejected(proxy):
    tool = {"type": "tool_use", "id": "duplicate", "name": "read_file", "input": {}}
    proxy.responses = [(200, message(content=[tool, tool], stop_reason="tool_use"), {})]
    with pytest.raises(ModelError) as error:
        proxy.client().messages("system", [{"role": "user", "content": "repair"}], [], reserve=0)
    assert error.value.code == "protocol_error"


@pytest.mark.parametrize("response,headers", [
    (b'{"type":"message","content":', {}),
    (json.dumps(message()).encode(), {"declared_length": 100_000}),
    (message(content=[{"type": "text", "text": "x" * (model.MAX_RESPONSE_BYTES + 1)}]), {}),
])
def test_partial_or_oversized_wire_response_charges_conservatively(proxy, response, headers):
    proxy.responses = [(200, response, headers)]
    client = proxy.client()
    history = [{"role": "user", "content": "repair"}]
    with pytest.raises(ModelError) as error:
        client.messages("system", history, [], reserve=0)
    assert error.value.code == "incomplete_response"
    assert client.ledger[-1]["cost_source"] == "reservation"
    assert client.spent_usd == pytest.approx(client.estimate_reservation(
        {"system": "system", "messages": history, "max_tokens": 4096}))


def test_worker_timeout_is_absolute_and_unknown_spend_is_reserved(proxy, monkeypatch):
    monkeypatch.setattr(model, "REQUEST_TIMEOUT", .25)
    proxy.delay = 2
    client = proxy.client()
    client._budget_supported = False
    started = time.monotonic()
    with pytest.raises(ModelError) as error:
        client.messages("system", [{"role": "user", "content": "repair"}], [], reserve=0)
    assert error.value.code == "interrupted_request"
    assert time.monotonic() - started < 1.5
    assert proxy.post_started.is_set() and len(proxy.posts) == 1
    assert client.ledger[0]["cost_source"] == "reservation"


def test_stop_event_cancels_blocked_http_without_waiting_for_server(proxy):
    proxy.delay = 2
    deadline = Deadline(time.time() + 20)
    client = proxy.client(deadline=deadline)
    client._budget_supported = False

    def cancel_when_started():
        if proxy.post_started.wait(1):
            deadline.stop_event.set()

    stopper = threading.Thread(target=cancel_when_started, daemon=True)
    stopper.start()
    started = time.monotonic()
    with pytest.raises(ModelError) as error:
        client.messages("system", [{"role": "user", "content": "repair"}], [], reserve=0)
    stopper.join(timeout=1)
    assert error.value.code == "interrupted_request"
    assert time.monotonic() - started < 1.5
    assert client.spent_usd > 0


def test_optional_budget_route_failure_falls_back_without_repeated_queries(proxy):
    proxy.budget_status = 404
    client = proxy.client()
    client.messages("system", [{"role": "user", "content": "repair"}], [], reserve=0)
    client.messages("system", [{"role": "user", "content": "repair"}], [], reserve=0)
    assert proxy.get_count == 1
    assert client.remaining_budget == pytest.approx(3 - .012)


def test_case_import_paths_do_not_enter_http_worker(proxy, tmp_path, monkeypatch):
    marker = tmp_path / "case-module-imported"
    (tmp_path / "json.py").write_text(
        "from pathlib import Path\nPath(%r).write_text('unexpected')\nraise RuntimeError('case module')\n"
        % str(marker))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    reply = proxy.client().messages("system", [{"role": "user", "content": "repair"}], [], reserve=0)
    assert reply.stop_reason == "end_turn" and not marker.exists()


def test_proxy_diagnostics_and_redirects_do_not_expose_credentials(proxy, capsys):
    proxy.responses = [(302, {"error": {"message": TOKEN}},
                        {"location": proxy.base + "/redirect-target"})]
    with pytest.raises(ModelError) as error:
        proxy.client().messages("system", [{"role": "user", "content": "repair"}], [], reserve=0)
    assert error.value.status == 302
    assert not any(path == "/redirect-target" for path, _, _ in proxy.requests)
    assert TOKEN not in str(error.value)
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err


def test_model_configuration_pins_the_permitted_model(proxy):
    deadline = Deadline(time.time() + 20)
    with pytest.raises(ModelError) as error:
        ModelClient(ProcessRunner(deadline), deadline, 3,
                    env={"ANTHROPIC_BASE_URL": proxy.base, "ANTHROPIC_API_KEY": TOKEN,
                         "ANTHROPIC_MODEL": "another-model"})
    assert error.value.code == "configuration_error" and proxy.requests == []
