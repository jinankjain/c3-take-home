"""MoraineSlots: generation-fenced reservations and external worker retirement."""
from copy import deepcopy


class Supervisor:
    def __init__(self, journal, transport, log, capacity=2):
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 1:
            raise ValueError("capacity must be positive")
        self.journal = journal
        self.transport = transport
        self.log = log
        self.capacity = capacity
        self.leases = {}
        self.states = {}
        self.retiring = set()
        records = self.journal.read()
        self.epoch = max((row["epoch"] for row in records if row["type"] == "epoch"), default=0) + 1
        self.serial = sum(row["type"] == "lease_open" for row in records)
        for row in records:
            if row["type"] == "lease_open":
                self.leases[row["task"]] = deepcopy(row["lease"])
                self.states[row["task"]] = "running"
            elif row["type"] == "lease_closed":
                current = self.leases.get(row["task"])
                if current and current["lease_id"] == row["lease_id"]:
                    del self.leases[row["task"]]
                    self.states[row["task"]] = row["outcome"]
        self.used_units = sum(row["units"] for row in self.leases.values())
        self.journal.append({"type": "epoch", "epoch": self.epoch})
        self.log("recovery", epoch=self.epoch, used_units=self.used_units,
                 live_leases=sorted(row["lease_id"] for row in self.leases.values()))
        for task, lease in list(self.leases.items()):
            if lease["epoch"] < self.epoch:
                self._retire(task, lease)

    def acquire(self, task, units):
        if not isinstance(task, str) or not task.strip():
            raise ValueError("task must be a nonempty identifier")
        if not isinstance(units, int) or isinstance(units, bool) or units < 1:
            raise ValueError("units must be positive")
        existing = self.leases.get(task)
        if existing:
            if existing["units"] != units:
                return {"accepted": False, "reason": "lease_conflict"}
            return {"accepted": True, "lease": deepcopy(existing)}
        if units > self.capacity - self.used_units:
            self.log("capacity", task=task, requested_units=units,
                     used_units=self.used_units, capacity=self.capacity)
            return {"accepted": False, "reason": "capacity"}
        self.serial += 1
        lease = {"task": task, "lease_id": f"moraine/{self.epoch}/{self.serial}/{task}",
                 "epoch": self.epoch, "units": units}
        self.journal.append({"type": "lease_open", "task": task, "lease": lease})
        self.leases[task] = lease
        self.used_units += units
        self.states[task] = "running"
        self.log("lease_granted", task=task, **{key: lease[key] for key in ("lease_id", "epoch", "units")})
        self.transport.start(task, deepcopy(lease))
        return {"accepted": True, "lease": deepcopy(lease)}

    def _retire(self, task, lease):
        self.retiring.add(lease["lease_id"])
        self.log("retire_requested", task=task, lease_id=lease["lease_id"],
                 lease_epoch=lease["epoch"], fence_epoch=self.epoch)
        self.transport.stop(task, lease["lease_id"], self.epoch)

    def on_stopped(self, task, lease_id):
        lease = self.leases.get(task)
        if not lease or lease["lease_id"] != lease_id or lease_id not in self.retiring:
            self.log("stop_ack_ignored", task=task, lease_id=lease_id)
            return
        self.journal.append({"type": "lease_closed", "task": task, "lease_id": lease_id,
                             "outcome": "cancelled"})
        del self.leases[task]
        self.used_units -= lease["units"]
        self.retiring.discard(lease_id)
        self.states[task] = "cancelled"
        self.log("retire_complete", task=task, lease_id=lease_id, used_units=self.used_units)

    def on_complete(self, task, lease_id):
        lease = self.leases.get(task)
        if not lease or lease["lease_id"] != lease_id:
            self.log("completion_ignored", task=task, lease_id=lease_id)
            return
        self.journal.append({"type": "lease_closed", "task": task, "lease_id": lease_id,
                             "outcome": "ok"})
        del self.leases[task]
        self.used_units -= lease["units"]
        self.retiring.discard(lease_id)
        self.states[task] = "ok"
        self.log("task_complete", task=task, lease_id=lease_id, used_units=self.used_units)

    def status(self, task):
        return {"outcome": self.states.get(task, "pending"),
                "used_units": self.used_units, "epoch": self.epoch}
