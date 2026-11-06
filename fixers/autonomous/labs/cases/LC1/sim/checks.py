"""Trace-only Citrine contracts: crash survival and one external effect."""
from collections import Counter
import json


def check_trace(path, reference_messages=None):
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if not rows or rows[0].get("kind") != "scenario" or rows[-1].get("kind") != "final":
        raise ValueError("trace requires scenario and final records")
    violations = []
    seen = set()

    def fail(invariant, row, detail, job=None):
        key = (invariant, job)
        if key not in seen:
            seen.add(key)
            violations.append({"invariant": invariant, "t": row["t"], "seq": row["seq"],
                               "detail": detail, **({"job": job} if job else {})})

    durable, acked = set(), set()
    effects = []
    expected = {}
    for action in rows[0]["actions"]:
        expected.setdefault(action["job"], action.get("payload", {"amount": 1}))
    for row in rows:
        if row["kind"] == "journal" and row.get("event") == "append":
            record = row["record"]
            if record["type"] == "intake":
                durable.add(record["job"])
        elif row["kind"] == "client" and row.get("phase") == "accepted":
            acked.add(row["job"])
        elif row["kind"] == "fault" and row.get("event") == "crash":
            for job in sorted(acked - durable):
                fail("DURABLE_HANDOFF", row, f"Accepted {job} has no durable intake at crash.", job)
        elif row["kind"] == "effect" and row.get("effect") == "apply":
            effects.append(row)
            if row["payload"] != expected.get(row["job"]):
                fail("EXACTLY_ONE_EFFECT", row, "External payload differs from the submitted intent.", row["job"])
    final = rows[-1]
    done = {action["job"] for action in final["actions"] if action["outcome"] == "ok"}
    for action in final["actions"]:
        if action["outcome"] not in {"ok", "rejected"}:
            fail("DURABLE_HANDOFF", final, f"Accepted {action['job']} remains unresolved at the horizon.", action["job"])
        elif action["outcome"] == "rejected":
            fail("DURABLE_HANDOFF", final, f"Valid submit for {action['job']} was rejected.", action["job"])
    counts = Counter(row["job"] for row in effects)
    for job in sorted(done | {job for job, count in counts.items() if count > 1}):
        if counts[job] != 1:
            fail("EXACTLY_ONE_EFFECT", final, f"{job} has {counts[job]} committed external effects; expected one.", job)
    for action in final["actions"]:
        if action["outcome"] == "ok":
            receipt = action.get("receipt", {})
            if not any(row["job"] == action["job"] and row["operation_id"] == receipt.get("operation_id")
                       and row["receipt_id"] == receipt.get("receipt_id") for row in effects):
                fail("EXACTLY_ONE_EFFECT", final, "Completion receipt has no matching external commit.", action["job"])
    metrics = {"completed": sum(action["outcome"] == "ok" for action in final["actions"]),
               "effects": len(effects), "all_resolved": all(action["outcome"] == "ok" for action in final["actions"])}
    for key, value in rows[0].get("expect", {}).items():
        if metrics.get(key) != value:
            fail("expect", final, f"{key}: observed {metrics.get(key)!r}, expected {value!r}", key)
    messages = sum(row["kind"] == "msg_send" for row in rows)
    if reference_messages is not None and messages > 3 * reference_messages:
        fail("I3", final, f"{messages} messages exceeds three times the reference {reference_messages}.")
    return {"violations": len(violations), "invariants": violations}
