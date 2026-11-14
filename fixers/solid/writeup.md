# C3 take-home: automated fixer — write-up

**How it works.** The fixer is one Claude Sonnet 4.6 tool-use agent inside a plain-Python orchestrator.
The orchestrator owns the clock, the budget and the last accepted copy of `src/`. The model proposes
changes; deterministic code decides what is kept.

1. **Baseline (no model):** run the public tests, then replay the public scenario plus every fault file
   over 20 seeds and record which invariants and `expect` checks fail.
2. **Cached context:** the case docs, all of `src/`, the service docs and CURRENT platform docs from the
   corpus, the simulator's `expect` checker, and the log event names from the evidence logs.
3. **Coverage first:** before editing, the agent maps each ticket item to a run that fails on the
   original code. Where none exists, it writes a scenario whose `expect` block counts the symptom. The
   tool immediately runs that scenario against the original code to show whether it reproduces.
4. **Fix, then checkpoint.** The agent works with read, grep, edit, replay, trace-query and test tools.
   A change is accepted only if it compiles, the tests pass, replays stay deterministic, no previously
   clean run starts failing, message counts stay bounded, and failures go down, including on the
   agent's own scenarios. A brief coverage review and a sweep on unseen seeds follow. When time runs
   short, the orchestrator checkpoints unchecked edits itself, and at the end a quick check (tests +
   public seeds) can still keep a partial fix; otherwise the last accepted code is restored.
5. **Robustness:** streamed model calls with separate idle and overall timeouts, bounded retries, and
   continuing after errors, so a single bad call can't end a case.

**Results.** Under the real grading setup the Docker image fixed **12/12 practice bugs in a single
run** (2,571 s of fixer time, $4.80, every case at 13–20% of its budget), and 33/36 across three runs.
The one miss was a B2 run where the agent had fixed two bugs but was still working on the third when
time ran short, and the fixer discarded the unchecked fix; that led to the safety checkpoint and quick
check above.
The first full run scored 6/12, and every miss came from slow or stalled model calls; the timeout and
retry changes fixed those. B2's bug-2 was the hardest: its symptom (spurious GPU cancels) breaks no
invariant, so it only became fixable once the agent wrote a scenario that counted it. That scenario
also caught the agent's first, wrong fix. Real runs exposed two tool bugs (grep-style `\|` patterns,
and treating broken scenarios as reproductions), and the offline tests now cover both. The practice
score is a guide only: the hidden codebases are new.

**What I'd do next.**
- Generate stress scenarios systematically by varying fault parameters around the ticket, rather than
  relying on the agent's choices.
- Run one agent per ticket item in parallel, with merge handling for bugs that touch the same files.
- Retrieve corpus documents on demand when service docs are too large to include.
- Use Jev for cheap yes/no checks, such as "does this patch disable functionality?", to reduce the
  risk from private regression tests.
- Trim long analysis turns, which occasionally hit the output limit.
