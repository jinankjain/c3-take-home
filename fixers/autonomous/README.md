# Autonomous C3 fixer

A Python controller gives Claude Sonnet 4.6 local tools to investigate an incident, retrieve current contracts, reproduce a failure, edit a staged source tree, and validate the repair. The entrypoint takes no arguments during grading.

## How it works

1. Copy the case into original, accepted and candidate snapshots. Run the original public tests and supplied public simulator seeds.
2. Read the incident, invariants and source inventory. Retrieve relevant service/platform documents from `C3_CORPUS`, starting at `INDEX.md` and prioritizing current versions.
3. Let Sonnet inspect files and traces, record hypotheses, create deterministic scenario probes, and edit only candidate `src/` files.
4. Check candidates against public tests, public seeds, three additional seeds, submitted probes and retained earlier experiments. Require reproduced progress, preserve previously passing scenarios and repaired obligations, and check replay determinism.
5. Review the patch with a separate Sonnet request. Promote accepted source, rerun tests and a replay in the actual workspace, and roll back failed installation verification. Continue investigating remaining failures.

A full passing local matrix can be accepted if model review is unavailable. Partial repairs require model review. Public success is evidence for a repair; the grader determines whether hidden bugs are fixed.

| Module | Responsibility |
| --- | --- |
| `main.py`, `controller.py`, `tools.py` | Model loop, hypotheses, probes, acceptance and final verification |
| `retrieval.py` | Bounded file access, document discovery, current-version ranking and focused excerpts |
| `evidence.py`, `validation.py` | Trace summaries, replay/check results, source/scenario hashes and deterministic repeats |
| `patches.py` | Isolated snapshots, exact edits, Python compilation, installation journals and rollback |
| `model.py`, `runtime.py` | Proxy requests, spend reservations, subprocess limits and cancellation |

## Verification status

The automated suite has **235 passing tests**, covering invented services and local proxy responses. A baseline smoke run completed on all six practice cases: public tests passed, simulator failures were detected, and service source stayed unchanged.

With the funded key, B1 directory grading fixed **1/1 bug across all 20 grading seeds**, passed public and private regression checks, took 128.9 seconds and spent $0.9807. The `linux/amd64` image builds and loads its pinned dependencies on Docker Desktop 29.9.2. Full image grading is in progress.

See [REVIEW.md](REVIEW.md) for the review findings, regression coverage and remaining verification gates, and [WRITEUP.md](WRITEUP.md) for the submission summary.

## Run locally

From the repository root, with Python 3.12 or newer:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r fixers/autonomous/requirements.txt
.venv/bin/python -m pytest -q fixers/autonomous/tests
```

Put the supplied practice OpenRouter key in the repository's `.env`, then start the proxy in another terminal:

```sh
.venv/bin/python proxy/server.py
```

Grade a case through the same proxy and budgets as the pack:

```sh
PATH="$PWD/.venv/bin:$PATH" \
C3_GRADER_PYTHON="$PWD/.venv/bin/python" \
./grade.sh --fixer fixers/autonomous --cases practice/B1
```

Omit `--cases` to run all practice cases. Grading makes paid model requests using the practice budget. The proxy reads `.env` when it starts; restart it after changing that file.

Directory grading preserves `PATH` but clears custom interpreter overrides, so the virtual environment must be on `PATH`. `C3_FIXER_PYTHON` works for direct entrypoint invocation.

For a baseline investigation with **no model requests**, set the required C3 case environment and run:

```sh
C3_FIXER_PYTHON="$PWD/.venv/bin/python" \
fixers/autonomous/run.sh --diagnose
```

This runs tests, replays and corpus retrieval and leaves service source unchanged. It still requires the case/workspace, corpus, budget and deadline variables below; use a disposable case copy when running by hand.

## Build and grade the submission image

```sh
docker build --platform linux/amd64 --provenance=false -t c3-fixer-autonomous fixers/autonomous
C3_GRADER_PYTHON="$PWD/.venv/bin/python" \
./grade.sh --fixer c3-fixer-autonomous
docker save -o c3-fixer-autonomous.tar c3-fixer-autonomous
```

The image uses Python 3.12.12 and installs pinned PyYAML/pytest dependencies at build time. Its entrypoint is `/bin/sh /fixer/run.sh`; `fixer.main` changes to the workspace supplied by `C3_WORKSPACE`. The image contains the fixer package and entrypoint, with no case code, corpus, grader, tests or credentials copied into it. Runtime network access is needed only for the supplied model proxy.

`--provenance=false` exports a single platform image. On Docker 29, an image index can omit the OS and architecture fields read by the supplied grader. Check the exported image before grading:

```sh
docker image inspect c3-fixer-autonomous --format '{{.Os}}/{{.Architecture}} {{.Size}}'
```

The current image reports `linux/amd64`; its saved tarball is 50,516,480 bytes, below the pack's 4 GB limit. Grade the exact image on every practice case before submission.

On macOS, Docker Desktop's CLI may be installed under `$HOME/.docker/bin` without being on `PATH`. In that shell, run:

```sh
export PATH="$HOME/.docker/bin:$PATH"
```

The grader's host socket transport was checked successfully on this Docker Desktop installation using a container with network access disabled.

## Configuration and limits

| Variable | Use |
| --- | --- |
| `C3_WORKSPACE` | Writable case directory containing `src/`; paths are not hardcoded |
| `C3_CORPUS` | Read-only corpus directory containing `INDEX.md` |
| `C3_DEADLINE` | Unix timestamp; converted to a monotonic deadline |
| `C3_BUDGET_USD` | Per-case model budget |
| `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY` | Anthropic-format proxy URL and per-case token |
| `ANTHROPIC_MODEL` | If set, must be `anthropic/claude-sonnet-4.6` |
| `OPENAI_BASE_URL` | Optional proxy URL for its budget endpoint; local accounting works without it |
| `C3_FIXER_PYTHON` | Optional interpreter override for direct invocation; directory grading uses `PATH` |
| `FIXER_THINKING_TOKENS` | Optional thinking budget; default `0`, capped by the response allowance and the proxy's 16k limit |

The controller runs at most two tool subprocesses concurrently and up to 32 repair turns. It bounds file scans, context, responses and command output. Model requests reserve their worst estimated spend before submission and settle against returned usage; prompt caching is provided by the proxy. A rejected spend reservation can trigger one smaller-context/output retry.

Upstream account payment failures stop model requests for that case. An unvalidated candidate can receive one final validation attempt when the loop ends. Completed validation verdicts are retained by source hash.

Time is reserved for installed-source verification and rollback, initially up to 120 seconds and adjusted from baseline measurements up to 180 seconds. SIGTERM/SIGINT sets a cancellation flag; local jobs and HTTP workers observe it and stop. HTTP runs in a separate process so a blocked response can be interrupted. Jev is not currently used; adding cheap relevance decisions is a possible extension.

## State, logs and verification limits

Only live `src/` changes are the submission output. Candidate edits require the current source hash, reject escaping/symlink paths, and compile Python before writing. Tests and simulator files are copied for staging and are not edited by the model tools.

Scratch state lives under `C3_WORKSPACE/.c3-fixer-*`: source snapshots, probes, replay traces and promotion journals. It is disposable and excluded from normal file searches. JSON logs on stdout include baseline results, hypotheses, tools, reviews, accepted repairs, final validation and model spend. `grade.sh` writes `result.json`, `fixer.log`, `patch.diff` and `eval.json` under `results/<timestamp>/<case>/`.

Local checks cannot certify the grader's reference-based goodput limit, private regression tests, or all 20 hidden seeds. The implementation uses general source/contracts and contains no practice-case patch table. Practice results and exact-image verification remain necessary evidence for submission.
