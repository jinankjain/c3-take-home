from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from test_citrine_api import Harness


def test_caller_mutation_does_not_change_committed_payload():
    harness = Harness()
    payload = {"amount": 11}
    harness.server.accept("immutable", payload)
    payload["amount"] = 99
    harness.run()
    assert list(harness.effects.values()) == [("immutable", {"amount": 11})]


def test_distinct_jobs_have_distinct_effect_domains():
    harness = Harness()
    for job, amount in (("left", 2), ("right", 13)):
        harness.server.accept(job, {"amount": amount})
    harness.run()
    assert sorted(harness.effects.values()) == [("left", {"amount": 2}), ("right", {"amount": 13})]


def test_unknown_and_late_receipts_do_not_create_journal_results():
    harness = Harness()
    harness.server.on_receipt("unknown", "foreign", {"status": "applied", "receipt_id": "foreign"})
    assert not harness.journal.records
    harness.server.accept("known", {"amount": 6})
    harness.run()
    before = harness.journal.read()
    harness.server.on_receipt("known", "late", {"status": "applied", "receipt_id": "late"})
    assert harness.journal.read() == before


def test_namespaces_keep_external_operation_domains_separate():
    first, second = Harness("tenant-one"), Harness("tenant-two")
    for harness in (first, second):
        harness.server.accept("same-local-id", {"amount": 2})
        harness.run()
    assert set(first.effects).isdisjoint(second.effects)
