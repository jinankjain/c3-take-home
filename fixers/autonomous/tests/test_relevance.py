"""Jev integration uses a local fake proxy, never a paid model endpoint."""

import json
import time

import pytest

import fixer.relevance as relevance
from fixer.model import CANONICAL_MODEL
from fixer.runtime import Deadline
from test_model import FakeProxy, TOKEN, message


def document(path="platform/notes.md", text="Lease observations from another mechanism."):
    return {"path": path, "title": "Historical notes", "status": "UNKNOWN", "text": text}


def decisions(probabilities, *, cost=.0002, model="typesafe/jev-1.13-20260917"):
    return {"model": model,
            "answers": {f"doc_{index}": {"type": "noul", "noul": probability}
                        for index, probability in enumerate(probabilities)},
            "usage": {"cost": cost, "input_tokens": 2000, "output_tokens": 0}}


@pytest.fixture
def proxy():
    proxy = FakeProxy()
    try:
        yield proxy
    finally:
        proxy.close()


def client_for(proxy, *, budget=3, deadline=None):
    client = proxy.client(budget=budget, deadline=deadline)
    client.env["JEV_DECISIONS_URL"] = proxy.base + "/api/alpha/decisions"
    client.env["JEV_MODEL"] = relevance.JEV_MODEL
    return client


def test_unconfigured_relevance_stays_lexical_without_http(proxy):
    client = proxy.client()
    assert not client.relevance("lease revoke", [document()], reserve=0)
    assert proxy.requests == [] and client.ledger == []


@pytest.mark.parametrize("url", [
    "https://example.invalid/api/alpha/decisions",
    "http://127.0.0.1:1/api/alpha/decisions",
    "{base}/v1/messages", "{base}/api/alpha/decisions?key=echo",
    "{base}/api/alpha/decisions#fragment", "ftp://example.invalid/alpha/decisions",
    "http://user:password@127.0.0.1/api/alpha/decisions",
])
def test_case_token_cannot_be_sent_to_another_origin_or_route(proxy, url):
    client = client_for(proxy)
    client.env["JEV_DECISIONS_URL"] = url.format(base=proxy.base)
    assert not client.relevance("lease revoke", [document()], reserve=0)
    assert proxy.requests == [] and client.spent_usd == 0


def test_unsupported_jev_model_makes_no_request(proxy):
    client = client_for(proxy)
    client.env["JEV_MODEL"] = CANONICAL_MODEL
    assert not client.relevance("lease revoke", [document()], reserve=0)
    assert proxy.requests == []


def test_actual_typed_decisions_payload_and_shared_upstream_accounting(proxy):
    proxy.responses = [(200, decisions([.02, .95, .50]), {})]
    client = client_for(proxy)
    docs = [document(f"platform/doc-{index}.md") for index in range(3)]
    assert client.relevance("lease revoke", docs, reserve=0) == {
        docs[0]["path"]: False, docs[1]["path"]: True}
    path, body, headers = proxy.posts[0]
    assert path == "/api/alpha/decisions"
    assert set(body) == {"model", "state", "questions"}
    assert body["model"] == relevance.JEV_MODEL
    assert body["state"]["query"] == "lease revoke"
    assert set(body["questions"]) == {"doc_0", "doc_1", "doc_2"}
    assert body["state"]["documents"]["doc_0"]["path"] == docs[0]["path"]
    assert all(question["type"] == "noul" and set(question["criteria"]) == {"true", "false"}
               for question in body["questions"].values())
    assert headers["X-Api-Key"] == TOKEN
    assert client.spent_usd == pytest.approx(.0002)
    assert client.ledger[-1]["model"] == relevance.JEV_MODEL
    assert client.ledger[-1]["purpose"] == "relevance"
    proxy.responses = [(200, message(), {})]
    reply = client.messages("repair", [{"role": "user", "content": "inspect"}], [], reserve=0)
    assert reply.remaining == pytest.approx(3 - .0002 - reply.cost)


@pytest.mark.parametrize("value", [None, True, "0.01", -1, 1.01, float("nan"), float("inf")])
def test_invalid_or_uncertain_noul_values_cannot_remove_a_document(proxy, value):
    proxy.responses = [(200, decisions([value]), {})]
    client = client_for(proxy)
    assert not client.relevance("lease revoke", [document()], reserve=0)
    assert client.spent_usd == pytest.approx(.0002)


@pytest.mark.parametrize("mutate", [
    lambda body: body.update(model="another/model"),
    lambda body: body.update(answers=[]),
    lambda body: body["answers"].update(unexpected={"type": "noul", "noul": 0}),
    lambda body: body["answers"]["doc_0"].update(type="text", noul=0),
    lambda body: body["answers"].pop("doc_0"),
])
def test_incompatible_or_missing_answer_protocol_preserves_lexical_results(proxy, mutate):
    body = decisions([0])
    mutate(body)
    proxy.responses = [(200, body, {})]
    assert not client_for(proxy).relevance("lease revoke", [document()], reserve=0)


def test_missing_cost_uses_jev_proxy_reservation_instead_of_sonnet_token_prices(proxy):
    body = decisions([0], cost=None)
    body["usage"]["input_tokens"] = 1_000_000
    proxy.responses = [(200, body, {})]
    client = client_for(proxy)
    assert client.relevance("lease revoke", [document()], reserve=0) == {"platform/notes.md": False}
    expected = relevance._reservation(proxy.posts[0][1])
    assert client.spent_usd == pytest.approx(expected)
    assert client.ledger[-1]["cost_source"] == "reservation"
    assert expected < .01


def test_cache_is_per_case_and_bound_to_query_and_document_content(proxy):
    proxy.responses = [(200, decisions([0]), {}), (200, decisions([1]), {}),
                       (200, decisions([0]), {})]
    client = client_for(proxy)
    doc = document()
    assert client.relevance("lease revoke", [doc], reserve=0) == {doc["path"]: False}
    assert client.relevance("lease revoke", [doc], reserve=0) == {doc["path"]: False}
    assert len(proxy.posts) == 1
    changed = {**doc, "text": "Now this excerpt describes revocation."}
    assert client.relevance("lease revoke", [changed], reserve=0) == {doc["path"]: True}
    assert client.relevance("clock skew", [doc], reserve=0) == {doc["path"]: False}
    assert len(proxy.posts) == 3
    another = client_for(proxy)
    proxy.responses = [(200, decisions([1]), {})]
    assert another.relevance("lease revoke", [doc], reserve=0) == {doc["path"]: True}
    assert len(proxy.posts) == 4


def test_failed_identical_decision_is_cached_and_not_repeated(proxy):
    proxy.responses = [(503, {"error": {"message": "echo " + TOKEN}}, {})]
    client = client_for(proxy)
    for _ in range(3):
        assert not client.relevance("lease revoke", [document()], reserve=0)
    assert len(proxy.posts) == 1 and client.spent_usd == 0


def test_complete_content_hash_invalidates_cache_when_the_sent_excerpt_is_identical(proxy):
    proxy.responses = [(200, decisions([0]), {}), (200, decisions([1]), {})]
    client = client_for(proxy)
    first = {**document(text="same bounded excerpt"), "content_hash": "a" * 64}
    second = {**first, "content_hash": "b" * 64}
    assert client.relevance("lease revoke", [first], reserve=0) == {first["path"]: False}
    assert client.relevance("lease revoke", [second], reserve=0) == {first["path"]: True}
    assert len(proxy.posts) == 2
    assert proxy.posts[0][1]["state"] == proxy.posts[1][1]["state"]


def test_jev_payment_rejection_is_redacted_and_does_not_poison_sonnet(proxy, capsys):
    proxy.responses = [(402, {"error": {"message": "echo " + TOKEN},
                              "usage": {"cost": .004, "input_tokens": TOKEN,
                                        "request_headers": {"x-api-key": TOKEN}}}, {}),
                       (200, message(), {})]
    client = client_for(proxy)
    assert not client.relevance("lease revoke", [document()], reserve=0)
    assert not client.relevance("other query", [document()], reserve=0)
    assert not client._payment_rejected
    assert client.spent_usd == pytest.approx(.004)
    reply = client.messages("repair", [{"role": "user", "content": "inspect"}], [], reserve=0)
    assert reply.stop_reason == "end_turn" and len(proxy.posts) == 2
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err + json.dumps(client.ledger)


def test_unknown_jev_402_refreshes_the_shared_case_budget_without_a_payment_flag(proxy):
    proxy.budgets = [{"budget_usd": 3, "remaining_usd": 3},
                    {"budget_usd": 3, "remaining_usd": 0}]
    proxy.responses = [(402, {"error": {"message": "budget exhausted"}}, {})]
    client = client_for(proxy)
    assert not client.relevance("lease revoke", [document()], reserve=0)
    assert client.remaining_budget == 0 and not client._payment_rejected
    assert len(proxy.posts) == 1


def test_billed_jev_error_with_unavailable_budget_route_retains_only_numeric_accounting(proxy, capsys):
    proxy.budget_status = 404
    proxy.responses = [(402, {"error": {"message": "echo " + TOKEN},
                              "usage": {"cost": .003, "output_tokens": TOKEN,
                                        "unknown_echo": TOKEN}}, {})]
    client = client_for(proxy)
    assert not client.relevance("lease revoke", [document()], reserve=0)
    assert client.spent_usd == pytest.approx(.003)
    assert client.remaining_budget == pytest.approx(2.997)
    assert client.ledger[-1]["cost_source"] == "upstream"
    assert len(proxy.posts) == 1 and proxy.get_count == 1
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err + json.dumps(client.ledger)


@pytest.mark.parametrize("state", ["cancelled", "payment", "deadline", "funds"])
def test_relevance_preserves_validation_time_and_sonnet_funds(proxy, state):
    deadline = Deadline(time.time() + (1 if state == "deadline" else 20))
    client = client_for(proxy, budget=.1 if state == "funds" else 3, deadline=deadline)
    if state == "cancelled":
        deadline.stop_event.set()
    if state == "payment":
        client._payment_rejected = True
    assert not client.relevance("lease revoke", [document()], reserve=2 if state == "deadline" else 0)
    assert proxy.posts == [] and client.ledger == []


def test_request_and_case_spend_caps_stop_new_decisions_but_allow_free_cache_hits(proxy, monkeypatch):
    monkeypatch.setattr(relevance, "MAX_CASE_REQUESTS", 2)
    proxy.responses = [(200, decisions([0]), {}), (200, decisions([1]), {})]
    client = client_for(proxy)
    assert client.relevance("lease revoke", [document()], reserve=0)
    assert client.relevance("clock drift", [document()], reserve=0)
    assert not client.relevance("heartbeat reports", [document()], reserve=0)
    assert len(proxy.posts) == 2
    assert client.relevance("lease revoke", [document()], reserve=0) == {"platform/notes.md": False}
    another = client_for(proxy)
    another._jev_relevance = relevance.JevRelevance(another)
    another._jev_relevance.spent_usd = another._jev_relevance.spend_limit
    assert not another.relevance("lease revoke", [document()], reserve=0)
    assert len(proxy.posts) == 2


def test_cache_entries_and_request_documents_are_bounded(proxy, monkeypatch):
    monkeypatch.setattr(relevance, "MAX_CACHE_ENTRIES", 3)
    docs = [document(f"platform/doc-{index}.md", "x" * 20_000) for index in range(20)]
    proxy.responses = [(200, decisions([0] * relevance.MAX_DOCUMENTS), {})]
    client = client_for(proxy)
    result = client.relevance("lease revoke", docs, reserve=0)
    assert len(proxy.posts[0][1]["questions"]) == relevance.MAX_DOCUMENTS
    assert all(len(item["text"]) <= relevance.MAX_DOCUMENT_CHARS
               for item in proxy.posts[0][1]["state"]["documents"].values())
    assert len(client._jev_relevance.cache) == 3 and len(result) <= relevance.MAX_DOCUMENTS


def test_cumulative_jev_time_cap_preserves_cache_and_does_not_block_sonnet(proxy):
    proxy.responses = [(200, decisions([0]), {}), (200, message(), {})]
    client = client_for(proxy)
    assert client.relevance("lease revoke", [document()], reserve=0)
    client._jev_relevance.seconds = relevance.MAX_CASE_SECONDS
    assert not client.relevance("clock drift", [document()], reserve=0)
    assert client.relevance("lease revoke", [document()], reserve=0) == {"platform/notes.md": False}
    reply = client.messages("repair", [{"role": "user", "content": "inspect"}], [], reserve=0)
    assert reply.stop_reason == "end_turn" and len(proxy.posts) == 2


def test_deadline_cancels_a_blocked_decision_and_retains_its_reservation(proxy):
    proxy.delay = .6
    proxy.responses = [(200, decisions([0]), {})]
    client = client_for(proxy, deadline=Deadline(time.time() + .3))
    client._budget_supported = False
    started = time.monotonic()
    assert not client.relevance("lease revoke", [document()], reserve=0)
    assert time.monotonic() - started < .55
    assert len(proxy.posts) == 1
    assert client.ledger[-1]["cost_source"] == "reservation"
    assert client.spent_usd >= .001


@pytest.mark.parametrize("query, docs", [
    ("", [document()]), ("x" * 513, [document()]),
    ("lease", [{"path": "../secret", "text": "lease"}]),
    ("lease", [document(), document()]), ("lease", [{"path": "notes", "text": 3}]),
    ("lease", None),
])
def test_bad_relevance_inputs_fall_back_without_http(proxy, query, docs):
    client = client_for(proxy)
    assert not client.relevance(query, docs, reserve=0)
    assert proxy.requests == []
