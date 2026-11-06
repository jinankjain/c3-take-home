from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fixer.evidence import summarize_trace, trace_slice


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
