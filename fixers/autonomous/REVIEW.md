# Engineering review record

## Scope

Reviewed source staging and promotion, process limits, corpus retrieval, simulator evidence, acceptance rules, native model messages, budget accounting, entrypoint behavior and submission packaging. Review combined source inspection with regression tests using invented services and local fake-proxy responses. These checks establish controller behavior; they do not establish autonomous repair performance on unseen services.

## Resolved risks and regression evidence

| Area | Resolution and verification |
| --- | --- |
| Source state — `patches.py` | Candidate changes use real copies and source hashes. Edits reject stale hashes and escaping/symlink paths and compile complete Python replacements. Promotion verifies the actual workspace and can restore additions, deletions and file/directory replacements. Repeated promotion attempts use distinct journals. Tests cover installation failure, rejected verification, stale validation, preserved source packages and rollback to the preceding accepted repair (`test_patches.py`). Replacement data is prepared before the short installation region. |
| Processes — `runtime.py` | Commands use argument arrays; no shell interpretation occurs. A shared runner limits concurrency, applies absolute deadlines, observes cancellation, bounds captured output and kills process groups. Group termination is idempotent. Tests cover literal shell characters, timeout, cancellation, large output and descendants surviving their parent (`test_runtime.py`). |
| Retrieval — `retrieval.py` | Paths and symlinks are guarded; scratch, secrets and generated files are excluded. Current contracts precede superseded versions. Incomplete cache entries reload, discovery shares a bounded scan budget, and excerpts retain late matches and their line numbers. Tests include invented service names, formatted status markers, large documents and pathological regex rejection (`test_retrieval.py`). |
| Simulator evidence — `validation.py`, `evidence.py` | Exit code zero alone cannot certify a repair. Checked results track complete checker verdicts, source/effective-scenario identity, trace hashes and action resolution. Fresh repeated processes check determinism. Tests cover changed inputs, malformed/missing evidence, timeout, stage imports and nondeterministic traces (`test_validation.py`, `test_evidence.py`). |
| Acceptance — `controller.py` | Partial repairs require reproduced progress and review. Previously passing scenarios and retained repaired obligations cannot regress. Targeted probes must be deterministic. Final failed tests restore a previous checkpoint; interrupted final tests retain the repair verified during promotion. Synthetic tests exercise these outcomes (`test_controller.py`). The reserve regression permits validation when remaining time is below the investigation reserve while preserving installed-source verification and rollback time. |
| Model protocol — `model.py`, `main.py` | Native tool calls/results remain paired during context compaction. Incomplete responses cannot execute tools. Requests reserve estimated spend and account for cached usage, rejection and ambiguous transport outcomes. HTTP workers receive request secrets through stdin and return bounded responses. Fake-proxy/native-message tests cover malformed responses, duplicate tool IDs, blocked HTTP cancellation, credential-safe diagnostics, independent recovery counters and an orchestrated patch (`test_model.py`, `test_main.py`, `test_tools.py`). |

## Evidence and remaining gates

- Final offline suite: **173 tests passed in 32.32 seconds** with `.venv/bin/python -m pytest -q fixers/autonomous/tests`. Local protocol tests use scripted responses; actual Sonnet repair performance remains unmeasured.
- Baseline workflows completed on all six practice cases. Their public tests passed, simulator failures were detected, and source stayed unchanged with $0 model spend.
- The real B1 directory smoke result was **0/1 bugs fixed, $0 model spend and no source patch**. The upstream account's confirmed credit balance was zero. This run does not measure repair capability or useful model latency.
- Docker is absent on this host. Static packaging/invocation checks passed, but image build, saved size and grading of the exact Linux image on all practice cases remain unverified.
- Public checks cannot certify private regression tests, all hidden seeds or the grader's reference-based goodput limit.

## Final review closure

The loop now gives an unvalidated candidate one final controller-gated attempt after a budget or turn limit. Cancellation and insufficient installation time skip this attempt. Completed validation hashes preserve an unchanged earlier review rejection. The 11 main tests cover full promotion, partial rejection without review, cancellation, time limits and this rejection guard.

Payment handling now separates exact case-budget refusals from upstream account failures using bounded, sanitized error envelopes and a fresh remote balance. Upstream failures stop further model requests for that case. Reported billed usage is accounted for even when the optional budget endpoint is unavailable. The 36 fake-proxy tests cover these cases, invalid usage and malformed/oversized responses. Both fixes passed independent source review.
