from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fixer.evidence import MAX_TRACE_CHARS, _matches_error_codes, bounded_result, summarize_trace, trace_slice


def write_trace(path: Path, events: list[dict]):
    path.write_text("".join(json.dumps(event) + "\n" for event in events))


def test_slice_preserves_global_trace_order_and_both_ends(tmp_path):
    path = tmp_path / "trace.jsonl"
    events = [
        {"kind": "log", "event": "job_progress", "job": "tenant:job-7",
         "node": "worker-1", "seq": i, "t": i, "local_t": 100 - i}
        for i in range(50)
    ]
    write_trace(path, events)
    result = trace_slice(path, ["tenant:job-7"], limit=6)
    assert [event["seq"] for event in result["events"]] == [0, 1, 2, 47, 48, 49]
    assert result["matched_events"] == 50
    assert result["selection_truncated"]
    assert not result["scan_truncated"]


def test_entity_and_event_filters_include_nested_rpc_payloads(tmp_path):
    path = tmp_path / "trace.jsonl"
    write_trace(path, [
        {"kind": "msg_send", "seq": 0, "method": "commit", "payload": {"task": "map/abc"}},
        {"kind": "log", "seq": 1, "event": "complete", "task": "map/abc"},
        {"kind": "msg_send", "seq": 2, "method": "commit", "payload": {"task": "map/other"}},
    ])
    result = trace_slice(path, ["map/abc"], event_types=["commit"])
    assert [event["seq"] for event in result["events"]] == [0]


def test_short_action_ids_do_not_match_timestamps_or_sequence_numbers(tmp_path):
    path = tmp_path / "trace.jsonl"
    write_trace(path, [
        {"kind": "client", "seq": 0, "action_id": 1},
        {"kind": "log", "seq": 1, "t": 1, "event": "unrelated"},
    ])
    assert len(trace_slice(path, ["1"])["events"]) == 1


def test_liveness_without_timestamp_follows_action_history(tmp_path):
    path = tmp_path / "trace.jsonl"
    write_trace(path, [
        {"kind": "client", "phase": "start", "seq": 0, "t": 0, "action_id": "c1",
         "action": "submit", "args": {"job_id": "tenant:job-7"}},
        {"kind": "log", "event": "submit_unknown", "seq": 1, "t": 1,
         "job": "tenant:job-7", "op_id": "op/old"},
        {"kind": "fault", "fault": "drop", "seq": 2, "t": 2},
        {"kind": "client", "phase": "start", "seq": 3, "t": 3, "action_id": "c2"},
        {"kind": "client", "phase": "end", "seq": 4, "t": 4, "action_id": "c2", "outcome": "ok"},
    ])
    violations = [{"invariant": "I2", "t": None, "detail": "action c1 never resolved"}]
    summary = {"source_hash": "source-a", "scenario_hash": "scenario-a", "seed": 1,
               "scenario": "probe", "trace_sha256": "trace-a", "messages_sent": 0}
    result = summarize_trace(path, violations, summary)
    assert result["unresolved_actions"][0]["action_id"] == "c1"
    assert "tenant:job-7" in result["affected_entities"]
    assert any(event.get("event") == "submit_unknown" for event in result["timeline"])
    assert result["fault_events"][0]["fault"] == "drop"
    assert result["identity"]["source_hash"] == "source-a"


def test_malformed_line_is_disclosed_and_later_events_are_retained(tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text('not-json\n{"kind":"log","event":"failure","seq":1}\n')
    result = summarize_trace(path, [], {})
    assert result["malformed_lines"] == 1
    assert result["events_scanned"] == 1
    assert result["abnormal_events"][0]["event"] == "failure"


def test_summary_bounds_large_payloads_and_failure_families(tmp_path):
    path = tmp_path / "trace.jsonl"
    write_trace(path, [{"kind": "msg_send", "seq": i, "payload": {"task": "large-task", "blob": "x" * 10000}}
                       for i in range(30)])
    violations = [{"invariant": "I2", "t": None, "detail": f"action {i} never resolved"} for i in range(100)]
    result = summarize_trace(path, violations, {}, max_events=4)
    assert len(result["failure_families"]) == 1
    assert result["failure_families"][0]["count"] == 100
    sliced = trace_slice(path, ["large-task"], limit=4)
    assert len(sliced["events"]) == 4
    assert len(json.dumps(sliced)) < 15000


def test_unlabelled_namespaced_key_has_a_causal_timeline(tmp_path):
    path = tmp_path / "trace.jsonl"
    write_trace(path, [{"kind": "log", "seq": 0, "event": "lease_granted", "key": "tenant:slow-job"},
                       {"kind": "msg_send", "seq": 1, "method": "report", "payload": {"key": "tenant:slow-job"}}])
    result = summarize_trace(path, [{"invariant": "LEASE", "t": 5, "detail": "report for tenant:slow-job was late"}], {})
    assert "tenant:slow-job" in result["affected_entities"]
    assert [event["seq"] for event in result["timeline"]] == [0, 1]


def test_large_trace_output_is_complete_json_with_provenance_and_both_ends(tmp_path):
    path = tmp_path / "trace.jsonl"
    write_trace(path, [{"kind": "log", "event": "progress", "seq": i, "t": i,
                       "op_id": "durable-operation", "payload": {"blob": "\x01" * 2000}}
                      for i in range(400)])
    identity = {"run_id": "run-original", "source_hash": "a" * 64,
                "current_source_hash": "b" * 64, "scenario_hash": "c" * 64}
    result = trace_slice(path, ["durable-operation"], limit=200, provenance=identity)
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
    assert len(encoded) <= MAX_TRACE_CHARS
    assert json.loads(encoded)["source_hash"] == identity["source_hash"]
    assert result["current_source_hash"] == identity["current_source_hash"]
    assert result["events"][0]["seq"] == 0 and result["events"][-1]["seq"] == 399
    assert len(result["events"]) < 200
    assert result["omitted_events"] == 400 - len(result["events"])
    assert result["selection_truncated"] and result["response_truncated"]


def test_time_focus_retains_neighbors_and_linked_request_outside_window(tmp_path):
    path = tmp_path / "trace.jsonl"
    events = [{"kind": "msg_send", "seq": 0, "t": 1, "local_t": 999,
               "payload": {"id": "request-7", "op_id": "operation-7", "method": "commit"}}]
    events += [{"kind": "log", "event": "stats", "seq": i, "t": i} for i in range(1, 50)]
    events += [{"kind": "rpc_error", "seq": 50, "t": 50, "local_t": -999,
                "reply_to": "request-7", "error": {"code": "OVERLOADED"}},
               {"kind": "fault", "seq": 51, "t": 51, "fault": "restart"}]
    write_trace(path, events)
    result = trace_slice(path, [], event_types=["rpc_error"], at=50, window_s=.1,
                         context_lines=1, limit=12)
    assert result["matched_events"] == 1
    assert [event["seq"] for event in result["events"]] == [0, 49, 50, 51]
    assert result["events"][0]["payload"]["op_id"] == "operation-7"
    assert result["events"][2]["error"]["code"] == "OVERLOADED"
    assert result["events"][0]["selection_role"] == "linked_request_or_reply"
    assert result["events"][2]["selection_role"] == "anchor"
    assert result["omitted_events"] == 0


def test_aggregate_rpc_expectation_uses_actual_error_time_and_entity(tmp_path):
    path = tmp_path / "trace.jsonl"
    events = [{"kind": "log", "event": "stats", "seq": i, "t": i / 2} for i in range(200)]
    events[80] = {"kind": "msg_send", "seq": 80, "t": 40,
                  "payload": {"id": "request-9", "op_id": "operation-middle", "method": "submit"}}
    events[81] = {"kind": "msg_send", "seq": 81, "t": 40.5,
                  "payload": {"id": "request-9", "op_id": "operation-middle", "error": {"code": "OVERLOADED"}}}
    events[82] = {"kind": "log", "event": "resource_overload", "seq": 82, "t": 41,
                  "op_id": "operation-middle"}
    write_trace(path, events)
    violations = [{"invariant": "expect", "t": 100,
                   "detail": "rpc errors {'code': 'OVERLOADED', 'method': 'submit'}: 1 > 0"}]
    result = summarize_trace(path, violations, {"source_hash": "a" * 64})
    assert result["causal_focus_reason"] == "checker_error_witnesses"
    assert "operation-middle" in result["affected_entities"]
    assert result["causal_windows"][0]["at"] == 40.5
    assert any(event.get("seq") == 81 for event in result["timeline"])
    assert not any(event.get("seq") == 199 for event in result["timeline"])
    assert result["failure_witnesses"] and result["identity"]["source_hash"] == "a" * 64


def test_cancelled_trace_scan_returns_bounded_partial_evidence(tmp_path):
    path = tmp_path / "trace.jsonl"
    write_trace(path, [{"kind": "log", "event": "progress", "seq": i} for i in range(10)])
    calls = 0

    def stopped():
        nonlocal calls
        calls += 1
        return calls > 1

    result = trace_slice(path, [], provenance={"source_hash": "a" * 64}, should_stop=stopped)
    assert result["scan_cancelled"] and result["scan_truncated"]
    assert [event["seq"] for event in result["events"]] == [0]
    assert result["source_hash"] == "a" * 64


@pytest.mark.parametrize("error", [{"code": "CAPACITY"}, ["CAPACITY", "pool is full"], "CAPACITY: pool is full"])
def test_aggregate_error_windows_exclude_normal_capacity_bookkeeping(tmp_path, error):
    path = tmp_path / "trace.jsonl"
    events = [{"kind": "log", "event": "capacity_reserved", "seq": index, "t": index / 10,
               "job": "tenant:normal-" + str(index)} for index in range(20)]
    events[0] = {"kind": "log", "event": "scenario", "seq": 0, "t": 0,
                 "expect": {"rpc_errors_max": [{"code": "CAPACITY", "max": 0}]}}
    events += [{"kind": "msg_send", "seq": 20, "t": 39,
                "payload": {"id": "request-right", "method": "submit", "op_id": "tenant:failed"}},
               {"kind": "msg_send", "seq": 21, "t": 42,
                "payload": {"id": "request-right", "err": error}},
               {"kind": "log", "event": "capacity_freed", "seq": 22, "t": 43,
                "job": "tenant:normal-last"}]
    write_trace(path, events)
    result = summarize_trace(path, [{"invariant": "expect", "t": 100,
                                    "detail": "rpc errors {'code': 'CAPACITY', 'method': 'submit'}: 1 > 0"}], {})
    assert result["causal_focus_reason"] == "checker_error_witnesses"
    assert [item["seq"] for item in result["failure_witnesses"]] == [21]
    assert [window["at"] for window in result["causal_windows"]] == [42]
    assert any(item["seq"] == 20 for item in result["timeline"]), "linked request outside time window was lost"
    assert not any(item["seq"] == 0 for item in result["timeline"])
    assert all(item["selection_role"] == "neighbor" for item in result["timeline"] if item["seq"] < 20)


@pytest.mark.parametrize("error", [{"code": "OTHER", "message": "CAPACITY was checked"},
                                  ["OTHER", "CAPACITY was checked"], ["CAPACITY_DETAIL", "pool full"]])
def test_typed_error_codes_take_precedence_over_descriptions(error):
    assert not _matches_error_codes({"payload": {"err": error}}, ["capacity"])


def test_error_code_declarations_require_a_failure_context():
    assert not _matches_error_codes({"event": "capacity_reserved", "code": "CAPACITY"}, ["capacity"])
    assert not _matches_error_codes({"type": "req", "payload": {"err": ["CAPACITY", "requested data"]}}, ["capacity"])
    assert _matches_error_codes({"kind": "rpc_error", "code": "CAPACITY"}, ["capacity"])
    assert not _matches_error_codes({"payload": {"outcome": {"code": "CAPACITY"}}}, ["capacity"])


@pytest.mark.parametrize("punctuation", [":", ".", ":."])
def test_prose_entity_punctuation_keeps_the_checker_linked_terminal_event(tmp_path, punctuation):
    path = tmp_path / "trace.jsonl"
    events = [{"kind": "log", "seq": index, "t": 5 + index / 1000,
               "event": "progress", "key": "Tenant/task"} for index in range(100)]
    events[50] = {"kind": "log", "seq": 50, "t": 5.05, "event": "task_terminal",
                  "key": "Tenant/task", "state": "CANCELLED"}
    events += [{"kind": "log", "seq": 100, "t": 20, "event": "effect_done",
                "key": "Tenant/task", "state": "SUCCEEDED"}]
    write_trace(path, events)
    summary = {"source_hash": "a" * 64, "scenario_hash": "b" * 64,
               "trace_sha256": "c" * 64, "seed": 7}
    result = summarize_trace(path, [{"invariant": "CANCELLATION", "t": 5.05,
                                    "detail": f"job Tenant/task{punctuation} CANCELLED but effect SUCCEEDED"}], summary,
                             max_events=12)
    assert "Tenant/task" in result["affected_entities"]
    assert "Tenant/task" + punctuation not in result["affected_entities"]
    assert [event["seq"] for event in result["failure_witnesses"]] == [50]
    assert result["failure_witnesses"][0]["witness_basis"] == "checker_time_entity_fact"
    assert any(event["seq"] == 50 and event.get("required_for_failure") is True
               for event in result["timeline"]), "the dense window lost the exact checker anchor"
    encoded = json.dumps(result, allow_nan=False)
    assert len(encoded) <= MAX_TRACE_CHARS
    assert json.loads(encoded)["identity"]["source_hash"] == summary["source_hash"]


def test_structured_entity_punctuation_is_not_normalized_as_prose(tmp_path):
    path = tmp_path / "trace.jsonl"
    write_trace(path, [{"kind": "log", "seq": 0, "t": 5, "event": "terminal",
                       "key": "Tenant/task:", "state": "FAILED"}])
    result = summarize_trace(path, [{"invariant": "RESULT", "t": 5,
                                    "key": "Tenant/task:", "detail": "terminal state FAILED"}], {})
    assert "Tenant/task:" in result["affected_entities"]
    assert "Tenant/task" not in result["affected_entities"]
    assert result["failure_witnesses"][0]["key"] == "Tenant/task:"


def test_nonerror_invariant_never_labels_normal_capacity_context_as_failure(tmp_path):
    path = tmp_path / "trace.jsonl"
    write_trace(path, [
        {"kind": "log", "seq": 0, "t": 1, "event": "capacity_reserved", "key": "Tenant/other"},
        {"kind": "log", "seq": 1, "t": 5, "event": "capacity_reserved", "key": "Tenant/capacity_reserved"},
        {"kind": "log", "seq": 2, "t": 6, "event": "capacity_freed", "key": "Tenant/capacity_reserved"},
    ])
    result = summarize_trace(path, [{"invariant": "DEADLINE", "t": 5,
                                    "detail": "job Tenant/capacity_reserved: deadline was exceeded"}],
                             {"source_hash": "a" * 64})
    assert result["failure_witnesses"] == []
    assert result["abnormal_events"], "normal bookkeeping should remain available as context"
    assert result["causal_windows"][0]["at"] == 5
    assert "Tenant/capacity_reserved" in result["affected_entities"]
    assert all(not event.get("required_for_failure") for event in result["timeline"])
    assert result["identity"]["source_hash"] == "a" * 64


def test_untyped_invariant_uses_explicit_response_errors_not_request_data(tmp_path):
    path = tmp_path / "trace.jsonl"
    write_trace(path, [
        {"kind": "log", "seq": 0, "t": 0, "event": "scenario", "expect": {"error": "STALE"}},
        {"kind": "msg_send", "seq": 1, "t": 1, "type": "req", "msg_id": 17,
         "payload": {"error": "requested data", "op_id": "Tenant/task"}},
        {"kind": "log", "seq": 2, "t": 2, "event": "capacity_reserved", "config": {"error": "STALE"}},
        {"kind": "msg_send", "seq": 3, "t": 20, "type": "rep", "rpc_id": 17,
         "payload": {"err": ["STALE", "lease no longer valid"]}},
    ])
    result = summarize_trace(path, [{"invariant": "COMPLETE", "t": 100,
                                    "detail": "a remote operation did not complete"}], {})
    assert [event["seq"] for event in result["failure_witnesses"]] == [3]
    assert result["failure_witnesses"][0]["witness_basis"] == "explicit_error"
    assert result["causal_windows"][0]["at"] == 20
    assert any(event["seq"] == 1 for event in result["timeline"]), "the failed RPC lost its request"


@pytest.mark.parametrize("bookkeeping", [
    {"kind": "log", "event": "error_count", "count": 0, "success": True},
    {"kind": "failure_count", "event": "metrics", "count": 0, "success": True},
    {"kind": "x" * 94 + "_error_count", "event": "metrics", "count": 0, "success": True},
    {"kind": "log", "event": "configuration_applied", "meta": {"error": "configured label"}},
    {"kind": "log", "event": "configuration_applied", "metadata": {"error": "configured label"}},
])
def test_counter_names_and_declarations_do_not_become_explicit_failure_witnesses(tmp_path, bookkeeping):
    path = tmp_path / "trace.jsonl"
    write_trace(path, [{**bookkeeping, "seq": 0, "t": 1, "key": "Tenant/other"}])
    result = summarize_trace(path, [{"invariant": "COMPLETE", "t": 100,
                                    "detail": "a remote operation did not complete"}],
                             {"source_hash": "a" * 64})
    assert result["failure_witnesses"] == []
    assert result["causal_windows"][0]["at"] == 100, "unrelated bookkeeping displaced the checker time"
    assert result["timeline"] == []
    assert result["identity"]["source_hash"] == "a" * 64


@pytest.mark.parametrize("kind", ["rpc_error", "transport.exception", "timeout"])
def test_actual_error_kind_tags_remain_explicit_witnesses(tmp_path, kind):
    path = tmp_path / "trace.jsonl"
    write_trace(path, [{"kind": kind, "seq": 0, "t": 12, "op_id": "Tenant/operation"}])
    result = summarize_trace(path, [{"invariant": "COMPLETE", "t": 100,
                                    "detail": "a remote operation did not complete"}], {})
    assert result["failure_witnesses"][0]["kind"] == kind
    assert result["failure_witnesses"][0]["witness_basis"] == "explicit_error"
    assert result["causal_windows"][0]["at"] == 12


def test_named_error_code_in_meta_remains_declaration_data():
    assert not _matches_error_codes({"kind": "log", "event": "configuration_applied",
                                     "meta": {"error": {"code": "CAPACITY"}}}, ["capacity"])


def test_typed_code_on_a_successful_counter_is_not_an_error_witness(tmp_path):
    path = tmp_path / "trace.jsonl"
    counter = {"kind": "log", "event": "error_count", "code": "CAPACITY",
               "count": 0, "success": True, "seq": 0, "t": 1}
    assert not _matches_error_codes(counter, ["capacity"])
    write_trace(path, [counter])
    result = summarize_trace(path, [{"invariant": "expect", "t": 100,
                                    "detail": "rpc errors {'code': 'CAPACITY'}: 1 > 0"}], {})
    assert result["failure_witnesses"] == []
    assert result["causal_windows"][0]["at"] == 100
    assert result["timeline"] == []


def test_response_fallback_is_bounded_and_never_alters_exact_provenance():
    result = bounded_result({"run_id": "run-a", "source_hash": "a" * 64,
                             "current_source_hash": "b" * 64, "seed": 17, "ok": False,
                             "events": [{"detail": "\x01" * 1000} for _ in range(100)],
                             "matched_events": 100, "eligible_events": 100}, max_chars=800)
    assert len(json.dumps(result, ensure_ascii=False, allow_nan=False)) <= 800
    assert result["source_hash"] == "a" * 64 and result["current_source_hash"] == "b" * 64
    assert result["ok"] is False
    assert result["seed"] == 17
    assert result["omitted_events"] == 100 - len(result["events"])
    with pytest.raises(ValueError, match="exact source/run provenance"):
        bounded_result({"source_hash": "a" * 1000}, max_chars=256)

    minimal = bounded_result({"run_id": "run-a", "source_hash": "a" * 64, "seed": 17, "ok": False,
                              "diagnostics": {str(i): "x" * 1000 for i in range(32)}}, max_chars=512)
    assert minimal["diagnostic_bodies_omitted"] and minimal["seed"] == 17
    assert minimal["source_hash"] == "a" * 64 and minimal["ok"] is False


@pytest.mark.parametrize("field,value", [("at", 2 ** 1024), ("window_s", 2 ** 1024),
                                        ("at", float("nan")), ("window_s", True)])
def test_trace_helper_rejects_nonfinite_focus_without_overflow(tmp_path, field, value):
    with pytest.raises(ValueError):
        trace_slice(tmp_path / "not-opened.jsonl", [], **{field: value})
