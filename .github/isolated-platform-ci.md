# Isolated build loop reference report

`isolated-platform-ci.yml` adds a separate HOBL-style build/loop comparison. It
has **four workload jobs**, one each on `windows-2025` (x64), `windows-11-arm`
(ARM64), `ubuntu-24.04` (x64), and `macos-15` (ARM64), plus **one report job**.
All are free standard GitHub-hosted runners for this public repository.

The existing `platform-ci.yml` retains its twelve independent-runner measurements
and non-isolated `uv build` packaging semantics. This workflow does not replace it.

## Loops and timing

Each workload job prepares one native Python 3.13.17 venv, test dependencies,
build tools, and an offline backend wheelhouse. Preparation is timed **once** and
reported as a separate value, not averaged or divided among the loops.

The same runner and workload venv then execute `loops` sequential build/test
pairs. Each command launches a fresh process. All loops, including the first,
are retained; there is no hidden build/test warm-up or first-loop exclusion.
Filesystem/tool caches can warm naturally between loops.
Runner queueing, checkout, Python/uv setup actions, helper quality checks, and
artifact transfers are outside the measured preparation/build/test phases.

Pushes use `loops=3`. The `workflow_dispatch` input is a positive integer with a
default of `3`; for example, `2` runs two pairs on each of the four runners.
Empty, zero, negative, fractional, and nonnumeric manual values fail explicitly.
There is no added fixed numerical cap. The existing 30-minute workload-job
timeout remains a limit on how much work can finish. Missing loops make the
report incomplete, including after cancellation or runner loss.

The workflow file must be present on the default branch before GitHub offers
manual **Run workflow** dispatch. This feature-branch submission is tested through
its push trigger; it does not change `master` to expose that button.

## Isolated build contract

`isolated-build-requirements.txt` pins:

- `build==1.6.1`
- `pip==25.0.1`
- `pdm-backend==2.5.0`

The workload venv explicitly receives pip; uv-created venvs need not contain it.
A pre-loop audit verifies the native interpreter, package versions, that pip is
inside the workload venv, and build 1.6.1's actual installer/environment selection.
The audit only inspects selectors: it does not create an isolated build env or
perform a warm-up build.

With valid vendored outer pip, [build 1.6.1's pip backend](https://github.com/pypa/build/blob/1.6.1/src/build/env.py)
selects `venv+pip`, creates build venvs with `with_pip=False`, and uses the outer
`python -m pip --python <isolated-python>` to install the backend. It does not
silently select uv or add ensurepip seeding.

Each timed build runs the native workload venv's
`python -m build --outdir <loop-specific-directory>`. No `--sdist`, `--wheel`,
`--no-isolation`, retained-environment, or retained-source flags replace its
default flow. The timer includes creating the isolated environments, installing
the backend, producing the sdist, extracting it and building the wheel from it,
and normal process/context cleanup.

The backend wheel and its dependencies are downloaded before the loop timer.
During builds, `PIP_NO_INDEX=1` and `PIP_FIND_LINKS` select only that local
wheelhouse. The binary-only download constraint applies **only to this backend
wheelhouse**, not to `uv sync` and the application's locked test dependencies.
For example, the locked httptools 0.7.1 can still be source-built during Windows
ARM64 preparation; that is separate from each loop's FastAPI distribution build.
The lock file is not upgraded.

The evidence must show two `venv+pip` creations, the pinned backend installed in
both environments, `Building wheel from sdist`, and nonempty wheel/sdist outputs.
Unexpected installer behavior or missing evidence fails the result.

Tests reuse `scripts/test-cov.sh` and `scripts/test.sh`, including Git Bash on
Windows, `pytest -n auto --dist loadgroup`, and coverage contexts. They are not
replaced with HOBL's serial test collection.

Only the build method and loop structure are aligned. Current checkout source
and Python 3.13.17 are **not fully matched conditions** with the older local
FastAPI 0.119.1 / Python 3.12.10 measurements. No local HOBL files are read or
uploaded, and no endpoint-security or billing settings are changed.

## Reports and failures

The Actions Summary contains four platform summaries and all observed loops.
Means are populated only when preparation and every requested loop succeed
with complete evidence. Failed loops remain visible, subsequent loops still
run, and missing indices are reported explicitly. Large missing ranges are
summarized without allocating a row for every nonexistent loop.

Download `isolated-report-<attempt>` for:

- `report.csv`: four summaries with one preparation duration and build/test means.
- `runs.csv`: individual-loop outcomes and timings, without repeated setup columns.
- `report.json` and `report.md`: the same data and measurement definitions.

Each `isolated-result-<platform>-<attempt>` contains setup logs and `audit.json`,
plus separate `loop-<number>` directories with result metadata, build/test logs,
wheel/sdist, JUnit, and coverage. The venv and downloaded wheelhouse are not
uploaded; the audit retains the wheelhouse filenames and hashes. All artifacts
expire after seven days. Use **Re-run all jobs** for a fresh complete attempt;
old attempts are not mixed into its means.

The shared result schema uses `repetition=0` in `setup.json` only for preparation
metadata, never a build/test warm-up. In `runs.csv`, `repetition` is the one-based
sequential loop index; preparation columns are omitted.
