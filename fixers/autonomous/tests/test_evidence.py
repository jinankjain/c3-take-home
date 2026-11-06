from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fixer.evidence import MAX_TRACE_CHARS, bounded_result, summarize_trace, trace_slice


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
