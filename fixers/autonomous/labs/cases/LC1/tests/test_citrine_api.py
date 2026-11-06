from copy import deepcopy
import heapq

import pytest

from citrine import Courier


class Journal:
    def __init__(self):
        self.records = []

    def append(self, row):
        self.records.append(deepcopy(row))

    def read(self):
        return deepcopy(self.records)


class Harness:
    def __init__(self, namespace="citrine"):
        self.journal = Journal()
        self.timers = []
        self.now = 0
        self.serial = 0
        self.effects = {}
        self.requests = []
        self.namespace = namespace
        self.server = Courier(self.journal, self, self.schedule, lambda *_args, **_kwargs: None, namespace)

    def schedule(self, delay, method, *args):
        self.serial += 1
        heapq.heappush(self.timers, (self.now + delay, self.serial, method, args))

    def apply(self, job, operation_id, payload):
        self.requests.append((job, operation_id, deepcopy(payload)))
        existed = operation_id in self.effects
        self.effects.setdefault(operation_id, (job, deepcopy(payload)))
        self.server.on_receipt(job, operation_id, {"status": "cached" if existed else "applied",
                                                  "receipt_id": "receipt/" + operation_id})

    def run(self):
        for _ in range(100):
            if not self.timers:
                return
            self.now, _, method, args = heapq.heappop(self.timers)
            getattr(self.server, method)(*args)
        raise AssertionError("healthy delivery did not quiesce")

    def reboot(self):
        self.server = Courier(self.journal, self, self.schedule, lambda *_args, **_kwargs: None, self.namespace)


def test_normal_delivery_and_receipt():
    harness = Harness()
    assert harness.server.accept("parcel", {"amount": 7})["accepted"]
    harness.run()
    assert harness.server.status("parcel")["outcome"] == "ok"
    assert list(harness.effects.values()) == [("parcel", {"amount": 7})]


def test_repeated_accept_keeps_one_effect():
    harness = Harness()
    for _ in range(3):
        assert harness.server.accept("parcel", {"amount": 4})["accepted"]
    harness.run()
    receipt = harness.server.status("parcel")["receipt"]
    assert harness.server.accept("parcel", {"amount": 4})["receipt"] == receipt
    harness.run()
    assert len(harness.requests) == len(harness.effects) == 1


def test_payload_conflict_preserves_original_intent():
    harness = Harness()
    harness.server.accept("parcel", {"amount": 3})
    assert not harness.server.accept("parcel", {"amount": 9})["accepted"]
    harness.run()
    assert list(harness.effects.values()) == [("parcel", {"amount": 3})]


@pytest.mark.parametrize("job,payload", [("", {"amount": 1}), ("x", {}), ("x", {"amount": True}), ("x", {"amount": 1.2})])
def test_invalid_admission(job, payload):
    with pytest.raises(ValueError):
        Harness().server.accept(job, payload)


def test_completed_receipt_survives_restart():
    harness = Harness()
    harness.server.accept("parcel", {"amount": 5})
    harness.run()
    receipt = harness.server.status("parcel")["receipt"]
    harness.reboot()
    assert harness.server.status("parcel")["receipt"] == receipt
    harness.run()
    assert len(harness.effects) == 1
