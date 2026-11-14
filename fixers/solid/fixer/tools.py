"""Agent tools: schemas and implementations. Every tool returns a short string."""
from __future__ import annotations

import hashlib
import py_compile
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml

from .context import log_event_counts
from .simgate import COUNT_KEYS, PY

MAX_OUT = 12_000

SCHEMAS = [
    {"name": "read_file",
     "description": "Read a text file with line numbers. Paths are relative to the case workspace "
                    "(src/..., logs/..., scenarios/..., sim/...) or start with corpus/ for the docs corpus.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}},
         "required": ["path"]}},
    {"name": "grep",
     "description": "Regex search (extended regex; use | for alternation). where: src | logs | corpus (service + platform docs) | "
                    "background (research papers, slow, rarely useful) | scenarios | sim. Max 60 hits.",
     "input_schema": {"type": "object", "properties": {
         "pattern": {"type": "string"}, "where": {"type": "string"},
         "ignore_case": {"type": "boolean"}}, "required": ["pattern", "where"]}},
    {"name": "edit",
     "description": "Replace an exact, unique snippet in a file under src/. `old` must match exactly once "
                    "(include enough context). The file must still compile or the edit is undone.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
         "required": ["path", "old", "new"]}},
    {"name": "run_sim",
     "description": "Replay a scenario on seeds and check invariants + the scenario's expect block. "
                    "scenario: a name in scenarios/ (e.g. public, or one you wrote). faults: optional "
                    "fault file path (e.g. scenarios/faults/network.yaml) replacing the scenario's faults. "
                    "seeds: e.g. '11,12' or '1000-1019'. Returns failures per seed with details.",
     "input_schema": {"type": "object", "properties": {
         "scenario": {"type": "string"}, "seeds": {"type": "string"}, "faults": {"type": "string"}},
         "required": ["scenario", "seeds"]}},
    {"name": "trace_query",
     "description": "Replay one seed with the CURRENT code and return lines matching a Python regex (use | for alternation) from its node logs "
                    "(source: logs) or its trace.jsonl (source: trace). Use a key/job/op id as pattern.",
     "input_schema": {"type": "object", "properties": {
         "scenario": {"type": "string"}, "seed": {"type": "integer"}, "faults": {"type": "string"},
         "pattern": {"type": "string"}, "source": {"type": "string", "enum": ["logs", "trace"]},
         "max_lines": {"type": "integer"}}, "required": ["scenario", "seed", "pattern"]}},
    {"name": "write_scenario",
     "description": "Write scenarios/<name>.yaml to reproduce a ticket item the existing runs do not catch. "
                    "With `base` (e.g. 'public'), `yaml` holds only overrides merged onto that scenario "
                    "(mappings merge, lists replace), e.g. faults: faults/x.yaml plus an expect block that "
                    "encodes the ticket's symptom (rpc_requests_max, rpc_errors_max, log_events_max/min, "
                    "outcomes_max, rpc_requests_per_group_max ... see sim/c3sim/check.py). The new scenario "
                    "is run at once on the ORIGINAL code and on the current code, so you see whether it "
                    "reproduces the bug. Written scenarios are part of every checkpoint.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string"}, "yaml": {"type": "string"}, "base": {"type": "string"}},
         "required": ["name", "yaml"]}},
    {"name": "run_tests", "description": "Run the public unit tests (pytest).",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "checkpoint",
     "description": "Run the acceptance check on the current src/. If accepted, this version is kept "
                    "even if later edits go wrong. Takes a while; call it after run_sim looks good.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "revert",
     "description": "Throw away edits since the last accepted checkpoint (or the original code).",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "done",
     "description": "Finish. Call after a checkpoint is accepted and no reproducible failure is left.",
     "input_schema": {"type": "object", "properties": {"summary": {"type": "string"}},
                      "required": ["summary"]}},
]


def _clip(s: str, n: int = MAX_OUT) -> str:
    return s if len(s) <= n else s[: n // 2] + f"\n... [{len(s) - n} chars cut] ...\n" + s[-n // 2:]


def normalize_pattern(pattern: str) -> str:
    """Models often write grep BRE alternation (`a\\|b`); in extended/Python regex that is a literal
    pipe and silently matches nothing. Treat it as alternation (a literal pipe is rare in these logs)."""
    return pattern.replace("\\|", "|")


def parse_seeds(spec: str, cap: int = 60) -> list[int]:
    out: list[int] = []
    for part in str(spec).replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part[1:]:
            a, b = part.split("-", 1)
            out += list(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out[:cap]


class Tools:
    def __init__(self, ws: Path, corpus: Path, gate, orchestrator, log=print):
        self.ws, self.corpus, self.gate, self.orch, self.log = Path(ws), Path(corpus), gate, orchestrator, log
        self.src = (self.ws / "src").resolve()
        self.done_summary: str | None = None
        self._replays = Path(tempfile.mkdtemp(prefix="c3replays-"))

    # ------------------------------------------------------------------ helpers
    def _resolve(self, path: str) -> Path:
        path = path.strip().lstrip("/")
        if path.startswith("corpus/"):
            base, rel = self.corpus.resolve(), path[len("corpus/"):]
        else:
            base, rel = self.ws.resolve(), path
            if path.startswith("workspace/"):
                rel = path[len("workspace/"):]
        p = (base / rel).resolve()
        if p != base and base not in p.parents:
            raise ValueError(f"path escapes the workspace/corpus: {path}")
        return p

    def _faults(self, faults: str | None) -> Path | None:
        if not faults:
            return None
        p = self._resolve(faults)
        if not p.exists():
            raise ValueError(f"no such fault file: {faults}")
        return p

    def dispatch(self, name: str, args: dict) -> str:
        fn = getattr(self, "t_" + name, None)
        if fn is None:
            return f"error: unknown tool {name}"
        try:
            return _clip(fn(**args))
        except TypeError as e:
            return f"error: bad arguments for {name}: {e}"
        except Exception as e:  # noqa: BLE001  (a tool failure must never kill the run)
            return f"error: {type(e).__name__}: {e}"

    # ------------------------------------------------------------------ tools
    def t_read_file(self, path: str, start: int = 1, end: int | None = None) -> str:
        p = self._resolve(path)
        if p.is_dir():
            return "\n".join(sorted(x.name + ("/" if x.is_dir() else "") for x in p.iterdir()))
        lines = p.read_text(errors="replace").splitlines()
        start = max(1, int(start))
        end = min(len(lines), int(end) if end else start + 399)
        body = "\n".join(f"{i:5d}| {lines[i - 1]}" for i in range(start, end + 1))
        more = f"\n[{len(lines)} lines total]" if end < len(lines) or start > 1 else ""
        return body + more

    def t_grep(self, pattern: str, where: str = "src", ignore_case: bool = False) -> str:
        roots = {
            "src": [self.ws / "src"], "logs": [self.ws / "logs"], "scenarios": [self.ws / "scenarios"],
            "sim": [self.ws / "sim"], "corpus": [self.corpus / "service", self.corpus / "platform",
                                                 self.corpus / "INDEX.md"],
            "background": [self.corpus / "background"],
        }.get(where)
        if roots is None:
            return "error: where must be src|logs|corpus|background|scenarios|sim"
        cmd = ["grep", "-rnE", "-I", "--exclude-dir=__pycache__", "-m", "20"]
        if ignore_case:
            cmd.append("-i")
        cmd += ["-e", normalize_pattern(pattern)] + [str(r) for r in roots if r.exists()]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if p.returncode == 2:
            return f"error: {p.stderr.strip()[:300]}"
        lines = []
        for ln in p.stdout.splitlines():
            for prefix, rep in ((str(self.corpus), "corpus"), (str(self.ws) + "/", "")):
                if ln.startswith(prefix):
                    ln = rep + ln[len(prefix):]
                    break
            lines.append(ln[:300])
        if not lines:
            return "no matches"
        extra = f"\n[{len(lines) - 60} more hits not shown]" if len(lines) > 60 else ""
        return "\n".join(lines[:60]) + extra

    def t_edit(self, path: str, old: str, new: str) -> str:
        p = self._resolve(path)
        if self.src not in p.parents:
            return "error: only files under src/ may be edited"
        if not p.exists():
            return "error: file does not exist (edit modifies existing files only)"
        text = p.read_text()
        n = text.count(old)
        if n != 1:
            return f"error: `old` matches {n} times; it must match exactly once"
        updated = text.replace(old, new, 1)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(updated)
        try:
            py_compile.compile(str(tmp), doraise=True, cfile=tempfile.gettempdir() + "/_c3_edit.pyc")
        except py_compile.PyCompileError as e:
            tmp.unlink()
            return f"error: edit not applied, it would not compile: {e.msg[:400]}"
        tmp.replace(p)                                   # atomic: src/ is never half-written
        self.log(f"edit {path}: -{old.count(chr(10)) + 1} +{new.count(chr(10)) + 1} lines")
        return "ok"

    def t_run_sim(self, scenario: str, seeds: str, faults: str | None = None) -> str:
        if not (self.ws / "scenarios" / f"{scenario}.yaml").exists():
            have = sorted(p.stem for p in (self.ws / "scenarios").glob("*.yaml"))
            return f"error: no scenario {scenario!r}; available: {have}"
        r = self.gate.sweep(scenario, parse_seeds(seeds), self._faults(faults))
        if r.error:
            return f"simulator error: {r.error}"
        out = [r.line()]
        for seed, invs in sorted(r.failed.items())[:10]:
            out.append(f"  seed {seed}: {sorted(set(invs))}")
        for inv, ex in r.examples.items():
            out.append(f"  {inv} e.g.: {ex}")
        return "\n".join(out)

    def t_trace_query(self, scenario: str, seed: int, pattern: str, faults: str | None = None,
                      source: str = "logs", max_lines: int = 60) -> str:
        src_hash = hashlib.sha1(b"".join(
            f.read_bytes() for f in sorted(self.src.rglob("*.py")))).hexdigest()[:10]
        key = f"{scenario}-{seed}-{Path(faults).stem if faults else 'nofaults'}-{src_hash}"
        out = self._replays / key
        if not (out / "trace.jsonl").exists():
            cmd = [PY, "-m", "sim", "replay", "--scenario", scenario, "--seed", str(seed), "--out", str(out)]
            f = self._faults(faults)
            if f:
                cmd += ["--faults", str(f)]
            p = subprocess.run(cmd, cwd=self.ws, capture_output=True, text=True, timeout=300)
            if p.returncode != 0:
                return f"replay failed: {(p.stderr or p.stdout)[-600:]}"
        rx = re.compile(normalize_pattern(pattern))
        files = [out / "trace.jsonl"] if source == "trace" else sorted((out / "logs").glob("*.log"))
        hits = []
        for f in files:
            for ln in f.read_text(errors="replace").splitlines():
                if rx.search(ln):
                    hits.append((f.stem + ": " if source != "trace" else "") + ln[:400])
        if not hits:
            return "no matching lines"
        hits.sort(key=lambda h: float(m.group(1)) if (m := re.search(r"\bt[=\":]+\s*([0-9.]+)", h)) else 0)
        max_lines = max(1, min(int(max_lines), 200))
        head = f"[{len(hits)} matching lines; showing {'last ' if len(hits) > max_lines else ''}{min(len(hits), max_lines)}]\n"
        return head + "\n".join(hits[-max_lines:])

    def t_write_scenario(self, name: str, yaml: str, base: str | None = None) -> str:  # noqa: A002
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name) or name in ("public", "base"):
            return "error: name must be [A-Za-z0-9_-], not public/base"
        try:
            doc = _yaml_load(yaml) or {}
        except Exception as e:  # noqa: BLE001
            return f"error: invalid YAML: {e}"
        if not isinstance(doc, dict):
            return "error: the YAML must be a mapping"
        own_expect = doc.get("expect") if isinstance(doc.get("expect"), dict) else {}
        counting = [k for k in own_expect if k in COUNT_KEYS]
        if base:
            bp = self.ws / "scenarios" / f"{base}.yaml"
            if not bp.exists():
                return f"error: no base scenario {base!r}"
            doc = _merge(_yaml_load(bp.read_text()) or {}, doc)
        if "workload" not in doc:
            return "error: scenario must have a workload (give a base, or see scenario_schema.md)"
        doc["name"] = name
        (self.ws / "scenarios" / f"{name}.yaml").write_text(_yaml_dump(doc))
        orig, cur = self.orch.probe_scenario(name, self.gate.public_seeds[:3] + [1000, 1001])
        if orig.failed and not orig.broken and cur.failed and not cur.broken:
            self.orch.note_open_bug(name)          # reproduces and is still failing: real work left
        if orig.broken:
            why = orig.error or orig.examples.get("ERROR", "")
            return (f"wrote scenarios/{name}.yaml, but it is BROKEN: the simulator cannot run it, so it "
                    f"proves nothing and is left out of checkpoints until fixed. Error: {why[:400]}\n"
                    f"(Paths inside a scenario are relative to scenarios/, e.g. faults: faults/x.yaml.)")
        if counting and orig.failed and not orig.counted:
            verdict = (f"fails on the original code, but NOT on your counting check(s) ({', '.join(counting)}): "
                       f"those pass on the original code, and it fails only on other checks. So it does not "
                       f"show an unwanted-event symptom; for that, a counting check must fail on the original "
                       f"code (right event or RPC name, the ticket's conditions, a tight max)")
        elif orig.failed:
            verdict = "REPRODUCES on the original code" + (" (your counting check fails there)" if counting else "")
        else:
            verdict = "does NOT reproduce anything on the original code (adjust faults/expect)"
        out = [f"wrote scenarios/{name}.yaml; it {verdict}.",
               "original code: " + orig.line(),
               "current code:  " + (cur.error[:300] if cur.error else cur.line())]
        out += [f"  {inv} e.g.: {ex}" for inv, ex in list(orig.examples.items())[:3]]
        if counting:
            out.append("counting checks that fail, original code: " + ("; ".join(orig.counted[:3]) or "none"))
            out.append("counting checks that fail, current code:  " + ("; ".join(cur.counted[:3]) or "none"))
        out += self._unknown_events(own_expect.get("log_events_max"))
        return "\n".join(out)

    def _unknown_events(self, counts) -> list[str]:
        """log_events_max counts records by their `event` field. Warn about names that are not events
        in the evidence logs, such as a field value (outcome=missing_from_heartbeat)."""
        if not isinstance(counts, dict):
            return []
        known = dict(log_event_counts(self.ws / "logs", limit=10_000))
        out = []
        for ev in counts:
            if ev in known:
                continue
            hint = ""
            for f in sorted((self.ws / "logs").glob("*.log")):
                m = re.search(rf"\bevent=(\S+)[^\n]*\b\w+={re.escape(str(ev))}\b", f.read_text(errors="replace"))
                if m:
                    hint = (f" It appears as a field value, e.g. in event={m.group(1)} records; count those "
                            f"with an RPC check, or a log event logged only in that case.")
                    break
            out.append(f"note: no log record in logs/ has event={ev}, so log_events_max on it can only "
                       f"pass (it counts the event field, not other fields).{hint}")
        return out

    def t_run_tests(self) -> str:
        ok, tail = self.gate.run_tests()
        return ("PASS\n" if ok else "FAIL\n") + tail

    def t_checkpoint(self) -> str:
        return self.orch.checkpoint()

    def t_revert(self) -> str:
        self.orch.restore_last_good()
        return "src/ restored to the last accepted checkpoint"

    def t_done(self, summary: str = "") -> str:
        self.done_summary = summary or "(no summary)"
        return "ok"

    def cleanup(self):
        shutil.rmtree(self._replays, ignore_errors=True)


def _yaml_load(text: str):
    return yaml.safe_load(text)


def _yaml_dump(doc) -> str:
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=None, width=120)


def _merge(base, over):
    """Deep-merge `over` onto `base`: mappings merge recursively, everything else replaces."""
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = _merge(base.get(k), v) if k in base else v
        return out
    return over
