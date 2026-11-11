# One-shot Windows/macOS ARM implementation diagnostics

This opt-in experiment runs exactly two free standard workload jobs:
`windows-11-arm` and `macos-15`, native ARM64 Python 3.13.17, uv 0.13.5, two xdist
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
Only worker selection is explicitly fixed at two. This is a mechanism comparison,
not a hardware-matched performance ranking. Full JUnit IDs, outcomes and
skip/xfail reasons must match the corresponding three consistent baseline
manifests from the corresponding ARM platform at commit `4d047839d`, run
`42752473021` (also checked against `42752473280`). Per-file coverage fingerprints
must match a retained same-platform baseline. The original Mac runs have two
known fingerprints differing only at `test_debugging/test_tutorial001.py:36`;
both exact historical fingerprints are recorded, not an arbitrary tolerance.

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
  Windows cancel hook includes its real timer cancel/join; macOS retains signals.
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

The 43 common decorated cases are retained; macOS also has its pre-existing
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

## ARM implementation observations

The ARM pair adds only aggregate instrumentation to the existing trace:

- Coverage 7.13.3's actual `SqliteDb._connect`/`close`, schema `_init_db`,
  `numbits_to_nums`, `PythonParser.parse_source` and `abs_file` calls. Counters
  retain calls, exceptions, inclusive wall/process CPU and work units. Connect
  entries distinguish a missing connection (new connection plus its normal
  initialization) from an already-connected no-op; close entries distinguish
  a live connection from a no-op/retained memory connection. These are named
  library operations, not a count of arbitrary OS calls or physical disk I/O.
- Real `Starlette._TestClientTransport.handle_request` calls and actual AnyIO
  portal dispatches with TestClient HTTP ASGI receive/send functions, websocket
  session and lifespan callables. The existing portal token supplies the
  backend class, native loop/token type, and exposed selector/proactor type.
  No extra sample loop is created. Calls outside these observed entrypoints,
  such as a separate direct AsyncClient transport, are not inferred.

Counters use per-thread in-memory aggregation and are grouped by their nearest
observed coverage method/report purpose. They do not write per hook. Calls may
nest (for example parser/path/SQLite inside a report), so their inclusive times
must not be added as exclusive costs. Bitmap input/output units are bytes and
decoded line numbers; parser input units are source characters. A missing row
means zero only when that hook was verifiably installed before the relevant
observed scope; otherwise it is unknown. Counter clock/aggregation overhead is
itself included in enclosing timings.

After the full test timer stops and the temporary activation file is removed,
the native venv records metadata in `implementation.json`: compiler/build flags,
SQLite version/compile options, actual Bash path/version, Python/uv and selected
extension PE/Mach-O architectures, installed package versions, CPU/resource
description, image and filesystem. These post-test imports/probes are not evidence
that a package was used by TestClient, and are not another performance sample.
The actual launch trace identifies the Bash used by the timed invocation.
Only allowlisted metadata is retained; no serial numbers, UUIDs, hostnames or
complete environment/configuration dump is collected.

macOS lacks `psutil.Process.io_counters` on some versions; the trace records an
explicit unavailable reason rather than substituting zeros or Linux semantics.
Universal Mach-O extensions are valid when they include ARM64 and the running
interpreter is independently verified as native ARM64. Missing private selector/
proactor attributes are reported as `not_exposed`, not guessed.

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
The corrective diagnostic records the original bound arguments and returned
coverage percentage of each `Coverage.report` call, distinguishing `total_only`
from `term_missing_skip_covered`. Both calls still execute unchanged, and their
individual wall/CPU intervals and counts are retained. The validator requires
both purposes in order with equal returned totals, actual timeout set/cancel
events for every item, and the real pytest-cov finish/summary envelopes.

Missing/incomplete traces, mismatched worker count, missing correlated children,
changed JUnit outcomes, and inconsistent monotonic boundaries are errors even
when pytest succeeds. Preserve failed artifacts. A failed measurement does not
authorize another run or a larger matrix.

The initial diagnostic run `42840865672` passed both complete test manifests but
failed trace verification: assertion-rewritten plugin imports bypassed the first
import-based timeout/controller wrappers. Its underlying coverage method,
worker/item and child traces remain evidence, but missing hooks are not zero
cost and that run is not a complete successful diagnostic. The user separately
authorized one corrective x64 run (`42942435889`), which completed with verified
hooks. The subsequently authorized Windows ARM64/macOS ARM64 pair has its own
run/evidence directory and is not pooled with either x64 sample. A failure in
this ARM pair does not authorize another run. No total-only report reuse,
dependency upgrade, protection change or other optimization is applied here.

`scripts/diagnostics/verify_synthetic.py` uses only mocks and synthetic timelines.
It lives outside `tests` and `scripts/tests` and is not added to the measured
test collection. Local validation must never run the full diagnostic workload.
Raw events, JUnit, coverage, logs, manifests, per-job CSVs and reports are retained
for seven days. No complete environment, hostname or machine identifier is
intentionally recorded in the diagnostic events.
