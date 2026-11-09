# Guarded Windows ARM64 storage A/B

This is a dedicated experiment, not a change to normal CI storage. It runs three
fresh free standard `windows-11-arm` jobs for each of two requested layouts:
the image's default workspace volume and a newly allocated ReFS Dev Drive.
There are **six workload jobs and one report job**. Each job prepares dependencies
once, then performs **one** existing HOBL-style isolated build and full test run.
The normal four-platform workflows and their `loops=3` behavior are unchanged.

## Safety boundary

The workflow and the production script both restrict execution to GitHub-hosted
Windows ARM64, and the script checks actual OS, PowerShell process, and Python
architecture. The repository and run identity are checked too. **Do not execute
the native adapter on a local or self-hosted machine.** Local validation only
parses native scripts and imports the effect-free core with fully mocked
operation callbacks. Missing mock operations throw; there is no native fallback.
Hosted lint/type checks also finish before any native storage action, using
a separate fresh preflight venv and cache outside the checkout. Those checks cannot
seed the later workload caches; their cost remains in full-job wall time.

The control group never initializes a disk or moves the workspace. A treatment
requires exactly one online, writable, non-boot/non-system NVMe disk that is RAW,
has zero partitions, is absent from the partition inventory, is at least 50 GiB,
and has a stable private identity for rechecks. Unknown properties, zero/multiple
candidates, or changed eligibility do not authorize a guess. No existing volume
is repurposed, no partition is deleted, and there is no Clear-Disk or VHDX fallback.

The candidate is checked again before initialization and later operations. Windows
assigns an available letter to the new partition; preexisting letters and the
partition's identity are rechecked. Only that newly created partition object is
passed to `Format-Volume -DevDrive -FileSystem ReFS`, without `-Force`, `-Full`,
or filesystem tuning overrides. ReFS is verified with volume metadata and Dev
Drive status with Python's Windows `os.path.isdevdrive`, not inferred from its name.

The workspace must be an ordinary checkout directory, not a reparse point or a
protected runner/toolcache/temp ancestor, and must not contain a preexisting
workload venv. The storage shell starts in the unchanged `RUNNER_TEMP`, outside
the checkout, so its inherited working-directory handles do not block renaming.
The populated checkout is copied first with hidden/read-only
entries, empty directories, symbolic links and junctions preserved. Unsupported
reparse/hardlink topologies fail rather than being silently flattened. Source and
destination manifests verify content hashes, attributes, links, counts and bytes,
and ensure the source did not change during copying.

Only after verification is the original renamed to a sibling backup and its
logical location replaced by a junction. The backup is retained for the job's
lifetime. There is no recursive removal of the workspace or backup. If cutover
fails, recovery never overwrites an unexpected existing entry; where safe it
copies the retained backup back to the original location, without consuming the
backup or deleting the new-volume copy. A failed operation is not a valid ReFS
trial even when recovery succeeds.

The running Actions agent, base Python, Visual Studio, hosted toolcache,
`GITHUB_WORKSPACE`, and `RUNNER_TEMP` are not moved or reassigned. No Defender
preference/exclusion, protection, filter, trust, or tamper setting is changed.
Real-time protection is read from `Get-MpComputerStatus`; `PerformanceModeStatus`
is read from `Get-MpPreference` before/after. This is the configured setting, not
proof that asynchronous scanning occurred on a particular volume. Status
`0` means Enabled and `1` means Disabled. Missing protection metadata or an
unexpected real-time-protection change is reported, not guessed away.

## Workload and cache controls

Both groups receive fresh per-trial uv/pip caches, TEMP/TMP and a backend
wheelhouse on their workspace's actual physical volume, outside the source tree.
This intentionally controls dependency-cache warmth: existing image caches are
neither copied nor cleared. Initial directory contents and actual effective
`uv cache dir` / `pip cache dir` paths are recorded and verified. Commands run
from the physical workspace while the runner's logical workspace stays intact.
Filesystem page caches are not flushed; populated-workspace copying may warm
them, which is part of this workflow-level intervention.

Versions remain native Python **3.13.17**, native uv **0.13.5**, build **1.6.1**,
pip **25.0.1**, and pdm-backend **2.5.0**, with the same commit and `uv.lock`.
The locked httptools **0.7.1** is not upgraded. `uv sync` still permits its
Windows ARM64 source build; binary-only preparation applies only to the backend
wheelhouse. Such compilation and storage costs can coexist.

The existing isolated build/test logic is reused with `loops=1`. Build uses the
native workload Python's default `python -m build --outdir ...`, offline backend
installation, two isolated venv+pip environments, sdist then wheel-from-sdist,
and normal cleanup. The same pytest-xdist/coverage scripts and safe Git Bash
selection remain in use. No extra build/test warm-up is added.

## Measurements and interpretation

Every trial records requested/applied layout separately, eligibility/capacity
summaries, copy/backup verification, physical workspace/venv/cache/temp paths and
filesystems, Dev Drive/protection state, initial cache state, exact inputs, and
all command outcomes. Serial numbers, unique disk IDs, complete environments,
local machine data, venvs, caches and workspace backups are not published.
JUnit hostname attributes are removed from the staged artifact copy.

Storage inspection, new-volume creation, populated-copy migration, path
configuration and post-preparation checks are separately timed. Their measured
total is separate from dependency preparation, isolated build and tests.
GitHub job wall time is independently read as **completed_at minus started_at**
for each workload job. It includes provisioning/checkout/setup/checks, migration,
artifact staging/upload and post steps within that interval; queue time before
started_at is excluded. It is not reconstructed by adding payload timers.

All six samples and failures remain visible. Missing/unapplied/failed ReFS trials
are not counted as default-layout successes or usable ReFS measurements. Group
means require all three valid successful trials; gains require both complete
groups and consistent versions, lock fingerprint and backend wheelhouse. Only
arithmetic means are used. A preparation win that disappears after migration
cost is included must not be presented as a full-job win.

The report is in the Actions Summary and `storage-ab-report-<attempt>`:
`report.csv` contains group means, `trials.csv` all six rows, and JSON retains
comparison and full storage metadata. Each `storage-ab-<layout>-<trial>-<attempt>`
artifact retains storage evidence plus setup/build/test logs, wheel/sdist, JUnit
and coverage. All artifacts expire after seven days.

Push triggers are limited to this experiment's exact files on the current
feature branch. Manual dispatch is declared for later use once the workflow
exists on the default branch. The existing twelve-job workflow excludes only
these dedicated paths; shared source, lock files and its own workflow still
trigger its existing checks. No workflow is enabled/disabled to run this experiment.

Three samples on variable hosted hardware do not establish a universal gain.
Default enablement and merging to `master` require a separate user decision.

## References

- [actions/runner-images#14878](https://github.com/actions/runner-images/issues/14878)
  remains an image-level request, not proof that the images are fixed.
- [Devolutions/IronRDP#2186](https://github.com/Devolutions/IronRDP/pull/2186)
  is a merged Rust workload optimization. Its reported benefit is not a Python
  prediction, and its performance samples used an empty-workspace path.

This Python-specific guarded implementation was written independently; it does
not copy the reference PR's script or move Cargo/agent paths.
