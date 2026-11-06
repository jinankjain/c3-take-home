"""Deterministic external executors and restart transport for MoraineSlots."""
from copy import deepcopy
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from moraine import Supervisor
from .kernel import Clock, Journal


class Transport:
    def __init__(self, system):
        self.system = system

    def start(self, task, lease):
        system = self.system
        system.clock.emit("msg_send", src="moraine", dst="executor", method="Start",
                          task=task, lease=lease)
        system.clock.enqueue(system.clock.wire_delay(), "start", generation=system.generation,
                             task=task, lease=lease)

    def stop(self, task, lease_id, fence_epoch):
        system = self.system
        system.clock.emit("msg_send", src="moraine", dst="executor", method="Stop",
                          task=task, lease_id=lease_id, fence_epoch=fence_epoch)
        system.clock.enqueue(system.clock.wire_delay(), "stop", generation=system.generation,
                             task=task, lease_id=lease_id, fence_epoch=fence_epoch)


class System:
    def __init__(self, scenario, seed):
        self.clock = Clock(scenario, seed)
        self.journal = Journal(self.clock)
        self.generation = 0
        self.capacity = scenario.get("world", {}).get("capacity", 2)
        if not isinstance(self.capacity, int) or isinstance(self.capacity, bool) or not 1 <= self.capacity <= 8:
            raise ValueError("world.capacity must be between 1 and 8")
        self.running = {}
        self.history = {}
        self.fences = {}
        self.actions = {}
        self.lease_intents = {}
        faults = scenario.get("faults", {})
        self.duplicate_stop_ack = faults.get("duplicate_stop_ack", False)
        if not isinstance(self.duplicate_stop_ack, bool):
            raise ValueError("duplicate_stop_ack must be boolean")
        restart_at = faults.get("restart_at")
        if restart_at is not None and (not isinstance(restart_at, (int, float)) or isinstance(restart_at, bool)
                                       or not 0 <= restart_at <= self.clock.horizon):
            raise ValueError("restart_at must lie within the horizon or be null")
        self._boot()
        for number, action in enumerate(scenario["actions"]):
            if not isinstance(action, dict) or action.get("op") != "acquire" or not isinstance(action.get("task"), str):
                raise ValueError("Moraine actions require op: acquire and task")
            units, duration = action.get("units", 1), action.get("duration", 4)
            if not isinstance(units, int) or isinstance(units, bool) or units < 1:
                raise ValueError("action units must be positive")
            if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration < 1:
                raise ValueError("duration must be positive")
            at = action.get("at", 0)
            if not isinstance(at, (int, float)) or isinstance(at, bool) or not 0 <= at <= self.clock.horizon:
                raise ValueError("action at must lie within the horizon")
            identity = str(action.get("id", f"m{number}"))
            if identity in self.actions:
                raise ValueError("action ids must be unique")
            self.actions[identity] = {"action_id": identity, "action": "acquire", "task": action["task"],
                                      "units": units, "duration": duration, "accepted": False, "outcome": "pending"}
            self.clock.enqueue(at, "acquire", action_id=identity)
        if restart_at is not None:
            self.clock.enqueue(restart_at, "restart")

    def _boot(self):
        self.generation += 1
        self.clock.emit("log", node="moraine", event="boot", generation=self.generation)
        self.server = Supervisor(self.journal, Transport(self), self._log, self.capacity)

    def _log(self, event, **data):
        self.clock.emit("log", node="moraine", event=event, **data)

    def _start(self, data):
        lease, task = data["lease"], data["task"]
        lease_id = lease["lease_id"]
        self.clock.emit("msg_deliver", src="moraine", dst="executor", method="Start", **data)
        if self.fences.get(lease_id, 0) > lease["epoch"]:
            self.clock.emit("executor", event="start_fenced", task=task, lease_id=lease_id,
                            lease_epoch=lease["epoch"], fence_epoch=self.fences[lease_id])
            return
        if lease_id in self.history:
            self.clock.emit("executor", event="start_duplicate", task=task, lease_id=lease_id)
            return
        intent = self.lease_intents.get(lease_id)
        if intent is None or (task, lease["units"]) != (intent["task"], intent["units"]):
            self.clock.emit("executor", event="request_mismatch", task=task, lease_id=lease_id)
            return
        if lease["units"] > self.capacity - sum(row["units"] for row in self.running.values()):
            self.clock.emit("executor", event="capacity_rejected", task=task, lease_id=lease_id)
            return
        if any(row["task"] == task for row in self.running.values()):
            self.clock.emit("executor", event="overlap_rejected", task=task, lease_id=lease_id)
            return
        self.running[lease_id] = deepcopy(lease)
        self.history[lease_id] = dict(deepcopy(lease), outcome="running")
        self.clock.emit("executor", event="start", task=task, lease_id=lease_id,
                        epoch=lease["epoch"], units=lease["units"])
        self.clock.enqueue(intent["duration"], "complete", generation=data["generation"],
                           task=task, lease_id=lease_id)

    def _stop(self, data):
        task, lease_id, fence = data["task"], data["lease_id"], data["fence_epoch"]
        self.clock.emit("msg_deliver", src="moraine", dst="executor", method="Stop", **data)
        previous = self.history.get(lease_id)
        if previous and fence <= previous["epoch"]:
            self.clock.emit("executor", event="fence_rejected", task=task, lease_id=lease_id,
                            fence_epoch=fence, lease_epoch=previous["epoch"])
            return
        # A stop tombstone also fences a Start that has not arrived yet.
        self.fences[lease_id] = max(fence, self.fences.get(lease_id, 0))
        self.running.pop(lease_id, None)
        if previous:
            previous["outcome"] = "cancelled"
        self.clock.emit("executor", event="stop", task=task, lease_id=lease_id, fence_epoch=fence)
        self.clock.emit("msg_send", src="executor", dst="moraine", method="StopAck",
                        task=task, lease_id=lease_id)
        self.clock.enqueue(self.clock.wire_delay(), "stop_ack", generation=data["generation"],
                           task=task, lease_id=lease_id)
        if self.duplicate_stop_ack:
            self.clock.emit("msg_send", src="executor", dst="moraine", method="StopAck",
                            task=task, lease_id=lease_id, duplicate=True)
            self.clock.enqueue(self.clock.wire_delay() + 1, "stop_ack", generation=data["generation"],
                               task=task, lease_id=lease_id)

    def handle(self, kind, data):
        if kind == "acquire":
            action = self.actions[data["action_id"]]
            if not action.get("began"):
                self.clock.emit("client", phase="begin", **action)
                action["began"] = True
            response = self.server.acquire(action["task"], action["units"])
            if response["accepted"]:
                action["accepted"] = True
                action["lease_id"] = response["lease"]["lease_id"]
                self.lease_intents.setdefault(action["lease_id"], {
                    "task": action["task"], "units": action["units"], "duration": action["duration"]})
                self.clock.emit("client", phase="accepted", result=response, **action)
                self.clock.enqueue(1, "poll", action_id=action["action_id"])
            elif response["reason"] == "capacity":
                self.clock.emit("client", phase="admission_denied", reason=response["reason"],
                                requested_units=action["units"], **action)
                self.clock.enqueue(4, "acquire", action_id=action["action_id"])
            else:
                action["outcome"] = "rejected"
                self.clock.emit("client", phase="end", result=response, **action)
        elif kind == "poll":
            action = self.actions[data["action_id"]]
            result = self.server.status(action["task"])
            if result["outcome"] in {"ok", "cancelled"}:
                action["outcome"] = result["outcome"]
                self.clock.emit("client", phase="end", result=result, **action)
            else:
                self.clock.enqueue(2, "poll", action_id=action["action_id"])
        elif kind == "restart":
            self.clock.emit("fault", node="moraine", event="crash", generation=self.generation)
            self._boot()
        elif kind == "start":
            self._start(data)
        elif kind == "stop":
            self._stop(data)
        elif kind == "complete":
            lease = self.running.pop(data["lease_id"], None)
            if lease is not None:
                self.history[data["lease_id"]]["outcome"] = "ok"
                self.clock.emit("executor", event="complete", task=data["task"], lease_id=data["lease_id"])
                self.clock.emit("msg_send", src="executor", dst="moraine", method="Complete",
                                task=data["task"], lease_id=data["lease_id"])
                self.clock.enqueue(self.clock.wire_delay(), "complete_ack", **data)
        elif kind in {"stop_ack", "complete_ack"}:
            if data["generation"] == self.generation:
                self.clock.emit("msg_deliver", src="executor", dst="moraine",
                                method="StopAck" if kind == "stop_ack" else "Complete", **data)
                callback = self.server.on_stopped if kind == "stop_ack" else self.server.on_complete
                callback(data["task"], data["lease_id"])
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
