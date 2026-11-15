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

**Results.** Under the real grading setup, the Docker image fixed **12/12 practice bugs in one run**
(2,333 s of fixer time, $4.13) and **35/36 over three more runs** (average 407 s and $0.69 per case).
Every miss on that image was B2, the hardest case, which went 3/3 in five of six runs.
The first full run scored 6/12, and every miss came from slow or stalled model calls; the timeout and
retry changes fixed those. B2's misses had two causes. Its bug-2 symptom (spurious re-queues) breaks no
invariant, so only a scenario that counts the event can show it, and bug 2 needs two fixes, the first of
which makes every built-in scenario pass. The agent sometimes accepted weaker evidence and stopped with a
half-fix. In the last such miss it counted a field value as if it were a log event, a check that can never
fail, and the scenario tool called a scenario that failed for other bugs a reproduction (see below). Other runs
ran out of time after a long thinking turn was cut and the retry redid the same analysis; the retry now
carries the cut reasoning over (retry times fell from 77–93 s to a median under 40 s). Real runs also
exposed two tool bugs (grep-style `\|` patterns, and treating broken scenarios as reproductions), and the
offline tests now cover both. The practice score is a guide only: I tuned against these cases, and the
hidden codebases are new.

**What I'd do next.**
- Generate stress scenarios systematically by varying fault parameters around the ticket, rather than
  relying on the agent's choices.
- Run one agent per ticket item in parallel, with merge handling for bugs that touch the same files.
- Retrieve corpus documents on demand when service docs are too large to include.
- Use Jev for cheap yes/no checks, such as "does this patch disable functionality?", to reduce the
  risk from private regression tests.
- Cut over-long thinking earlier without losing good long turns: each cutoff still costs about 75 s.
- Make the scenario tool judge a scenario by its own counting check and show per-seed event counts on the
  original and current code, so a half-fix is visible ("5,4,8 before, 5,4,8 after"). I prototyped this
  with a 50 s thinking cutoff; over 10 runs it scored no better and ran slower, so it isn't in the image.
- Accept the agent's common scenario mistakes (a `base:` key in the YAML, a `scenarios/` prefix on
  fault paths) instead of rejecting them, saving one turn per case.
