# Platform CI reference report

`platform-ci.yml` checks the same commit on four platforms in this public fork.
Each platform runs three repetitions in separate, freshly provisioned free
standard GitHub-hosted runner jobs: twelve test jobs in total.

| Platform | Runner label | Python architecture |
|---|---|---|
| Windows x64 | `windows-2025` | `x64` |
| Windows ARM64 | `windows-11-arm` | `arm64` |
| Linux x64 | `ubuntu-24.04` | `x64` |
| macOS ARM64 | `macos-15` | `arm64` |

It uses Python 3.13.17 and the committed dependency lock file. Both the setup
interpreter and the test environment are checked against the requested native
architecture, including the Windows interpreter build tag to reject x64
emulation. The report records the actual Python version, runner image, OS release,
interpreter architecture, CPU allocation, RAM, and xdist worker count. Uploaded
evidence comes only from hosted CI, never from local experiment files.

After dependency installation, each job separately builds both a wheel and an
sdist using `uv build --sdist --wheel --no-build-isolation --offline` and the
native environment's Python. The PDM backend version is pinned in
`platform-build-requirements.txt`, installed before the build timer starts, and
recorded in the report. The build performs no dependency downloads or isolated
environment setup. Both outputs must exist and be non-empty; they are retained
in the repetition's evidence artifact. Nothing is published to a package index.

The test command remains `scripts/test-cov.sh`: the full `tests` and
`scripts/tests` suites with `-n auto --dist loadgroup` and coverage contexts.
The helper records xdist's physical-CPU-based automatic worker count explicitly.
On Windows it selects Git Bash explicitly, rather than the WSL launcher.
No tests or assertions are removed, and the existing Test workflow, its dependency
matrix, benchmarks, and combined 100%-coverage gate are unchanged.

## Running and downloading

Pushes to the dedicated `lei9444-fastapi-platform-ci` branch trigger this workflow.
Changes confined to the separate [isolated build loop workflow](isolated-platform-ci.md)
and its dedicated helper, tests, requirements, and documentation are excluded.
Shared source/dependencies and this workflow itself still trigger these checks.
`workflow_dispatch` is also declared for future manual use, but GitHub only offers
manual dispatch once the workflow exists on the default branch. The initial run
does not require a default-branch change. No pull request or deployment is needed.
To repeat an existing run, use **Re-run all jobs** so all twelve jobs produce
evidence for the same attempt; old attempts are deliberately not mixed in.
The GitHub run attempt is separate from the repetition number (1, 2, or 3).

Open the Actions run's **Summary** for the aggregate tables. Download
`platform-report-<attempt>` for `report.csv` (four platform-average rows),
`runs.csv` (twelve individual-run rows), `report.json`, and `report.md`.
Each `platform-result-<platform>-<repetition>-<attempt>` artifact contains
dependency/test logs, metadata, JUnit XML, coverage JSON/XML, and the coverage
database, plus the build log, wheel, and sdist. Artifacts expire after seven days.
No environments or dependency caches are uploaded.

The aggregation job runs even after platform failures. Missing, malformed,
wrong-repetition, wrong-commit, and wrong-attempt evidence is reported as
incomplete, never as a zero-test success. Test failures keep their available
counts and coverage. A failed matrix job or incomplete report fails the workflow.

The report calculates arithmetic means of dependency installation time, build
time, test command time, and coverage for each platform. All three repetitions
must have valid evidence and pass before that platform's means are populated.
Otherwise, its means are left blank, the successful-run count is shown, and every
individual result remains available. Failed runs are never silently excluded
from an average.

## Interpreting the reference results

Dependency time is the sum of `uv sync --locked --no-dev --group tests --extra all`
and the pinned build-backend installation command times, including downloads and
the editable FastAPI install.

Build time measures the native Python wrapper and
`uv build --sdist --wheel --no-build-isolation --offline`, building both
distributions from the checkout with dependencies already installed. This is
an additional, explicit packaging operation, not a split of the editable
installation's time and not a native-code compilation benchmark.

Test time measures the test command, including interpreter/xdist startup and
coverage reporting. These timings exclude queueing, checkout, Python/uv setup
actions, and artifact transfers.

Dependency caching is disabled, but hosted image/tool caches and network conditions
can differ. Hardware allocations and worker counts also differ. Averaging three
repetitions is still a CI reference measurement, **not** the HOBL build-plus-test
baseline or a controlled OS/CPU architecture benchmark. There are no paid/larger
runners, external benchmark services, or endpoint-security changes.
