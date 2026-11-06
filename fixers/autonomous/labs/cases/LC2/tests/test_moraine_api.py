from copy import deepcopy

import pytest

from moraine import Supervisor


class Journal:
    def __init__(self):
        self.records = []

    def append(self, row):
        self.records.append(deepcopy(row))

    def read(self):
        return deepcopy(self.records)


class Harness:
    def __init__(self, capacity=2):
        self.journal = Journal()
        self.starts = []
        self.stops = []
        self.capacity = capacity
        self.server = Supervisor(self.journal, self, lambda *_args, **_kwargs: None, capacity)

    def start(self, task, lease):
        self.starts.append((task, deepcopy(lease)))

    def stop(self, task, lease_id, fence_epoch):
        self.stops.append((task, lease_id, fence_epoch))

    def complete(self, task):
        self.server.on_complete(task, self.server.leases[task]["lease_id"])

    def reboot(self):
        self.server = Supervisor(self.journal, self, lambda *_args, **_kwargs: None, self.capacity)


def test_weighted_reservations_reuse_normal_completion_capacity():
    harness = Harness()
    assert harness.server.acquire("one", 1)["accepted"]
    assert harness.server.acquire("two", 1)["accepted"]
    assert not harness.server.acquire("three", 1)["accepted"]
    harness.complete("one")
    assert harness.server.acquire("three", 1)["accepted"]
    harness.complete("two")
    harness.complete("three")
    assert harness.server.used_units == 0


def test_duplicate_acquire_is_idempotent_and_conflicting_units_reject():
    harness = Harness()
    first = harness.server.acquire("task", 1)["lease"]
    assert harness.server.acquire("task", 1)["lease"] == first
    assert not harness.server.acquire("task", 2)["accepted"]
    assert len(harness.starts) == 1 and harness.server.used_units == 1


@pytest.mark.parametrize("units", [0, -1, True, 1.5])
def test_invalid_units_reject_without_reservation(units):
    harness = Harness()
    with pytest.raises(ValueError):
        harness.server.acquire("task", units)
    assert harness.server.used_units == 0 and not harness.starts


def test_old_completion_cannot_release_a_new_lease_with_same_task_name():
    harness = Harness()
    old = harness.server.acquire("task", 1)["lease"]["lease_id"]
    harness.complete("task")
    new = harness.server.acquire("task", 1)["lease"]["lease_id"]
    assert new != old
    harness.server.on_complete("task", old)
    assert harness.server.used_units == 1 and harness.server.leases["task"]["lease_id"] == new


def test_completed_state_and_epoch_survive_idle_restart():
    harness = Harness()
    harness.server.acquire("task", 1)
    harness.complete("task")
    old_epoch = harness.server.epoch
    harness.reboot()
    assert harness.server.status("task")["outcome"] == "ok"
    assert harness.server.used_units == 0 and harness.server.epoch > old_epoch
