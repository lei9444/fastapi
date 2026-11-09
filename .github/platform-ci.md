# Platform CI reference report

`platform-ci.yml` is an independent, single-run reference check of the same commit
on four free standard GitHub-hosted runners in this public fork:

| Platform | Runner label | Python architecture |
|---|---|---|
| Windows x64 | `windows-2025` | `x64` |
| Windows ARM64 | `windows-11-arm` | `arm64` |
| Linux x64 | `ubuntu-24.04` | `x64` |
| macOS ARM64 | `macos-15` | `arm64` |

It uses Python 3.13 and the committed dependency lock file. Both the setup
interpreter and the test environment are checked against the requested native
architecture, including the Windows interpreter build tag to reject x64
emulation. The report records the actual Python version, runner image, OS release,
interpreter architecture, CPU allocation, RAM, and xdist worker count. Uploaded
evidence comes only from hosted CI, never from local experiment files.

The test command remains `scripts/test-cov.sh`: the full `tests` and
`scripts/tests` suites with `-n auto --dist loadgroup` and coverage contexts.
The helper records xdist's physical-CPU-based automatic worker count explicitly.
No tests or assertions are removed, and the existing Test workflow, its dependency
matrix, benchmarks, and combined 100%-coverage gate are unchanged.

## Running and downloading

Pushes to the dedicated `lei9444-fastapi-platform-ci` branch trigger this workflow.
`workflow_dispatch` is also declared for future manual use, but GitHub only offers
manual dispatch once the workflow exists on the default branch. The initial run
does not require a default-branch change. No pull request or deployment is needed.
To repeat an existing run, use **Re-run all jobs** so all four platforms produce
evidence for the same attempt; old attempts are deliberately not mixed in.

Open the Actions run's **Summary** for the aggregate tables. Download
`platform-report-<attempt>` for `report.csv`, `report.json`, and `report.md`.
Each `platform-result-<platform>-<attempt>` artifact contains dependency/test logs,
metadata, JUnit XML, coverage JSON/XML, and the coverage database. Artifacts expire
after seven days. No environments or dependency caches are uploaded.

The aggregation job runs even after platform failures. Missing, malformed,
wrong-commit, and wrong-attempt evidence is reported as incomplete, never as a
zero-test success. Test failures keep their available counts and coverage. A
failed matrix job or incomplete report fails the workflow.

## Interpreting the reference results

Dependency time measures `uv sync --locked --no-dev --group tests --extra all`,
including downloads and package builds. Test time measures the test command,
including interpreter/xdist startup and coverage reporting. Neither includes
queueing, checkout, Python/uv setup actions, or artifact transfers.

Dependency caching is disabled, but hosted image/tool caches and network conditions
can differ. Hardware allocations and worker counts also differ. These are one-off
CI reference measurements, **not** the HOBL build-plus-test baseline or a controlled
OS/CPU architecture benchmark. There are no paid/larger runners, external
benchmark services, repeated benchmark batches, or endpoint-security changes.
