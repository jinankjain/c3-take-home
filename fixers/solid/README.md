# Solid fixer

One Sonnet 4.6 tool-use agent, driven by a plain-Python orchestrator that owns the clock, the model
budget and the last accepted `src/`. The model proposes edits; deterministic code decides what is kept.

## Flow

1. **Bootstrap (no model):** public tests, then a baseline sweep of the public scenario plus every
   fault file over the public seeds and 15 more. Builds the cached context: case docs, all of `src/`,
   the service and CURRENT platform docs from the corpus, the simulator's `expect` checker, and the
   log event names seen in the evidence logs.
2. **Agent loop** (`agent.py`): streamed Anthropic-format calls through the C3 proxy with prompt caching
   and a 4K thinking budget. The agent first maps every ticket item to a run that fails on the original
   code, writing its own scenarios (`write_scenario`) when nothing reproduces a symptom, then fixes.
3. **Acceptance check** (`simgate.py`, on `checkpoint()`): compiles, public tests pass, deterministic
   replays, no run that was clean at baseline now fails, messages <= 2x baseline, and fewer failures
   than the last accepted state, including on the agent's own scenarios. Only accepted code is kept.
4. **Coverage review and verification:** the first `done()` asks for an item-by-item coverage map; then
   a sweep on unseen seeds sends the agent back if anything still fails.
5. **Keeping partial progress:** when time runs short and `src/` holds unchecked edits, the orchestrator
   checkpoints them itself. At the end, unchecked edits get the full check if it fits, else a quick check
   (compile, public tests, determinism, public seeds better than the last accepted state), else they are
   reverted. SIGTERM restores the last accepted `src/`.

| File | Role |
| --- | --- |
| `fixer/main.py` | Orchestrator: phases, time/budget, snapshots, coverage review, verification |
| `fixer/agent.py` | Tool-use loop, cache breakpoints, history trimming, error tolerance |
| `fixer/tools.py` | read_file, grep, edit, run_sim, trace_query, write_scenario, run_tests, checkpoint, revert, done |
| `fixer/simgate.py` | Simulator/pytest wrappers and the acceptance check |
| `fixer/context.py` | Cached context and the agent's instructions |
| `fixer/llm.py` | Streaming proxy client (idle + overall timeouts), scripted client for tests |

## Run on the practice cases (needs internet access to OpenRouter)

```sh
python3 -m venv .venv && .venv/bin/pip install pyyaml pytest
export PATH=$PWD/.venv/bin:$PATH
python3 proxy/server.py &                                         # reads OPENROUTER_API_KEY from .env
./grade.sh --fixer fixers/solid --cases practice/B2 --proxy http  # one case, ~$1-2
./grade.sh --fixer fixers/solid --proxy http                      # all six
grep -E "turn [0-9]+:" results/*/B2/fixer.log                     # per-turn latency and cache use
```

Image, exactly as graded: `docker build --platform linux/amd64 -t c3-solid fixers/solid && ./grade.sh --fixer c3-solid`.

## Offline tests (no model, no network)

```sh
python -m pytest -q fixers/solid/testdata/test_llm_stream.py      # stream parser, idle vs slow calls
python fixers/solid/testdata/run_scripted.py A1 A1-fix            # scripted agent, graded by the real grader
```

Scripts in `testdata/scripts/` cover the happy path, bad edits, test-breaking patches, unchecked
`done()`, budget exhaustion, model errors, broken scenarios, the coverage-review cap, the scenario
workflow and the safety checkpoint (`A1-no-checkpoint`, run with `--deadline-s 140`).
`test_end_of_run.py` checks the end-of-run quick check keeps a real partial fix and rolls back
test-breaking or no-progress edits. `testdata/claude_p_shim.py` is a
local stand-in upstream for the proxy backed by the `claude -p` CLI (testing only; slower and noisier
than the real API).
