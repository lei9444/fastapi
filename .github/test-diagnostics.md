# One-shot Windows/Linux test diagnostics

This opt-in experiment runs exactly two free standard workload jobs:
`windows-2025` and `ubuntu-24.04`, native Python 3.13.17, uv 0.13.5, two xdist
workers, and one complete existing test command per job. It does not build
distributions, prepare ReFS, change dependencies, skip tests, disable coverage or
timeouts, or change protection settings. Dependency preparation is a prerequisite,
not part of the test timer. No extra report job or repeated samples are added.

The workflow is manual-only. For the initial feature-branch execution, the known
registered `storage-ab.yml` dispatch entry temporarily contains this diagnostic
definition. Publication uses `[skip ci]` commits, which suppress push/PR workflows
but not the one explicit workflow dispatch. The original storage workflow is
restored afterward, without dispatching it. No workflow or account setting is
enabled/disabled. The standalone `test-diagnostics.yml` remains available for
future explicitly authorized use; no default-branch deployment is implied.

## What is observed

The wrapper retains `uv run --no-sync` -> native Python -> Git Bash/system Bash
-> the unchanged `test-cov.sh`/`test.sh` -> pytest/xdist/coverage command chain.
Only worker selection is explicitly fixed at two. Full JUnit IDs, outcomes and
skip/xfail reasons must match the corresponding three consistent baseline
manifests from commit `4d047839d`, run `42752473021`.

A temporary `.pth` file in the hosted workload venv activates diagnostics only
when `FASTAPI_DIAG_ACTIVE=1`. It records the earliest observable `.pth` point,
not OS process creation or the first Python instruction. It installs:

- A pytest plugin for process/worker initialization, collection, item protocol,
  setup/call/teardown, worker completion and session boundaries.
- Timing wrappers around actual pytest-cov controller finish/summary methods and
  coverage start/stop/save/combine/report/exit methods, including the actual
  tracer class selected at start. Buffered console report timestamps are not
  used as substitutes. Controller methods are attached after pytest registers
  its real plugin, since assertion rewriting can bypass normal import finders.
- Timing wrappers around the unchanged pytest-timeout set/cancel hooks. The
  Windows cancel hook includes its real timer cancel/join; Linux retains signals.
  These are registered pluggy wrappers, not replacements of the timeout plugin.
- A narrowly matched wrapper of the existing telemetry `subprocess.run` call.
  The original command, environment filtering, `PYDANTIC_DISABLE_PLUGINS`,
  capture settings, 15-second child timeout, return value and exceptions remain
  intact. Only diagnostic correlation environment variables are added.
- In the child, selective wrappers around the existing target `import_module`
  and `inspect.unwrap` result measure target-module import and the original
  function call without replacing the process or SDK isolation. Imports inside
  the function remain part of that combined function interval. Outermost builtin
  import calls in that thread are timed separately (including cache hits), with
  nested imports suppressed; other-thread imports are not exhaustively traced.

The 43 common decorated cases are retained; Linux also has its pre-existing
POSIX fork lifecycle case. Forked trace state is reset rather than duplicating
the parent's buffer. Coverage's `_exit` behavior runs before the final
diagnostic flush when a process exits that way.

Every process buffers records in memory and writes one JSONL payload plus one
write-completion footer at exit. No file is written per item or hook.
`monotonic_ns` is comparable across processes on the same host only. CPU counts
are per-process (including threads, excluding children). Basic process I/O
counters have OS-dependent semantics and are not uniquely disk operations;
wall minus CPU is not a measurement of disk or Defender waiting.

Instrumentation adds startup imports, Python wrappers, clock reads, in-memory
events, a pytest plugin, process-counter snapshots and a final serialization/
write. Child startup, shutdown and protocol residuals can contain that overhead.
Do not subtract historical uninstrumented means to claim a measured probe cost.

## Interpretation and failure handling

The analyzer partitions outer wall time into consecutive observed boundaries:
launch to first item, first to last item, last item to last worker coverage
finish, then master finish, reporting finish, and outer return. Worker activity
is reconstructed from intervals, not JUnit sum divided by two. Nested coverage,
test and child times are retained but must not be added as independent tools.
Child exit residual includes SDK/interpreter shutdown, recorded coverage,
diagnostic output, pipe handling and process teardown; unmatched time is not
silently assigned to a specific cause.
In pytest-cov 7.1.0, `Coverage.report` runs once to obtain the total and again
for the requested terminal report. Their combined time is not terminal printing
alone; XML and JSON reports are separate calls. No report or gate is removed.

Missing/incomplete traces, mismatched worker count, missing correlated children,
changed JUnit outcomes, and inconsistent monotonic boundaries are errors even
when pytest succeeds. Preserve failed artifacts. A failed measurement does not
authorize another run or a larger matrix.

The initial diagnostic run `42840865672` passed both complete test manifests but
failed trace verification: assertion-rewritten plugin imports bypassed the first
import-based timeout/controller wrappers. Its underlying coverage method,
worker/item and child traces remain evidence, but missing hooks are not zero
cost and that run is not a complete successful diagnostic. The corrected
registration above is covered by synthetic tests; another hosted measurement
requires explicit approval rather than an automatic rerun.

`scripts/diagnostics/verify_synthetic.py` uses only mocks and synthetic timelines.
It lives outside `tests` and `scripts/tests` and is not added to the measured
test collection. Local validation must never run the full diagnostic workload.
Raw events, JUnit, coverage, logs, manifests, per-job CSVs and reports are retained
for seven days. No complete environment, hostname or machine identifier is
intentionally recorded in the diagnostic events.
