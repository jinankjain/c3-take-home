"""The static context the agent sees on every turn (sent once, then served from the prompt cache)."""
from __future__ import annotations

import json
import re
from pathlib import Path

CASE_DOCS = ["SYMPTOM.md", "README.md", "INVARIANTS.md", "scenario_schema.md", "case.json"]
MAX_SRC_CHARS = 250_000
MAX_DOC_CHARS = 220_000

INSTRUCTIONS = """\
You are an autonomous engineer fixing bugs in a small distributed Python service that runs on a
deterministic simulator. Nobody will answer questions; work only through the tools.

Goal: make the incident(s) in SYMPTOM.md go away by fixing the service's source under src/.
- There may be MORE THAN ONE bug, even if the ticket reads like one problem. Fixing one bug can
  expose another that it was hiding. Keep going until every failure you can reproduce is gone.
- Bugs are graded on harsher, bug-specific scenarios with unseen seeds. A fix must be correct in
  general, not just on the public seeds. Fix the root cause, not the symptom in one log.
- The service documentation and platform contracts (below, and grep corpus) are the spec. When
  code and docs disagree, the CURRENT docs win. Constants and protocol rules often live only there.
- Only src/ is graded. Never "fix" by editing sim/, tests/ or scenarios/. Do not weaken the
  service to pass checks (e.g. rejecting requests, dropping retries, disabling features):
  private regression tests will catch it and score the case 0.
- Keep patches minimal and in the existing style. The simulator must stay deterministic: no
  wall-clock time, no unseeded randomness, no iteration over sets whose order can vary.

How to work:
1. Reproduce EVERY ticket item. Map each item/symptom in SYMPTOM.md to a run (public scenario,
   with or without a fault file) that fails on the original code because of it; the baseline below
   shows which runs fail. Green runs do not prove an item absent: many symptoms (spurious cancels,
   retry storms, duplicate work, extra registrations) break no invariant and only show up through an
   expect check. For any item with no failing run, write_scenario (base: public) under the conditions
   the ticket describes, with an expect block encoding the symptom (keys in sim/c3sim/check.py
   below), and confirm it fails on the original code. Grading uses bug-specific scenarios like that.
   Encode the symptom itself, not generic liveness: an action the ticket says should not happen
   ("gets cancelled", "re-queued", "registers again") is usually an RPC (rpc_requests_max with
   src/dst/method; find the method name in src/) or a log event (log_events_max; the event names
   seen in the evidence logs are listed below). Do this BEFORE editing code.
   Use trace_query on a failing seed to follow the exact job/key through the logs.
2. Diagnose: find the exact line that violates the documented behaviour. Cite the doc.
3. Fix with edit, then run_sim again (several seeds, several fault files).
4. checkpoint() runs the acceptance check (tests, sweeps over all fault files and many seeds,
   determinism, message counts). Only checkpointed code is kept. If it is rejected, read why.
5. When the check passes and nothing reproducible is left, call done().
Be economical: time and model budget are limited. Prefer targeted reads over whole files once
you know where to look. Several tool calls in one turn are fine when they are independent."""


def _read(p: Path) -> str:
    try:
        return p.read_text(errors="replace")
    except OSError:
        return ""


def superseded_docs(corpus: Path) -> set[str]:
    """Docs marked SUPERSEDED in the corpus INDEX.md (relative paths)."""
    out = set()
    for line in _read(corpus / "INDEX.md").splitlines():
        m = re.match(r"\|\s*`([^`]+)`\s*\|\s*SUPERSEDED", line)
        if m:
            out.add(m.group(1))
    return out


def corpus_docs(ws: Path, corpus: Path) -> list[Path]:
    """Service docs for this case's codebase, then CURRENT platform docs."""
    try:
        codebase = json.loads(_read(ws / "case.json")).get("codebase")
    except json.JSONDecodeError:
        codebase = None
    services = corpus / "service"
    dirs = [services / codebase] if codebase and (services / codebase).is_dir() else (
        sorted(d for d in services.iterdir() if d.is_dir()) if services.is_dir() else [])
    skip = superseded_docs(corpus)
    files = []
    for d in dirs + [corpus / "platform"]:
        if d.is_dir():
            files += [f for f in sorted(d.rglob("*")) if f.is_file()
                      and str(f.relative_to(corpus)) not in skip]
    return files


def log_event_counts(logs: Path, limit: int = 120) -> list[tuple[str, int]]:
    """`event=NAME` occurrences across the evidence logs, most frequent first."""
    counts: dict[str, int] = {}
    for f in sorted(logs.glob("*.log")) if logs.is_dir() else []:
        for m in re.finditer(r"\bevent=([A-Za-z0-9_.:-]+)", _read(f)):
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]


def build(ws: Path, corpus: Path, baseline_text: str) -> tuple[str, list[str]]:
    """Return (context_text, notes). Notes list anything left out for size."""
    notes = []
    parts = ["# CASE DOCUMENTS"]
    for name in CASE_DOCS:
        p = ws / name
        if p.exists():
            parts.append(f"\n===== {name} =====\n{_read(p)}")

    parts.append("\n# SOURCE (src/)")
    used = 0
    for f in sorted((ws / "src").rglob("*.py")):
        if "__pycache__" in f.parts:
            continue
        text = _read(f)
        rel = f.relative_to(ws)
        if used + len(text) > MAX_SRC_CHARS:
            notes.append(f"source not inlined (size): {rel}")
            continue
        used += len(text)
        numbered = "\n".join(f"{i:4d}| {ln}" for i, ln in enumerate(text.splitlines(), 1))
        parts.append(f"\n===== {rel} =====\n{numbered}")

    parts.append("\n# CORPUS DOCUMENTS (service + CURRENT platform; read others with read_file corpus/...)")
    used = 0
    for f in corpus_docs(ws, corpus):
        text = _read(f)
        rel = f.relative_to(corpus)
        if used + len(text) > MAX_DOC_CHARS:
            notes.append(f"doc not inlined (size): corpus/{rel}")
            continue
        used += len(text)
        parts.append(f"\n===== corpus/{rel} =====\n{text}")
    if notes:
        parts.append("\n# NOT INLINED (use read_file / grep)\n" + "\n".join(notes))

    events = log_event_counts(ws / "logs")
    if events:
        parts.append("\n# LOG EVENTS IN THE EVIDENCE LOGS (event name: count across logs/; usable in "
                     "log_events_max/min)\n" + ", ".join(f"{k}: {v}" for k, v in events))
    checker = ws / "sim" / "c3sim" / "check.py"
    if checker.exists():
        parts.append("\n# HOW `expect` IS CHECKED (sim/c3sim/check.py; BUILTIN_EXPECT lists the keys)\n"
                     + _read(checker))
    parts.append("\n# FAULT FILES\n" + "\n".join(
        f"scenarios/faults/{p.name}" for p in sorted((ws / "scenarios" / "faults").glob("*.yaml"))))
    parts.append("\n# BASELINE (unfixed code): public scenario and each fault file\n" + baseline_text)
    return "\n".join(parts), notes
