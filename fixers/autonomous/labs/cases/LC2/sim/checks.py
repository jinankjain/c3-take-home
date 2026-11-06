"""Trace-only Moraine contracts: retirement, fencing, and usable capacity."""
import json


def check_trace(path, reference_messages=None):
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if not rows or rows[0].get("kind") != "scenario" or rows[-1].get("kind") != "final":
        raise ValueError("trace requires scenario and final records")
    violations, seen = [], set()

    def fail(invariant, row, detail, identity=None):
        key = (invariant, identity)
        if key not in seen:
            seen.add(key)
            violations.append({"invariant": invariant, "t": row["t"], "seq": row["seq"],
                               "detail": detail, **({"task": identity} if identity else {})})

    capacity = rows[0].get("world", {}).get("capacity", 2)
    leases, required, stopped, completed = {}, set(), set(), set()
    for row in rows:
        if row["kind"] == "journal" and row.get("event") == "append":
            record = row["record"]
            if record["type"] == "lease_open":
                leases[record["lease"]["lease_id"]] = record["lease"]
            elif record["type"] == "lease_closed":
                leases.pop(record["lease_id"], None)
        elif row["kind"] == "fault" and row.get("event") == "crash":
            required.update(leases)
        elif row["kind"] == "executor":
            event, lease_id = row.get("event"), row.get("lease_id")
            if event == "stop":
                stopped.add(lease_id)
            elif event == "complete":
                completed.add(lease_id)
            elif event in {"capacity_rejected", "overlap_rejected", "request_mismatch", "fence_rejected"}:
                fail("WORKFLOW_RESULT", row, f"External executor rejected {event}.", row.get("task"))
            elif event == "start" and lease_id not in leases:
                fail("WORKFLOW_RESULT", row, "Executor started without a durable reservation.", row.get("task"))
        elif row["kind"] == "client" and row.get("phase") == "admission_denied" and row.get("reason") == "capacity":
            live_units = sum(lease["units"] for lease in leases.values())
            if row["requested_units"] <= capacity - live_units:
                fail("CAPACITY_REUSE", row,
                     f"{row['task']} was denied although durable reservations occupy only {live_units}/{capacity} units.",
                     row["task"])
    final = rows[-1]
    for lease_id in sorted(required):
        if lease_id in leases or lease_id not in stopped | completed:
            fail("RECOVERY_RETIRE", final,
                 f"Pre-restart lease {lease_id} has not been durably retired and stopped.", lease_id)
    for action in final["actions"]:
        lease_id = action.get("lease_id")
        if action["outcome"] == "ok" and lease_id not in completed:
            fail("WORKFLOW_RESULT", final, "Client completion lacks an external completion.", action["task"])
        elif action["outcome"] == "cancelled" and lease_id not in stopped:
            fail("WORKFLOW_RESULT", final, "Client cancellation lacks an external stop.", action["task"])
        elif action["outcome"] == "rejected":
            fail("WORKFLOW_RESULT", final, "Valid lease request was rejected.", action["task"])
    unresolved = any(action["outcome"] == "pending" for action in final["actions"])
    if unresolved and not violations:
        fail("WORKFLOW_PROGRESS", final, "A valid workflow remained unresolved without a capacity or retirement explanation.")
    metrics = {"completed": sum(action["outcome"] == "ok" for action in final["actions"]),
               "cancelled": sum(action["outcome"] == "cancelled" for action in final["actions"]),
               "all_resolved": not unresolved}
    for key, value in rows[0].get("expect", {}).items():
        if metrics.get(key) != value:
            fail("expect", final, f"{key}: observed {metrics.get(key)!r}, expected {value!r}", key)
    messages = sum(row["kind"] == "msg_send" for row in rows)
    if reference_messages is not None and messages > 3 * reference_messages:
        fail("I3", final, f"{messages} messages exceeds three times the reference {reference_messages}.")
    return {"violations": len(violations), "invariants": violations}
