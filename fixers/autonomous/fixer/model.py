"""Bounded Anthropic Messages calls through the assessment proxy.

HTTP runs in an isolated Python subprocess. The controller can therefore cancel a
request at an absolute deadline even when urllib is blocked waiting for a body.
The worker prints one bounded JSON envelope; partial responses never reach tools.
"""
from __future__ import annotations

import http.client
import json
import math
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


CANONICAL_MODEL = "anthropic/claude-sonnet-4.6"
MAX_REQUEST_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 512 * 1024
MAX_ERROR_BYTES = 8 * 1024
MAX_WORKER_OUTPUT_BYTES = 768 * 1024
REQUEST_TIMEOUT = 120.0
TRANSIENT_STATUSES = {408, 429, 500, 502, 503, 504}
_CASE_BUDGET_MESSAGE = re.compile(
    r"budget exhausted: this request may cost up to \$[0-9]+\.[0-9]{4}, "
    r"remaining \$-?[0-9]+\.[0-9]{4}\Z"
)
_USAGE_FIELDS = (
    "cost", "input_tokens", "output_tokens", "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


class ModelError(RuntimeError):
    """A failed request, with no response suitable for executing tools."""

    def __init__(self, message: str, *, status: int | None = None,
                 code: str = "model_error", retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable


class BudgetExhausted(ModelError):
    """The requested reservation does not fit; the caller can shrink its request."""

    def __init__(self, reservation_usd: float, remaining_usd: float):
        super().__init__(
            f"model reservation ${reservation_usd:.4f} refused; estimated available "
            f"${remaining_usd:.4f}; reduce context/output or finish validation",
            status=402, code="budget_exhausted")
        self.reservation_usd = reservation_usd
        self.remaining_usd = remaining_usd


@dataclass(frozen=True)
class ModelReply:
    content: list[dict[str, Any]]
    stop_reason: str
    usage: dict[str, Any]
    cost_usd: float
    remaining_usd: float

    @property
    def cost(self) -> float:
        return self.cost_usd

    @property
    def remaining(self) -> float:
        return self.remaining_usd

    def as_dict(self) -> dict[str, Any]:
        return {"content": self.content, "stop_reason": self.stop_reason,
                "usage": self.usage, "cost_usd": self.cost_usd,
                "remaining_usd": self.remaining_usd}


def estimate_reservation(body: Mapping[str, Any]) -> float:
    """Match the pack's conservative reservation, including uncached input prices.

    Cache hits reduce settlement, but do not reduce the initial reservation.
    """
    chars = len(json.dumps([body.get("system"), body.get("messages"),
                           body.get("tools")], ensure_ascii=False))
    max_tokens = min(int(body.get("max_tokens", 4096)), 64_000)
    return (math.ceil(chars / 4) * 3.75 + max_tokens * 15.0) / 1_000_000


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            value = float(value)
        except (OverflowError, ValueError):
            return None
        if math.isfinite(value) and value >= 0:
            return value
    return None


def _error_kind(status: int, body: Any) -> str | None:
    """Recognize the pack's exact budget error without returning its text."""
    if status != 402:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return "payment_rejected"
    native = body.get("type") == "error" and error.get("type") == "billing_error"
    openai = (error.get("type") == "c3_proxy"
              and type(error.get("code")) is int and error["code"] == 402)
    message = error.get("message")
    if (native or openai) and isinstance(message, str) and len(message) <= 1024:
        if _CASE_BUDGET_MESSAGE.fullmatch(message) is not None:
            return "case_budget"
    return "payment_rejected"


def _safe_error_usage(body: Any) -> dict | None:
    usage = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(usage, dict):
        return None
    # Unknown fields and string values can echo request headers. Invalid known
    # values become null, preserving conservative accounting for malformed usage.
    return {key: _number(usage[key]) for key in _USAGE_FIELDS if key in usage}


def _usage_cost(usage: Any, reservation: float) -> tuple[float, str]:
    if not isinstance(usage, dict):
        return reservation, "reservation"
    reported = _number(usage.get("cost"))
    if reported is not None:
        return reported, "upstream"
    counts = [_number(usage.get(key, 0)) for key in
              ("input_tokens", "output_tokens", "cache_read_input_tokens",
               "cache_creation_input_tokens")]
    if any(value is None for value in counts) or not any(
            key in usage for key in ("input_tokens", "output_tokens")):
        return reservation, "reservation"
    uncached, output, cached, writes = counts
    return (uncached * 3.0 + output * 15.0 + cached * .30
            + writes * 3.75) / 1_000_000, "computed"


def _validate_message(body: Any) -> tuple[list[dict[str, Any]], str]:
    if (not isinstance(body, dict) or body.get("type") != "message"
            or body.get("role") != "assistant"):
        raise ModelError("proxy returned an invalid Messages response", code="protocol_error")
    reason = body.get("stop_reason")
    if reason not in {"end_turn", "tool_use", "stop_sequence"}:
        raise ModelError("model response is incomplete or stopped unexpectedly; discarded",
                         code="incomplete_response")
    content = body.get("content")
    if not isinstance(content, list) or not content:
        raise ModelError("model returned no usable content", code="protocol_error")
    seen_tools = set()
    for block in content:
        if not isinstance(block, dict):
            raise ModelError("invalid model content block", code="protocol_error")
        kind = block.get("type")
        if kind == "tool_use":
            if (not isinstance(block.get("id"), str) or not block["id"]
                    or block["id"] in seen_tools
                    or not isinstance(block.get("name"), str) or not block["name"]
                    or not isinstance(block.get("input"), dict)):
                raise ModelError("invalid or duplicate model tool call", code="protocol_error")
            seen_tools.add(block["id"])
        elif kind == "text":
            if not isinstance(block.get("text"), str):
                raise ModelError("invalid model text", code="protocol_error")
        elif kind == "thinking":
            if (not isinstance(block.get("thinking"), str)
                    or not isinstance(block.get("signature"), str)):
                raise ModelError("invalid thinking block", code="protocol_error")
        elif kind == "redacted_thinking":
            if not isinstance(block.get("data"), str):
                raise ModelError("invalid redacted thinking block", code="protocol_error")
        else:
            raise ModelError("unsupported model content block", code="protocol_error")
    if bool(seen_tools) != (reason == "tool_use"):
        raise ModelError("model tool calls and stop reason disagree", code="protocol_error")
    return content, reason


class ModelClient:
    """Sequential client with usage accounting and at most one transient retry."""

    def __init__(self, runner: Any, deadline: Any, budget_usd: float,
                 env: Mapping[str, str] | None = None):
        self.runner = runner
        self.deadline = deadline
        self.env = dict(os.environ if env is None else env)
        budget = _number(budget_usd)
        if budget is None:
            raise ModelError("invalid model budget", code="configuration_error")
        self.budget_usd = budget
        self.spent_usd = 0.0
        self.ledger: list[dict[str, Any]] = []
        self._budget_supported: bool | None = None
        self._remote_remaining_usd: float | None = None
        self._payment_rejected = False
        self._base = self.env.get("ANTHROPIC_BASE_URL", "").rstrip("/")
        parsed = urllib.parse.urlsplit(self._base)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ModelError("ANTHROPIC_BASE_URL must name the model proxy", code="configuration_error")
        self._key = self.env.get("ANTHROPIC_API_KEY", "")
        if not self._key:
            raise ModelError("ANTHROPIC_API_KEY is missing", code="configuration_error")
        if self.env.get("ANTHROPIC_MODEL", CANONICAL_MODEL) != CANONICAL_MODEL:
            raise ModelError("only the permitted Sonnet model can be used", code="configuration_error")

    @property
    def remaining_budget(self) -> float:
        return max(0.0, self.budget_usd - self.spent_usd)

    @staticmethod
    def estimate_reservation(body: Mapping[str, Any]) -> float:
        return estimate_reservation(body)

    def _remaining_time(self) -> float:
        remaining = self.deadline.remaining
        return float(remaining() if callable(remaining) else remaining)

    def _timeout(self, requested: float, reserve: float) -> float:
        from .runtime import TimeLimitError

        try:
            return self.deadline.timeout(requested, reserve=reserve)
        except TimeLimitError:
            raise ModelError("no model time remains before final validation", code="deadline") from None

    def _http(self, method: str, url: str, body: dict | None,
              *, timeout: float, reserve: float) -> dict[str, Any]:
        payload = json.dumps({"method": method, "url": url, "body": body,
                              "headers": {"x-api-key": self._key,
                                          "anthropic-version": "2023-06-01",
                                          "content-type": "application/json"},
                              "timeout": timeout}, ensure_ascii=False)
        if len(payload.encode()) > MAX_REQUEST_BYTES:
            raise ModelError("model request exceeds local size limit", code="request_too_large")
        worker = Path(__file__).resolve()
        from .runtime import TimeLimitError

        try:
            result = self.runner.run(
                [sys.executable, "-I", str(worker), "--http-worker"],
                cwd=worker.parent.parent, timeout=timeout, reserve=reserve,
                env={"PYTHONPATH": "", "PYTHONUNBUFFERED": "1"}, input_text=payload)
        except TimeLimitError:
            raise ModelError("no HTTP worker slot remains before the deadline", code="deadline") from None
        except OSError:
            raise ModelError("model HTTP worker could not start", code="worker_start_error") from None
        if result.timed_out or result.cancelled:
            raise ModelError("model request timed out or was cancelled", code="interrupted_request")
        if result.returncode != 0 or getattr(result, "truncated", False):
            raise ModelError("model HTTP worker failed or its output was truncated", code="worker_error")
        try:
            envelope = json.loads(result.stdout)
        except (ValueError, TypeError):
            raise ModelError("model HTTP worker returned incomplete JSON", code="worker_error") from None
        if not isinstance(envelope, dict) or not isinstance(envelope.get("status"), int):
            raise ModelError("model HTTP worker returned an invalid envelope", code="worker_error")
        return envelope

    def refresh_budget(self, *, reserve: float = 90) -> bool:
        """Use the pack's optional budget route, keeping a conservative fallback ledger."""
        if self._budget_supported is False or self.deadline.stop_event.is_set():
            return False
        try:
            timeout = self._timeout(3.0, reserve)
        except ModelError:
            return False
        openai_base = self.env.get("OPENAI_BASE_URL", self._base + "/v1").rstrip("/")
        try:
            response = self._http("GET", openai_base + "/budget", None,
                                  timeout=timeout, reserve=reserve)
        except ModelError:
            self._budget_supported = False
            return False
        body = response.get("body")
        if response["status"] != 200 or not response.get("complete") or not isinstance(body, dict):
            self._budget_supported = False
            return False
        raw_remaining = body.get("remaining_usd")
        try:
            remaining = (float(raw_remaining)
                         if isinstance(raw_remaining, (int, float))
                         and not isinstance(raw_remaining, bool) else None)
        except (OverflowError, ValueError):
            remaining = None
        if remaining is not None:
            remaining = max(0.0, remaining) if math.isfinite(remaining) else None
        remote_budget = _number(body.get("budget_usd"))
        if remaining is None:
            self._budget_supported = False
            return False
        if remote_budget is not None:
            self.budget_usd = min(self.budget_usd, remote_budget)
        self._remote_remaining_usd = min(self.budget_usd, remaining)
        # An interrupted request may still hold a reservation. Never reclaim its
        # local worst-case charge based on a response that could be racing it.
        self.spent_usd = max(self.spent_usd, self.budget_usd - remaining)
        self._budget_supported = True
        return True

    def _charge(self, cost: float, source: str, status: int | None) -> None:
        self.spent_usd += cost
        self.ledger.append({"request": len(self.ledger) + 1, "status": status,
                            "cost_usd": cost, "cost_source": source,
                            "remaining_usd": self.remaining_budget})

    @staticmethod
    def _payment_error() -> ModelError:
        return ModelError(
            "upstream model payment was rejected; check the upstream account balance",
            status=402, code="upstream_payment_error", retryable=False,
        )

    def messages(self, system: str, messages: list, tools: list,
                 max_tokens: int = 4096, reserve: float = 90, *,
                 tool_choice: dict[str, str] | None = None) -> ModelReply:
        if (not isinstance(system, str) or not isinstance(messages, list) or not messages
                or not isinstance(tools, list) or not isinstance(max_tokens, int)
                or isinstance(max_tokens, bool) or not 1 <= max_tokens <= 64_000):
            raise ModelError("invalid Messages request arguments", code="configuration_error")
        if tool_choice is not None:
            if (not isinstance(tool_choice, dict) or set(tool_choice) != {"type", "name"}
                    or tool_choice.get("type") != "tool"
                    or not isinstance(tool_choice.get("name"), str)
                    or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tool_choice["name"]) is None
                    or sum(isinstance(tool, dict) and tool.get("name") == tool_choice["name"]
                           for tool in tools) != 1):
                raise ModelError("tool_choice must name one declared local tool", code="configuration_error")
        if self._payment_rejected:
            raise self._payment_error()
        body = {"model": CANONICAL_MODEL, "system": system, "messages": messages,
                "max_tokens": max_tokens, "stream": False}
        if tools:
            body["tools"] = tools
        if tool_choice is not None:
            body["tool_choice"] = {"type": "tool", "name": tool_choice["name"]}
        # Explicit thinking is opt-in; the short tool loop remains affordable by
        # default. Preserve returned signed blocks for native continuation.
        try:
            thinking = int(self.env.get("FIXER_THINKING_TOKENS", "0"))
        except ValueError:
            raise ModelError("invalid thinking configuration", code="configuration_error") from None
        if thinking >= 1024 and max_tokens > 2048:
            if tool_choice is not None:
                raise ModelError("forced tool_choice is incompatible with explicit thinking", code="configuration_error")
            body["thinking"] = {"type": "enabled", "budget_tokens": min(
                thinking, 16_000, max_tokens - 1024)}
        reservation = estimate_reservation(body)
        if self._budget_supported is None:
            self.refresh_budget(reserve=reserve)
        for attempt in range(2):
            if self.deadline.stop_event.is_set() or self._remaining_time() <= reserve:
                raise ModelError("no model time remains before final validation", code="deadline")
            if reservation > self.remaining_budget + 1e-9:
                raise BudgetExhausted(reservation, self.remaining_budget)
            timeout = self._timeout(REQUEST_TIMEOUT, reserve)
            try:
                response = self._http("POST", self._base + "/v1/messages", body,
                                      timeout=timeout, reserve=reserve)
            except ModelError as error:
                if error.code not in {"request_too_large", "deadline", "worker_start_error"}:
                    self._charge(reservation, "reservation", None)
                    self.refresh_budget(reserve=reserve)
                raise error
            status = response["status"]
            result = response.get("body")
            if status == 200:
                usage = result.get("usage") if isinstance(result, dict) else None
                cost, source = _usage_cost(usage, reservation)
                self._charge(cost, source, status)
                if not response.get("complete"):
                    raise ModelError("model response is incomplete; discarded", code="incomplete_response")
                content, reason = _validate_message(result)
                return ModelReply(content, reason, usage or {}, cost, self.remaining_budget)
            if status == 402:
                usage = result.get("usage") if isinstance(result, dict) else None
                cost, source = (_usage_cost(usage, reservation) if isinstance(usage, dict)
                                else (0.0, "rejected"))
                self._charge(cost, source, status)
                fresh_budget = self.refresh_budget(reserve=reserve)
                if (response.get("error_kind") == "case_budget"
                        or (fresh_budget and self._remote_remaining_usd is not None
                            and reservation > self._remote_remaining_usd + 1e-9)):
                    raise BudgetExhausted(reservation, self.remaining_budget)
                self._payment_rejected = True
                raise self._payment_error()
            # Explicit HTTP errors settle at zero unless usage reports a charge.
            # A transport failure has an unknown outcome, so keep its reservation.
            usage = result.get("usage") if isinstance(result, dict) else None
            if status == 0:
                cost, source = reservation, "reservation"
            elif isinstance(usage, dict):
                cost, source = _usage_cost(usage, reservation)
            else:
                cost, source = 0.0, "http_error"
            self._charge(cost, source, status)
            retryable = status in TRANSIENT_STATUSES or status == 0
            if retryable and attempt == 0:
                delay = min(2.0, max(.25, _number(response.get("retry_after")) or .25))
                if self._remaining_time() > reserve + delay + 1:
                    if not self.deadline.stop_event.wait(delay):
                        continue
            raise ModelError(f"proxy request failed (HTTP {status})", status=status,
                             code="http_error", retryable=retryable)
        raise ModelError("model retry limit reached")  # Defensive; the loop always returns or raises.


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_worker() -> int:
    """Read request secrets from stdin, never argv, and emit no raw error bodies."""
    status = 0
    envelope: dict[str, Any] = {"status": 0, "complete": False, "body": None}
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("request_size")
        request = json.loads(raw)
        method = request["method"]
        data = None if method == "GET" else json.dumps(request["body"]).encode()
        req = urllib.request.Request(request["url"], data=data,
                                     headers=request["headers"], method=method)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            response = opener.open(req, timeout=max(.05, float(request["timeout"])))
        except urllib.error.HTTPError as error:
            response = error
        with response:
            status = response.code
            limit = MAX_RESPONSE_BYTES if status == 200 else MAX_ERROR_BYTES
            raw = response.read(limit + 1)
            try:
                retry_after = _number(float(response.headers.get("retry-after", "0")))
            except ValueError:
                retry_after = None
            declared_length = response.headers.get("content-length")
        envelope["status"] = status
        if status == 402:
            envelope["error_kind"] = "payment_rejected"
        if len(raw) > limit:
            envelope["error"] = "response_size"
        elif declared_length is not None and int(declared_length) != len(raw):
            envelope["error"] = "response_length"
        else:
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeError, RecursionError):
                body = None
            if status == 200 and isinstance(body, dict):
                envelope.update(body=body, complete=True)
            elif status != 200:
                # Preserve only accounting data, not an upstream diagnostic that
                # might echo headers. The controller logs a generic status.
                usage = _safe_error_usage(body)
                envelope.update(body={"usage": usage} if usage is not None else None,
                                complete=True)
                kind = _error_kind(status, body)
                if kind is not None:
                    envelope["error_kind"] = kind
            if retry_after:
                envelope["retry_after"] = retry_after
    except (ValueError, KeyError, TypeError, RecursionError, urllib.error.URLError,
            http.client.HTTPException, OSError) as error:
        envelope.update(status=status, error=type(error).__name__)
    encoded = json.dumps(envelope, ensure_ascii=False)
    if len(encoded.encode()) > MAX_WORKER_OUTPUT_BYTES:
        encoded = json.dumps({"status": status, "complete": False,
                              "body": None, "error": "response_size"})
    print(encoded, flush=True)
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--http-worker"]:
        raise SystemExit(_http_worker())
    raise SystemExit("use --http-worker for the isolated HTTP helper")
