# Autonomous C3 fixer

A Python controller gives Claude Sonnet 4.6 local tools to investigate an incident, retrieve current contracts, reproduce a failure, edit a staged source tree, and validate the repair. The entrypoint takes no arguments during grading.

## How it works

1. Copy the case into original, accepted and candidate snapshots. Run the original public tests and supplied public simulator seeds. Discover healthy public scenarios and fault profiles, cover distinct input-derived holdouts before repeating supplied seeds, and retain passing controls within a bounded bootstrap phase.
2. Read the incident, invariants and source inventory. Retrieve relevant service/platform documents from `C3_CORPUS`, starting at `INDEX.md` and prioritizing current versions.
3. Let Sonnet inspect files and causal trace windows, record hypotheses, compose immutable fault scenarios, and edit only candidate `src/` files. Capture the repair's intended scope against its exact before/after source hashes.
4. Check candidates against public tests, public seeds, submitted probes, healthy controls and retained earlier experiments. Every scenario that established a repaired obligation remains mandatory, including scenarios that still fail another invariant. Add up to three extra seeds when measured replay costs fit the remaining time. Require reproduced progress and replay determinism.
5. Review the patch with a separate Sonnet request using matched before/after inputs, complete changed functions and relevant unchanged protocol handlers. Promote accepted source, rerun tests and a replay in the actual workspace, and roll back failed installation verification. Continue investigating remaining failures.

A full passing local matrix can be accepted if model review is unavailable. Partial repairs require model review. Public success is evidence for a repair; the grader determines whether hidden bugs are fixed.

| Module | Responsibility |
| --- | --- |
| `main.py`, `controller.py`, `tools.py` | Model loop, hypotheses, probes, acceptance and final verification |
| `retrieval.py`, `relevance.py` | Bounded file access, current-version ranking, focused excerpts and conservative Jev filtering |
| `evidence.py`, `validation.py` | Trace summaries, replay/check results, source/scenario hashes and deterministic repeats |
| `review.py`, `review_source.py` | Exact before/after pairs, typed review evidence, changed bodies and unchanged protocol peers |
| `probe_guidance.py` | Bounded resolved configurations and actionable corrections for failed diagnostic construction |
| `experiments.py` | Measured replay scheduling; required public and retained obligations cannot be omitted |
| `controls.py` | Bounded public-input healthy controls, deterministic holdout seeds and coverage accounting |
| `patches.py` | Isolated snapshots, exact edits, Python compilation, installation journals and rollback |
| `model.py`, `runtime.py` | Proxy requests, spend reservations, subprocess limits and cancellation |

## Verification status

The combined fixer and grader suite has **581 passing tests**: 548 fixer checks and 33 grader checks, completed in 127.03 seconds. It covers invented services, local proxy responses, causal evidence, healthy controls, forced diagnostic probes, cached replay progress, matched review inputs, repair intent, complete protocol context and reporting namespaces. Independent reviews found no remaining blocker. An earlier baseline smoke run completed on all six practice cases: public tests passed, simulator failures were detected, and service source stayed unchanged.

The first frozen image (`29a2adb`) fixed **7/12 practice bugs**, after correcting a B1 JUnit namespace mismatch in the reporting harness. Original results are preserved under `results/autonomous-image-full-r1`; the B1 reevaluation and corrected summary are under its `reporting-correction/` directory. That suite spent $10.7682. A2 and B2 motivated the current context, progress and experiment improvements.

The second frozen image, `c3-fixer-autonomous:b347883`, completed all six practice cases at **6/12 bugs for $24.3544**, with no infrastructure errors or timeouts. B2 improved to 1/3; B3 scored 0/2 because its accepted patch introduced baseline regressions. This run did not demonstrate a cost or score improvement over the first image. Artifacts are preserved at `results/autonomous-image-full-r2`.

The B3 miss motivated a public-only healthy-control safeguard: the original source passed all 16 controls, while a retained input-derived seed exposed the saved candidate's regression. The actual controller rejected that candidate before review and preserved the original live source. A separate Linux check retained the useful control within its time allowance and rejected the same saved patch. A2's saved trace also exposed a summary defect: normal capacity events displaced the actual failed RPCs. Corrected summaries retain both failures and their request sends through bounded reviewer output. These checks made no model calls.

The third image, **`553364e`**, finished at **4/12 bugs for $32.4736**, with no infrastructure errors or timeouts. B2 reached 2/3, but B3 repeated the same regression and scored 0/2. Under concurrent grading, the bootstrap stopped after nine controls and omitted the useful later holdout. A1 also exposed lost recent evidence during compaction and misleading trace labels. These findings motivated distinct-holdout ordering, retention of the latest complete tool exchange, stricter failure witnesses, and a diagnostic probe for stalls on accepted source. Results remain at `results/autonomous-image-full-r3`.

The fourth image, **`44e94ee`**, finished at **7/12 for $23.1984**, with every public/regression gate passing and no infrastructure errors or timeouts. B2 passed 2/3. B3's retained crash/profile holdout blocked the exact regressing candidate seen in earlier runs, preserving original source at 0/2. A2 exposed another progress defect: cached replay requests delayed validation until turn 79. The corrected loop counts those requests as information and recognizes fresh controller-owned runs as measurements. Results and audits remain at `results/autonomous-image-full-r4`.

The fifth frozen runtime, **`24e51ae`**, completed all six official cases at **6/12 bugs for $25.4619**, in 2,798.7 cumulative fixer seconds and 1,569.8 grading seconds. Every public-test and regression gate passed, with no infrastructure errors or timeouts. B2 repaired only bug 3, scoring **1/3**; B3 preserved original source at **0/2**. A2 also scored 0/2: the corrected loop triggered validation at turn 14, but model diagnosis, experiment comparisons and probe composition still limited repairs. This run did not improve score or spend over the fourth image. Results and independent audits are at `results/autonomous-image-full-r5`.

Both labs completed against that exact image at **4/4 for $0.711097** in 187.443 cumulative fixer seconds, with all seed and public/private gates passing; results are at `results/autonomous-chained-labs-r4`. Earlier labs also passed 4/4 on `553364e` ($0.932173) and `44e94ee` ($0.746846), with their artifacts preserved. The balance before the followup was **$122.33 used and $27.67 remaining** from the $150 allowance, at `results/artifacts/budget-before-targeted-r6.json`.

The current runtime is frozen at **`92b15ee`**. All 17 module hashes match its `linux/amd64` image. It preserves repair intent during automatic validation, supplies exact paired review evidence and unchanged protocol peers, checks factual regression citations, and carries construction corrections into a second diagnostic attempt within the existing limits. The 581-test gate and image smoke/export checks passed. Exact-image grading of A2, B2 and B3 is running at `results/autonomous-targeted-r6`; a current-image practice score is pending.

Real Jev relevance decisions were verified through the funded proxy: one relevant lease document was retained and an unrelated SQL document rejected, in 0.497 seconds for $0.000026544. This verifies the API integration; corpus searches still retain direct/current/referenced evidence and fall back to lexical results on uncertainty or failure.

Two additional [chained scenarios](labs/README.md) use independent service code. Offline checks prove **0 → 1 → 2 fixes**, deterministic replays, healthy controls, and 20 passing seeds per bug with full private references. That 40.41-second proof makes no model calls. The earlier `b347883` image also solved **4/4 bugs autonomously**, passing all grading seeds and public/private checks for $0.643882 total; artifacts are at `results/autonomous-chained-labs-r1`. Each paid run repaired both bugs in one patch; the offline Controller proof separately exercises partial then full promotion.

See [REVIEW.md](REVIEW.md) for the review findings, regression coverage and measured limitations, and [WRITEUP.md](WRITEUP.md) for the submission summary.

## Run locally

From the repository root, with Python 3.12 or newer:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r fixers/autonomous/requirements.txt
.venv/bin/python -m pytest -q fixers/autonomous/tests grader/tests
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

On macOS, Docker Desktop's CLI may be installed under `$HOME/.docker/bin` without being on `PATH`. In that shell, run this before the Docker commands:

```sh
export PATH="$HOME/.docker/bin:$PATH"
```

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

The frozen **`c3-fixer-autonomous:92b15ee`** image reports `linux/amd64`; its saved tarball is **50,569,728 bytes**, below the pack's 4 GB limit. The tarball and identity metadata are under `results/artifacts/c3-fixer-autonomous-92b15ee-linux-amd64.*`. The tarball SHA-256 is `9f8f0074fdce5d379ff56da89b4b9f524881e3bd60d4c605426cda94495a440f`.

On another machine, copy the repository and frozen tarball, install the host Python dependencies above, start the proxy with the funded `.env`, then load and grade the exact image:

```sh
docker load -i results/artifacts/c3-fixer-autonomous-92b15ee-linux-amd64.tar
PATH="$PWD/.venv/bin:$PATH" \
C3_GRADER_PYTHON="$PWD/.venv/bin/python" \
./grade.sh --fixer c3-fixer-autonomous:92b15ee --concurrency 2 --eval-workers 2 --keep
```

The grader's host socket transport was checked successfully on this Docker Desktop installation using a container with network access disabled.

## Submission artifacts

The local handoff contains the tested image, its source directory and Dockerfile, the half-page write-up, and redacted visible development logs:

| Artifact | Path from the repository root |
| --- | --- |
| Frozen Linux image | `results/artifacts/c3-fixer-autonomous-92b15ee-linux-amd64.tar` |
| Source directory archive | `results/artifacts/c3-fixer-autonomous-source.tar.gz` |
| Write-up | `fixers/autonomous/WRITEUP.md` |
| Development chat logs | `results/artifacts/c3-build-visible-chat-logs.tar.gz` |
| Identities, hashes and result references | `results/artifacts/submission-manifest.json` |

The source archive includes the authored tests and chained fixtures for review; the Dockerfile copies only runtime files. Its documentation commit may follow the frozen runtime; the manifest verifies that every runtime module matches the image. The earlier source, logs, balance and handoff manifest are preserved under `results/artifacts/history-24e51ae`. Current source/log/manifest refresh follows completed grading.

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
| `JEV_DECISIONS_URL` | Optional Decisions route on the same configured proxy; otherwise retrieval stays lexical |
| `JEV_MODEL` | If set, must be `typesafe/jev-1.13` |
| `C3_FIXER_PYTHON` | Optional interpreter override for direct invocation; directory grading uses `PATH` |
| `FIXER_THINKING_TOKENS` | Optional thinking budget; default `0`, capped by the response allowance and the proxy's 16k limit |

The controller runs at most two tool subprocesses concurrently and up to 128 repair turns within the case deadline and spend limit. Compaction targets 120,000 characters with an absolute 180,000-character bound, preserving source-bound findings and the latest complete native tool exchange. It shortens older incident context before dropping that exchange and may exceed the soft target to keep it within the hard bound. Six information-only turns can trigger a concise hypothesis or validation of an unchanged staged candidate. After a completed rejection, they can instead request one new composed probe and replay identical immutable inputs on accepted and candidate snapshots, preserving the rejection and final verification time. On unchanged accepted source with known failures and a durable finding, the intervention runs one accepted-source diagnostic; it is explicitly labelled as an observation with no repair comparison or automatic promotion. Results and source/run identity survive compaction. Attempts are bounded to two per source hash and four per case, with one completed diagnostic per hash. Model requests reserve their worst estimated spend and settle against returned usage; prompt caching is provided by the proxy. A rejected spend reservation can trigger one smaller-context/output retry.

Forced diagnostic requests expose only `compose_probe`, use 2,048 output tokens and disable thinking. Novelty comparisons normalize labels, expectations and the overridden document seed, including already measured scenario/fault combinations. Inputs are limited to 32 protected files, 512 discovery entries and 64 combinations; oversized or unsupported configurations decline the intervention. Measured conservative replay slots range from five to 60 seconds, with 60 seconds as the unknown-cost fallback. A slow or invalid run remains labelled incomplete while preserving final verification and 30 seconds for feedback.

Construction guidance includes up to two resolved base configurations within 8,000 characters. A failed attempt retains up to 4,000 characters of source-bound correction context, including exact conflicting paths, required arguments or incomplete replay status. Conflict responses collect bounded details before publishing a probe; overrides remain explicit. Correction requests share the existing two-per-source/four-per-case attempt limits and replay reserves.

Edits can supply a bounded `repair_plan`; otherwise the controller captures current hypotheses. Automatic validation retains that intent only for the exact candidate and accepted-source basis. Review pairs use scenario hash, effective fault hash and seed together, with host-computed resolved, remaining and newly observed failures. Review source context is bounded to 24,000 characters with complete-body and omission labels. Native blockers distinguish regression, source defect and unverified repair. A false factual regression citation can receive one same-source correction; a legitimate source blocker remains blocking. Factual protocol errors cannot use the unavailable-review fallback.

Replay requests that return existing cached observations count toward the information-turn limit. Genuine new executions register new controller-owned run IDs and reset the streak, including fresh execution of identical inputs. Tool logs report `fresh_replay`; requested filenames and response cache flags cannot establish progress.

Upstream account payment failures stop model requests for that case. An unvalidated candidate can receive one final validation attempt when the loop ends. Completed validation verdicts are retained by source hash.

Time is reserved for installed-source verification and rollback, initially up to 120 seconds and adjusted from baseline measurements up to 180 seconds. SIGTERM/SIGINT sets a cancellation flag; local jobs, trace scans and HTTP workers observe it and stop. HTTP runs in a separate process so a blocked response can be interrupted.

Healthy-control discovery reads only bounded regular documents under the original case's scenarios directory. Up to 16 controls alternate scenario and scenario/profile inputs. Each distinct input receives a stable seed derived from its public bytes before any input repeats the first public seed, so a short bootstrap covers more fault combinations. Bootstrap spends at most 30 seconds or 3% of remaining case time, while preserving final verification time. Passing controls retain their source and effective input hashes and become mandatory candidate checks. Coverage logs report omissions, failures, cancellation and actual execution time. Discovery requires a recognizable public scenario structure; unfamiliar schemas may plan no additional controls.

Optional Jev calls classify at most eight weak document matches of 2,000 characters each. Limits are eight seconds per request, 20 seconds and 12 requests per case, and at most the smaller of $0.02 or 1% of the case budget, while retaining $0.25 for Sonnet. Only confident irrelevant decisions remove weak matches; uncertain or unavailable answers preserve lexical retrieval. All charges share the case ledger.

## State, logs and verification limits

Only live `src/` changes are the submission output. Candidate edits require the current source hash, reject escaping/symlink paths, and compile Python before writing. Tests and simulator files are copied for staging and are not edited by the model tools.

Scratch state lives under `C3_WORKSPACE/.c3-fixer-*`: source snapshots, probes, replay traces and promotion journals. It is disposable and excluded from normal file searches. JSON logs on stdout include baseline results, hypotheses, tools, reviews, accepted repairs, final validation and model spend. `grade.sh` writes `result.json`, `fixer.log`, `patch.diff` and `eval.json` under `results/<timestamp>/<case>/`.

Visible development conversations and tool events from the main agent and its three reviewers are exported with credential redaction at `results/artifacts/c3-build-visible-chat-logs.tar.gz`. The archive contains one JSONL file per agent and a manifest recording capture time, event counts and hashes.

Local checks cannot certify the grader's reference-based goodput limit, private regression tests, or all 20 hidden seeds. The implementation uses general source/contracts and contains no practice-case patch table. Practice results and exact-image verification remain necessary evidence for submission.
