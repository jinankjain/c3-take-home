# How the fixer works

The fixer is a Python controller around Claude Sonnet 4.6 with local investigation and repair tools. It first runs public tests and deterministic simulator seeds on an untouched snapshot, reads the incident and invariants, and builds a source inventory. Corpus retrieval starts at `INDEX.md`, discovers the relevant service documents, prioritizes current contracts, and returns bounded excerpts. Background research is accessed only through explicit searches.

Sonnet records hypotheses, examines traces and source, constructs scenario probes, and edits a separate candidate tree. The controller associates experiments with source and scenario hashes. Acceptance requires public tests, reproduced progress, preserved passing scenarios and earlier repaired obligations, additional seeds, and deterministic repeated replays. A separate Sonnet request reviews the patch and its evidence; a completely passing matrix can proceed if review is unavailable. Partial fixes require review.

Accepted patches are installed into the actual workspace, checked again there, and rolled back if installation verification fails. Only service source is changed. Exact edits and Python compilation catch stale or malformed changes early. Model requests use conservative spend reservations and returned usage, while bounded subprocesses and a measured validation reserve control time. A cancellation flag lets the fixer stop local jobs and HTTP requests without executing incomplete model output.

## Results

Practice grading and exact-image results are pending. Public local checks do not establish hidden-seed coverage, private-test safety or the grader's reference-based goodput bound.

## What I would do next

I would grade the exact Linux image across all practice cases, examine missed bugs and regressions, and tune experiment selection using measured cost and latency. The next improvements would be stronger fault combinations for interacting bugs, tighter trace reduction around causal events, and broader retained probes for earlier repairs. Jev could cheaply filter document relevance; it is not implemented in the current fixer.
