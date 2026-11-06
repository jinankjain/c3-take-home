"""Trusted deterministic clock, journal, and trace writer; no service imports."""
from collections import Counter
from copy import deepcopy
import hashlib
import heapq
import json
from pathlib import Path
import random


class Clock:
    def __init__(self, scenario, seed):
        self.scenario = deepcopy(scenario)
        self.random = random.Random(seed)
        self.now = 0
        self.events = []
        self.serial = 0
        self.rows = []
        self.horizon = scenario.get("params", {}).get("horizon", 60)
        if not isinstance(self.horizon, (int, float)) or isinstance(self.horizon, bool) or not 1 <= self.horizon <= 1000:
            raise ValueError("params.horizon must be between 1 and 1000")
        self.emit("scenario", scenario=scenario["name"], seed=seed, world=scenario.get("world", {}),
                  actions=scenario["actions"], faults=scenario.get("faults", {}),
                  expect=scenario.get("expect", {}))

    def emit(self, kind, **data):
        self.rows.append({"seq": len(self.rows), "t": self.now, "kind": kind, **deepcopy(data)})

    def enqueue(self, delay, kind, **data):
        if not isinstance(delay, (int, float)) or isinstance(delay, bool) or delay < 0:
            raise ValueError("scheduled delay must be nonnegative")
        self.serial += 1
        heapq.heappush(self.events, (self.now + delay, self.serial, kind, deepcopy(data)))

    def wire_delay(self):
        return self.random.randint(1, 3)

    def run(self, handler):
        processed = 0
        while self.events:
            at, _, kind, data = heapq.heappop(self.events)
            if at > self.horizon:
                break
            self.now = at
            handler(kind, data)
            processed += 1
            if processed > 2000:
                raise RuntimeError("event budget exhausted; protocol is not making bounded progress")
        self.now = self.horizon

    def write(self, out, actions, error=None):
        self.emit("final", actions=actions, error=error)
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        text = "".join(json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n" for row in self.rows)
        (out / "trace.jsonl").write_text(text)
        resolved = all(row["outcome"] in {"ok", "cancelled", "rejected"} for row in actions)
        summary = {"scenario": self.scenario["name"], "seed": self.rows[0]["seed"],
                   "actions": actions, "all_resolved": resolved, "error": error,
                   "outcomes": dict(Counter(row["outcome"] for row in actions)),
                   "messages_sent": sum(row["kind"] == "msg_send" for row in self.rows),
                   "event_counts": dict(Counter(row["kind"] for row in self.rows)),
                   "final_t": self.now, "trace_sha256": hashlib.sha256(text.encode()).hexdigest()}
        (out / "summary.json").write_text(json.dumps(summary, sort_keys=True) + "\n")
        logs = out / "logs"
        logs.mkdir(exist_ok=True)
        (logs / "public-trace.jsonl").write_text(text)
        return summary


class Journal:
    def __init__(self, clock):
        self.clock = clock
        self.records = []

    def append(self, record):
        stored = deepcopy(record)
        self.records.append(stored)
        self.clock.emit("journal", node="journal", event="append", record=stored)

    def read(self):
        return deepcopy(self.records)
