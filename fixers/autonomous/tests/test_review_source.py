import difflib
import hashlib
import json
import os
import threading
from pathlib import Path

import pytest

from fixer import review_source
from fixer.review_source import review_source_context


def write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def diff(name, before, after):
    return "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                        fromfile="a/" + name, tofile="b/" + name))


def find(result, suffix, symbol):
    return next(block for block in result["blocks"] if block["path"].endswith(suffix) and block["symbol"] == symbol)


def test_changed_producer_includes_unchanged_receiver_and_complete_helpers(tmp_path):
    before = '''from .base import handler
class Agent:
    @handler("grant")
    async def grant(self, request):
        self.persist(request)
        return {"cut": self.acked}

    def persist(self, request):
        self.intent = request
        self.store.save(request)
'''
    after = before.replace('self.acked', 'self.sent')
    producer = write(tmp_path, "src/demo/agent.py", after)
    receiver_text = '''from .agent import Agent
class Broker:
    async def offer(self):
        reply = await self.rpc("grant", {})
        self.cut = min(self.seen, reply["cut"])
        return self.retire()

    def retire(self):
        self.owner = None
        return self.cut
'''
    receiver = write(tmp_path, "src/demo/broker.py", receiver_text)
    result = review_source_context(tmp_path, diff("src/demo/agent.py", before, after), [])
    changed = find(result, "agent.py", "Agent.grant")
    peer = find(result, "broker.py", "Broker.offer")
    assert changed["complete"] and changed["role"] == "changed"
    assert '@handler("grant")' in changed["text"] and 'return {"cut": self.sent}' in changed["text"]
    assert peer["complete"] and 'min(self.seen, reply["cut"])' in peer["text"]
    assert find(result, "agent.py", "Agent.persist")["complete"]
    assert find(result, "broker.py", "Broker.retire")["complete"]
    assert changed["sha256"] == hashlib.sha256(producer.read_bytes()).hexdigest()
    assert peer["sha256"] == hashlib.sha256(receiver.read_bytes()).hexdigest()
    assert len(json.dumps(result, ensure_ascii=False)) <= 24_000


def test_multiple_diff_file_headers_are_not_changed_source_lines(tmp_path):
    before = "def first():\n    return 0\n\ndef innocent():\n    return 42\n\ndef trailing():\n    return 43\n"
    after = before.replace("return 0", "return 1")
    other_before = "def other():\n    return 0\n"
    other_after = other_before.replace("0", "1")
    write(tmp_path, "src/first.py", after)
    write(tmp_path, "src/other.py", other_after)
    packet = review_source_context(tmp_path, diff("src/first.py", before, after)
                                   + diff("src/other.py", other_before, other_after), [])
    changed = [(row["path"], row["symbol"]) for row in packet["blocks"] if row["role"].startswith("changed")]
    assert changed == [("src/first.py", "first"), ("src/other.py", "other")]


def test_header_shaped_string_changes_inside_a_hunk_stay_source_content(tmp_path):
    before = 'def message():\n    return """\n-- token\n"""\n'
    after = before.replace("-- token", "++ token")
    write(tmp_path, "src/message.py", after)
    packet = review_source_context(tmp_path, diff("src/message.py", before, after), [])
    assert '++ token' in find(packet, "message.py", "message")["text"]


def test_protocol_receiver_precedes_broad_helpers_when_packet_space_is_limited(tmp_path):
    helpers = "\n".join("def helper_%d(value):\n%s    return value\n" %
                          (i, "    # bounded but unrelated helper detail\n" * 5) for i in range(14))
    before = ('from .base import handler\n@handler("reserve")\ndef send(self):\n'
              + "".join(f"    self.helper_{i}(self.state)\n" for i in range(14))
              + '    return {"cut": self.acked}\n\n' + helpers)
    after = before.replace("self.acked", "self.sent")
    write(tmp_path, "src/agent.py", after)
    write(tmp_path, "src/broker.py", '''class Broker:
    async def receive(self):
        reply = await self.rpc("reserve", {})
        self.cut = min(self.seen, reply["cut"])
        return self.cut
''')
    packet = review_source_context(tmp_path, diff("src/agent.py", before, after), [], max_chars=4600)
    assert find(packet, "agent.py", "send")["complete"]
    receiver = find(packet, "broker.py", "Broker.receive")
    assert receiver["role"] == "protocol_peer" and receiver["complete"]
    assert 'min(self.seen, reply["cut"])' in receiver["text"]
    selected_helpers = [row for row in packet["blocks"] if row["role"] == "helper"]
    assert selected_helpers and packet["blocks"].index(receiver) < packet["blocks"].index(selected_helpers[0])
    assert packet["truncated"] and len(json.dumps(packet, ensure_ascii=False)) <= 4600


def test_reply_producer_and_runtime_writer_precede_competing_helpers(tmp_path):
    sender = '''from .rpc import exposed
class Monitor:
    def __init__(self):
        self.reported = 0

    @exposed("reserve")
    def offer(self):
        return {"cut": self.reported}

    async def pulse(self):
        sent = self.reported
        while True:
            sent += 1
            await self.send(sent)
            self.reported = sent
'''
    helpers = "\n".join(f"    def helper_{i}(self):\n" + "        # unrelated helper context\n" * 8
                         + "        return 1\n" for i in range(16))
    before = ('class Broker:\n    def accept(self):\n        reply = self.rpc("reserve", {})\n'
              + "".join(f"        self.helper_{i}()\n" for i in range(16))
              + '        self.cut = min(self.seen, reply["cut"])\n\n' + helpers)
    after = before.replace('min(self.seen, reply["cut"])', 'reply["cut"]')
    write(tmp_path, "src/monitor.py", sender)
    write(tmp_path, "src/broker.py", after)
    packet = review_source_context(tmp_path, diff("src/broker.py", before, after), [], max_chars=6500)
    producer = find(packet, "monitor.py", "Monitor.offer")
    writer = find(packet, "monitor.py", "Monitor.pulse")
    initial = find(packet, "monitor.py", "Monitor.__init__")
    assert producer["complete"] and producer["role"] == "protocol_peer"
    assert writer["complete"] and writer["role"] == "protocol_state_writer"
    assert "await self.send(sent)\n            self.reported = sent" in writer["text"]
    assert initial["complete"] and initial["role"] == "protocol_state_initializer"
    assert packet["blocks"].index(producer) < packet["blocks"].index(writer) < packet["blocks"].index(initial)
    assert find(packet, "broker.py", "Broker.accept")["complete"]
    assert packet["truncated"] and len(json.dumps(packet, ensure_ascii=False)) <= 6500


def test_state_writers_are_isolated_to_the_producers_exact_class(tmp_path):
    before = '''class Monitor:
    def offer(self):
        return {"cut": 0}

    def advance(self, receipt):
        self.value = receipt

class Other:
    def advance(self, receipt):
        self.value = receipt
'''
    after = before.replace('return {"cut": 0}', 'return {"cut": self.value}')
    write(tmp_path, "src/state.py", after)
    packet = review_source_context(tmp_path, diff("src/state.py", before, after), [])
    assert find(packet, "state.py", "Monitor.advance")["role"] == "protocol_state_writer"
    assert not any(row["symbol"] == "Other.advance" for row in packet["blocks"])


def test_nested_static_and_rebound_instances_cannot_impersonate_class_writers(tmp_path):
    before = '''import builtins
class Monitor:
    def offer(self):
        return {"cut": 0}

    def advance(self, receipt):
        self.value = receipt

    def nested(self):
        def mutate(other):
            other.value = 3
        return mutate

    @builtins.staticmethod
    def static_mutate(other):
        other.value = 3

    def rebound(self, other):
        self = other
        self.value = 3
'''
    after = before.replace('return {"cut": 0}', 'return {"cut": self.value}')
    write(tmp_path, "src/state.py", after)
    packet = review_source_context(tmp_path, diff("src/state.py", before, after), [])
    writers = [row["symbol"] for row in packet["blocks"] if row["role"] == "protocol_state_writer"]
    assert writers == ["Monitor.advance"]
    assert any(row["reason"] == "state_instance_rebound" for row in packet["omissions"])


def test_returned_alias_and_indexed_state_use_the_declared_instance_parameter(tmp_path):
    before = '''class Offsets:
    def __init__(node):
        node.cursors = {}

    def reply(node, stream):
        result = {"cut": 0}
        return result

    def advance(node, stream, received):
        node.cursors[stream] = received
'''
    after = before.replace('{"cut": 0}', '{"cut": node.cursors[stream]}')
    write(tmp_path, "src/offsets.py", after)
    packet = review_source_context(tmp_path, diff("src/offsets.py", before, after), [])
    assert find(packet, "offsets.py", "Offsets.reply")["complete"]
    writer = find(packet, "offsets.py", "Offsets.advance")
    assert writer["complete"] and writer["role"] == "protocol_state_writer"
    assert 'node.cursors[stream] = received' in writer["text"]
    assert find(packet, "offsets.py", "Offsets.__init__")["role"] == "protocol_state_initializer"


def test_state_edge_limit_retains_runtime_writer_before_initializer(tmp_path, monkeypatch):
    monkeypatch.setattr(review_source, "MAX_LINKS", 1)
    before = '''class Monitor:
    def __init__(self):
        self.value = 0

    def offer(self):
        return {"cut": 0}

    def advance(self, receipt):
        self.value = receipt
'''
    after = before.replace('return {"cut": 0}', 'return {"cut": self.value}')
    write(tmp_path, "src/state.py", after)
    packet = review_source_context(tmp_path, diff("src/state.py", before, after), [])
    assert find(packet, "state.py", "Monitor.advance")["complete"]
    assert packet["stats"]["state_edges"] == 1 and packet["stats"]["state_edges_omitted"] == 1
    assert not any(row["symbol"] == "Monitor.__init__" for row in packet["blocks"])
    assert any(row["reason"] == "state_edge_limit" for row in packet["omissions"])


def test_cancellation_during_returned_state_analysis_stops_context_selection(tmp_path, monkeypatch):
    before = "class Monitor:\n    def offer(self):\n        return 0\n"
    after = before.replace("return 0", 'return {"cut": self.value}')
    write(tmp_path, "src/state.py", after)
    stop = threading.Event()
    original = review_source._returned_attrs

    def cancel(*args, **kwargs):
        stop.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(review_source, "_returned_attrs", cancel)
    packet = review_source_context(tmp_path, diff("src/state.py", before, after), [], stop_event=stop)
    assert packet["cancelled"] and not packet["blocks"]
    assert packet["stats"]["files_read"] == 1


def test_cyclic_return_aliases_and_staticmethod_targets_do_not_create_writers(tmp_path):
    before = '''class Monitor:
    def offer(self):
        return 0

    @staticmethod
    def mutate(other):
        other.value = 3
'''
    after = before.replace("return 0", "left = right\n        right = left\n        return {\"cut\": left}")
    write(tmp_path, "src/state.py", after)
    packet = review_source_context(tmp_path, diff("src/state.py", before, after), [])
    assert find(packet, "state.py", "Monitor.offer")["complete"]
    assert packet["stats"]["state_edges"] == 0
    assert not any(row["role"] == "protocol_state_writer" for row in packet["blocks"])


def test_oversized_hunk_numbers_are_declined_without_integer_parse_failure(tmp_path):
    write(tmp_path, "src/agent.py", "def send():\n    return 1\n")
    patch = "--- a/src/agent.py\n+++ b/src/agent.py\n@@ -1 +" + "9" * 5000 + " @@\n-x\n+y\n"
    packet = review_source_context(tmp_path, patch, [])
    assert packet["truncated"] and not packet["blocks"]


def test_generation_reset_includes_caller_handoff_through_imported_helper(tmp_path):
    before = '''class ReceiptState:
    def admit(self, generation):
        self.generation = generation
        self.last_receipt = 100
'''
    after = before.replace('100', '-1')
    write(tmp_path, "src/state.py", after)
    write(tmp_path, "src/lease.py", '''from state import ReceiptState
class Lease:
    def renew(self, generation):
        self.state.admit(generation)
        return generation
''')
    write(tmp_path, "src/owner.py", '''from lease import Lease
class Owner:
    def renew_remote(self, generation):
        self.lease.renew(generation)
        for task in self.tasks:
            task.generation = generation
''')
    result = review_source_context(tmp_path, diff("src/state.py", before, after), [])
    assert find(result, "state.py", "ReceiptState.admit")["complete"]
    assert find(result, "lease.py", "Lease.renew")["complete"]
    assert 'task.generation = generation' in find(result, "owner.py", "Owner.renew_remote")["text"]


def test_changed_pacing_constant_includes_unchanged_importing_consumer(tmp_path):
    before = "RETRY_DELAY = 0.0\n"
    after = "RETRY_DELAY = 0.1\n"
    write(tmp_path, "src/settings.py", after)
    write(tmp_path, "src/client.py", "from settings import RETRY_DELAY\ndef retry(clock):\n    clock.sleep(RETRY_DELAY)\n")
    result = review_source_context(tmp_path, diff("src/settings.py", before, after), [])
    assert 'RETRY_DELAY = 0.1' in find(result, "settings.py", "<module>")["text"]
    assert 'clock.sleep(RETRY_DELAY)' in find(result, "client.py", "retry")["text"]


def test_explicit_cited_source_line_is_complete_even_when_unchanged(tmp_path):
    before = "def repair():\n    return 0\n"
    after = before.replace("0", "1")
    write(tmp_path, "src/repair.py", after)
    write(tmp_path, "src/consumer.py", "def consume(token):\n    return token.receipt\n\ndef unrelated():\n    return 9\n")
    result = review_source_context(tmp_path, diff("src/repair.py", before, after),
                                   [{"evidence": ["src/consumer.py:2 checks the received token."]}])
    cited = find(result, "consumer.py", "consume")
    assert cited["complete"] and cited["role"] == "cited"
    assert not any(block["symbol"] == "unrelated" for block in result["blocks"])


def test_ambiguous_bare_citation_is_reported_instead_of_guessing(tmp_path):
    before = "def repair():\n    return 0\n"
    after = before.replace("0", "1")
    write(tmp_path, "src/repair.py", after)
    for package in ("left", "right"):
        write(tmp_path, f"src/{package}/consumer.py", "def consume():\n    return 3\n")
    result = review_source_context(tmp_path, diff("src/repair.py", before, after), [{"evidence": ["consumer.py:2"]}])
    assert any(row["reason"] == "citation_unavailable_or_ambiguous" for row in result["omissions"])
    assert not any(block["path"].endswith("consumer.py") for block in result["blocks"])


@pytest.mark.parametrize("reference", ["src/../../outside.py", "/outside.py"])
def test_diff_and_citations_cannot_read_outside_src(tmp_path, reference):
    outside = tmp_path.parent / "outside.py"
    outside.write_text("SECRET_MARKER = 'foreign'\n")
    write(tmp_path, "src/safe.py", "def safe():\n    return 1\n")
    malicious = f"--- a/{reference}\n+++ b/{reference}\n@@ -1 +1 @@\n-x\n+y\n"
    result = review_source_context(tmp_path, malicious, [{"evidence": [reference + ":1"]}])
    assert "SECRET_MARKER" not in json.dumps(result)
    assert all(block["path"].startswith("src/") for block in result["blocks"])


@pytest.mark.parametrize("ancestor", [False, True])
def test_source_symlink_or_symlink_directory_is_never_followed(tmp_path, ancestor):
    outside = tmp_path / "foreign"
    outside.mkdir()
    write(outside, "secret.py", "SECRET_MARKER = 'foreign'\n")
    (tmp_path / "src").mkdir()
    if ancestor:
        (tmp_path / "src/link").symlink_to(outside, target_is_directory=True)
        name = "src/link/secret.py"
    else:
        (tmp_path / "src/secret.py").symlink_to(outside / "secret.py")
        name = "src/secret.py"
    result = review_source_context(tmp_path, diff(name, "SECRET_MARKER = 0\n", "SECRET_MARKER = 1\n"), [])
    assert "foreign" not in json.dumps(result)
    assert result["stats"]["files_read"] == 0
    assert result["stats"]["excluded_links"] == 1
    assert result["truncated"]


def test_fifo_source_is_declined_without_blocking(tmp_path):
    (tmp_path / "src").mkdir()
    os.mkfifo(tmp_path / "src/channel.py")
    result = review_source_context(tmp_path, diff("src/channel.py", "x = 0\n", "x = 1\n"), [])
    assert result["stats"]["files_read"] == 0
    assert any(row["reason"] == "nonregular_source" for row in result["omissions"])


def test_source_root_itself_cannot_be_a_symlink(tmp_path):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    write(foreign, "secret.py", "SECRET_MARKER = 'outside'\n")
    (tmp_path / "src").symlink_to(foreign, target_is_directory=True)
    result = review_source_context(tmp_path, diff("src/secret.py", "x = 0\n", "x = 1\n"), [])
    assert not result["blocks"] and result["stats"]["files_read"] == 0
    assert any(row["reason"] == "source_root_unavailable" for row in result["omissions"])


def test_large_function_keeps_changed_excerpt_without_claiming_completeness(tmp_path):
    before = "def process():\n" + "".join(f"    value_{i} = {i}\n" for i in range(300)) + "    return value_299\n"
    after = before.replace("value_150 = 150", "value_150 = 151  # changed boundary")
    write(tmp_path, "src/process.py", after)
    result = review_source_context(tmp_path, diff("src/process.py", before, after), [], max_chars=3000)
    block = find(result, "process.py", "process")
    assert not block["complete"] and block["omitted_lines"] > 0
    assert "changed boundary" in block["text"] and "def process" in block["text"]
    assert block["segments"] and result["truncated"]
    assert len(json.dumps(result, ensure_ascii=False)) <= 3000


def test_tiny_output_limit_returns_bounded_explicit_omissions(tmp_path):
    before = "def action():\n    return 0\n"
    after = before.replace("0", "1")
    write(tmp_path, "src/action.py", after)
    result = review_source_context(tmp_path, diff("src/action.py", before, after), [], max_chars=256)
    assert result["truncated"] and len(json.dumps(result, ensure_ascii=False)) <= 256


def test_byte_limits_account_for_invalid_encoding_and_do_not_reread_failed_source(tmp_path, monkeypatch):
    monkeypatch.setattr(review_source, "MAX_TOTAL_BYTES", 64)
    (tmp_path / "src").mkdir()
    (tmp_path / "src/a.py").write_bytes(b"\xff" * 48)
    write(tmp_path, "src/b.py", "# padding\n" * 5)
    result = review_source_context(tmp_path, diff("src/a.py", "x = 0\n", "x = 1\n"), [{"evidence": ["src/a.py:1"]}])
    assert result["stats"]["bytes_read"] == 48 and result["stats"]["files_read"] == 1
    reasons = {row["reason"] for row in result["omissions"]}
    assert "source_encoding" in reasons and "source_byte_limit" in reasons


def test_entry_limit_keeps_direct_changed_source_and_reports_incomplete_inventory(tmp_path, monkeypatch):
    monkeypatch.setattr(review_source, "MAX_ENTRIES", 2)
    before = "def repair():\n    return 0\n"
    after = before.replace("0", "1")
    write(tmp_path, "src/changed.py", after)
    write(tmp_path, "src/a.py", "def other():\n    return 9\n")
    write(tmp_path, "src/z.py", "def another():\n    return 7\n")
    result = review_source_context(tmp_path, diff("src/changed.py", before, after), [])
    assert find(result, "changed.py", "repair")["complete"]
    assert result["stats"]["entries_seen"] <= 3
    assert any(row["reason"] == "inventory_entry_limit" for row in result["omissions"])


def test_call_graph_seed_limit_preserves_changed_functions_and_reports_coverage(tmp_path, monkeypatch):
    monkeypatch.setattr(review_source, "MAX_LINKS", 1)
    before = '''def first():
    return helper_one(0)

def second():
    return helper_two(0)

def helper_one(value):
    return value

def helper_two(value):
    return value
'''
    after = before.replace("helper_one(0)", "helper_one(1)").replace("helper_two(0)", "helper_two(1)")
    write(tmp_path, "src/changes.py", after)
    result = review_source_context(tmp_path, diff("src/changes.py", before, after), [])
    assert find(result, "changes.py", "first")["complete"]
    assert find(result, "changes.py", "second")["complete"]
    assert find(result, "changes.py", "helper_one")["complete"]
    assert not any(block["symbol"] == "helper_two" for block in result["blocks"])
    assert result["stats"]["graph_seeds"] == 1 and result["stats"]["graph_seeds_omitted"] == 1
    assert any(row["reason"] == "call_graph_seed_limit" for row in result["omissions"])


def test_total_symbol_index_limit_reports_unsearched_dependencies(tmp_path, monkeypatch):
    monkeypatch.setattr(review_source, "MAX_SYMBOLS", 2)
    before = "def changed():\n    return 0\n"
    after = before.replace("0", "1")
    write(tmp_path, "src/a.py", after)
    write(tmp_path, "src/b.py", "def unrelated():\n    return 2\n")
    write(tmp_path, "src/c.py", "def other():\n    return 3\n")
    result = review_source_context(tmp_path, diff("src/a.py", before, after), [])
    assert find(result, "a.py", "changed")["complete"]
    assert result["stats"]["symbols_indexed"] == 2 and result["stats"]["symbols_omitted"] == 1
    assert any(row["reason"] == "symbol_index_limit" for row in result["omissions"])
    assert result["truncated"]


def test_cancel_before_or_during_read_does_not_continue_source_scan(tmp_path, monkeypatch):
    before = "def action():\n    return 0\n"
    after = before.replace("0", "1")
    write(tmp_path, "src/action.py", after)
    stopped = threading.Event()
    stopped.set()
    result = review_source_context(tmp_path, diff("src/action.py", before, after), [], stop_event=stopped)
    assert result["cancelled"] and not result["blocks"] and result["stats"]["files_read"] == 0
    stopped.clear()
    real_read = os.read

    def read(fd, length):
        data = real_read(fd, length)
        stopped.set()
        return data

    monkeypatch.setattr(os, "read", read)
    result = review_source_context(tmp_path, diff("src/action.py", before, after), [], stop_event=stopped)
    assert result["cancelled"] and not result["blocks"]
    assert result["stats"]["files_read"] == 1 and result["stats"]["bytes_read"] <= 8192


def test_case_python_is_parsed_without_execution(tmp_path):
    marker = tmp_path / "executed"
    before = f"from pathlib import Path\nPath({str(marker)!r}).write_text('unsafe')\ndef action():\n    return 0\n"
    after = before.replace("return 0", "return 1")
    write(tmp_path, "src/action.py", after)
    result = review_source_context(tmp_path, diff("src/action.py", before, after), [])
    assert find(result, "action.py", "action")["complete"] and not marker.exists()


@pytest.mark.parametrize("max_chars", [0, -1, True, 65_000])
def test_invalid_output_budget_is_rejected(tmp_path, max_chars):
    with pytest.raises(ValueError, match="max_chars"):
        review_source_context(tmp_path, "", [], max_chars=max_chars)


@pytest.mark.parametrize("delegated", [
    "def delegate(node, peers):\n        for node in peers:\n            node.value = 7\n",
    "async def delegate(node, peers):\n        async for node in peers:\n            node.value = 7\n",
    "def delegate(node, peer):\n        if (node := peer):\n            node.value = 7\n",
    "def delegate(node, resource):\n        with resource as node:\n            node.value = 7\n",
    "async def delegate(node, resource):\n        async with resource as node:\n            node.value = 7\n",
    "def delegate(node, error):\n        try:\n            raise error\n        except ValueError as node:\n            node.value = 7\n",
    "def delegate(node, peer):\n        node, other = peer, None\n        node.value = 7\n",
    "def delegate(node):\n        del node\n        node.value = 7\n",
    "def delegate(node):\n        import peer as node\n        node.value = 7\n",
    "def delegate(node):\n        from peers import node\n        node.value = 7\n",
    "def delegate(node):\n        def node():\n            pass\n        node.value = 7\n",
    "def delegate(node, peer):\n        match peer:\n            case node:\n                node.value = 7\n",
])
def test_parameter_binding_targets_cannot_label_peer_writes_as_instance_state(tmp_path, delegated):
    before = '''class Monitor:
    def offer(node):
        return {"cut": 0}

    def advance(node):
        shadow = lambda node: (node := None)
        node.value += 1

    ''' + delegated
    after = before.replace('return {"cut": 0}', 'return {"cut": node.value}')
    write(tmp_path, "src/monitor.py", after)
    result = review_source_context(tmp_path, diff("src/monitor.py", before, after), [])
    writer = find(result, "monitor.py", "Monitor.advance")
    assert writer["role"] == "protocol_state_writer" and writer["complete"]
    assert not any(block["symbol"] == "Monitor.delegate" for block in result["blocks"])
    assert any(row.get("symbol") == "Monitor.delegate" and row["reason"] == "state_instance_rebound"
               for row in result["omissions"])


def test_per_function_ast_limit_reports_incomplete_state_discovery(tmp_path, monkeypatch):
    monkeypatch.setattr(review_source, "MAX_FILE_BYTES", 4096)
    before = '''class Monitor:
    def offer(node):
        return {"cut": 0}

    def advance(node, receipt):
        node.value = receipt
        ''' + "a;" * 1500 + "\n"
    after = before.replace('return {"cut": 0}', 'return {"cut": node.value}')
    assert len(after.encode()) < 4096
    write(tmp_path, "src/monitor.py", after)
    result = review_source_context(tmp_path, diff("src/monitor.py", before, after), [])
    assert find(result, "monitor.py", "Monitor.offer")["complete"]
    assert result["truncated"]
    assert any(row.get("symbol") == "Monitor.advance" and row["reason"] == "state_analysis_limit"
               for row in result["omissions"])
