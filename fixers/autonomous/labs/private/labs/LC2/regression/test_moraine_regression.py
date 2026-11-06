from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from test_moraine_api import Harness


def test_out_of_order_weighted_completions_do_not_free_other_work():
    harness = Harness(4)
    harness.server.acquire("small", 1)
    harness.server.acquire("large", 3)
    harness.complete("large")
    assert harness.server.used_units == 1
    assert harness.server.acquire("replacement", 3)["accepted"]
    harness.complete("small")
    harness.complete("replacement")
    assert harness.server.used_units == 0


def test_returned_lease_is_a_snapshot():
    harness = Harness()
    returned = harness.server.acquire("task", 1)["lease"]
    returned["units"] = 200
    harness.complete("task")
    assert harness.server.used_units == 0


def test_duplicate_completion_does_not_make_capacity_negative():
    harness = Harness()
    lease = harness.server.acquire("task", 2)["lease"]["lease_id"]
    harness.server.on_complete("task", lease)
    harness.server.on_complete("task", lease)
    assert harness.server.used_units == 0
    assert harness.server.acquire("replacement", 2)["accepted"]


def test_stale_stop_ack_does_not_retire_reused_task_identifier():
    harness = Harness()
    old = harness.server.acquire("task", 1)["lease"]["lease_id"]
    harness.complete("task")
    new = harness.server.acquire("task", 1)["lease"]["lease_id"]
    harness.server.on_stopped("task", old)
    assert harness.server.used_units == 1 and harness.server.leases["task"]["lease_id"] == new
