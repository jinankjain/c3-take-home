"""Deterministic transport and external effects for CitrineCourier."""
from collections import Counter
from copy import deepcopy
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from citrine import Courier
from .kernel import Clock, Journal


class Transport:
    def __init__(self, system):
        self.system = system

    def apply(self, job, operation_id, payload):
        system = self.system
        system.clock.emit("msg_send", src="citrine", dst="external-ledger", method="Apply",
                          job=job, operation_id=operation_id, payload=payload)
        system.clock.enqueue(system.clock.wire_delay(), "apply", generation=system.generation,
                             job=job, operation_id=operation_id, payload=payload)


class System:
    def __init__(self, scenario, seed):
        self.clock = Clock(scenario, seed)
        self.journal = Journal(self.clock)
        self.generation = 0
        self.effects = {}
        self.crashed_jobs = set()
        self.lost_replies = set()
        self.actions = {}
        faults = scenario.get("faults", {})
        self.crash_jobs = set(faults.get("crash_after_accept", []))
        self.drop_jobs = set(faults.get("lose_first_reply", []))
        if any(not isinstance(value, list) or any(not isinstance(job, str) for job in value)
               for value in (faults.get("crash_after_accept", []), faults.get("lose_first_reply", []))):
            raise ValueError("fault job selectors must be lists of job identifiers")
        self.namespace = scenario.get("world", {}).get("namespace", "citrine")
        self._boot()
        for number, action in enumerate(scenario["actions"]):
            if not isinstance(action, dict) or action.get("op") != "submit":
                raise ValueError("Citrine actions use op: submit")
            if not isinstance(action.get("job"), str) or not action["job"]:
                raise ValueError("submit requires job")
            payload = action.get("payload", {"amount": 1})
            if not isinstance(payload, dict) or not isinstance(payload.get("amount"), int) or isinstance(payload["amount"], bool):
                raise ValueError("submit payload requires integer amount")
            at = action.get("at", 0)
            if not isinstance(at, (int, float)) or isinstance(at, bool) or not 0 <= at <= self.clock.horizon:
                raise ValueError("action at must lie within the horizon")
            identity = str(action.get("id", f"c{number}"))
            if identity in self.actions:
                raise ValueError("action ids must be unique")
            self.actions[identity] = {"action_id": identity, "action": "submit", "job": action["job"],
                                      "outcome": "pending", "accepted": False, "payload": deepcopy(payload)}
            self.clock.enqueue(at, "submit", action_id=identity)

    def _boot(self):
        self.generation += 1
        self.clock.emit("log", node="citrine", event="boot", generation=self.generation)
        self.server = Courier(self.journal, Transport(self), self._schedule, self._log, self.namespace)

    def _schedule(self, delay, method, *args):
        self.clock.enqueue(delay, "timer", generation=self.generation, method=method, args=list(args))

    def _log(self, event, **data):
        self.clock.emit("log", node="citrine", event=event, **data)

    def _apply(self, data):
        job, operation_id, payload = data["job"], data["operation_id"], data["payload"]
        self.clock.emit("msg_deliver", src="citrine", dst="external-ledger", method="Apply", **data)
        previous = self.effects.get(operation_id)
        if previous is not None and (previous["job"], previous["payload"]) != (job, payload):
            result = {"status": "conflict", "receipt_id": ""}
            self.clock.emit("log", node="external-ledger", event="operation_conflict", job=job,
                            operation_id=operation_id)
        elif previous is not None:
            result = {"status": "cached", "receipt_id": previous["receipt_id"]}
            self.clock.emit("log", node="external-ledger", event="deduplicated", job=job,
                            operation_id=operation_id)
        else:
            result = {"status": "applied", "receipt_id": f"receipt/{operation_id}"}
            self.effects[operation_id] = {"job": job, "payload": deepcopy(payload),
                                          "receipt_id": result["receipt_id"]}
            self.clock.emit("effect", node="external-ledger", effect="apply", job=job,
                            operation_id=operation_id, payload=payload, receipt_id=result["receipt_id"])
        if job in self.drop_jobs and job not in self.lost_replies:
            self.lost_replies.add(job)
            self.clock.emit("fault", event="reply_lost", node="transport", job=job,
                            operation_id=operation_id, method="Apply")
            return
        self.clock.emit("msg_send", src="external-ledger", dst="citrine", method="ApplyReply",
                        job=job, operation_id=operation_id, result=result)
        self.clock.enqueue(self.clock.wire_delay(), "receipt", generation=data["generation"],
                           job=job, operation_id=operation_id, result=result)

    def handle(self, kind, data):
        if kind == "submit":
            action = self.actions[data["action_id"]]
            self.clock.emit("client", phase="begin", **action)
            result = self.server.accept(action["job"], action["payload"])
            action["accepted"] = result["accepted"]
            if not result["accepted"]:
                action["outcome"] = "rejected"
                self.clock.emit("client", phase="end", result=result, **action)
            else:
                self.clock.emit("client", phase="accepted", result=result, **action)
                job = action["job"]
                if job in self.crash_jobs and job not in self.crashed_jobs:
                    self.crashed_jobs.add(job)
                    self.clock.enqueue(.5, "crash", job=job)
                self.clock.enqueue(1, "poll", action_id=action["action_id"])
        elif kind == "poll":
            action = self.actions[data["action_id"]]
            result = self.server.status(action["job"])
            if result["outcome"] == "ok":
                action["outcome"] = "ok"
                action["receipt"] = result["receipt"]
                self.clock.emit("client", phase="end", result=result, **action)
            else:
                self.clock.enqueue(2, "poll", action_id=action["action_id"])
        elif kind == "crash":
            self.clock.emit("fault", node="citrine", event="crash", generation=self.generation, **data)
            self._boot()
        elif kind == "timer":
            if data["generation"] == self.generation:
                getattr(self.server, data["method"])(*data["args"])
            else:
                self.clock.emit("log", node="citrine", event="stale_timer_ignored", **data)
        elif kind == "apply":
            self._apply(data)
        elif kind == "receipt":
            if data["generation"] == self.generation:
                self.clock.emit("msg_deliver", src="external-ledger", dst="citrine", method="ApplyReply", **data)
                self.server.on_receipt(data["job"], data["operation_id"], data["result"])
            else:
                self.clock.emit("fault", node="transport", event="stale_reply", **data)


def run_scenario(scenario, seed, out):
    system = System(scenario, seed)
    error = None
    try:
        system.clock.run(system.handle)
    except Exception as failure:
        error = f"{type(failure).__name__}: {failure}"
    return system.clock.write(out, list(system.actions.values()), error)
