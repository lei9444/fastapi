# Windows ARM64 storage and httptools reference batch

This bounded experiment runs three independent free `windows-11-arm` jobs for
each condition: **A default/httptools 0.7.1**, **B default/0.9.0**, and
**C ReFS Dev Drive/0.9.0**, plus one aggregation job. Each prepares once and runs
one original isolated build and complete test workload. There are no retries,
additional platforms, new warm-ups, HOBL upgrades, or coverage optimization patches.

The baseline is the existing FastAPI 0.162.2 experiment, Python 3.13.17,
uv 0.13.5, build 1.6.1, pip 25.0.1 and PDM 2.5.0. The original lock is preserved
in Git. Condition A replaces only its httptools block in the disposable checkout
with the exact package-manager-generated block from commit
`176b4ea88550a68a82c0a7fa06605e1b670950d9`. All other entries, including alternate
resolution entries, and lock metadata must match. Runtime versions excluding
httptools, source trees, wheelhouse, actual workers and test identities are compared.
Checkout and canonical lock hashes are recorded separately; all conditions use
the same LF normalization before preparation, independent of Git's CRLF checkout.

Default controls do **not** invoke `storage_ab.ps1` or the native adapter, disk
discovery, migration, legacy repeated volume checks or protection queries.
They only create fresh empty uv/pip caches, TEMP/TMP and an offline backend
wheelhouse beside the existing checkout. The small cache-configuration timer is
separate. Treatment alone reuses the unchanged guarded RAW unused NVMe logic,
copy verification, junction, retained backup and postcheck. No local/self-hosted
disk or security configuration is touched, and unavailable treatment fails
explicitly without fallback.

Every condition has the same preparation timer: original locked uv sync/new venv,
build tools, offline backend wheelhouse, original build audit and one common
installation/path audit. This includes actual source-build or prebuilt-wheel
selection, WHEEL/INSTALLER/uv build marker, ARM64 PE headers and installed RECORD
hashes. The common Python audit reads effective cache/volume paths; it does not
perform disk discovery or provision storage. Audits are not claimed to be free.

The existing build helpers retain two isolated venv+pip environments and a wheel
built from the sdist. The unchanged test helper retains Git for Windows Bash,
pytest-xdist's physical-CPU worker policy, `tests` plus `scripts/tests`, 20-second
timeout, coverage contexts and JSON/XML/terminal reports. Synthetic checks for
this helper live under `scripts/experiments`, outside that measured test selection.

Report all nine individual samples and arithmetic means; never remove slow or
failed samples. A complete group of three successful, consistent samples is
required for a mean. Storage processing is separate, including creation,
migration and treatment verification. Prep+build+test excludes storage and
cache configuration. Full-job time comes from GitHub job start/end timestamps
and includes setup, storage, audit, artifact and post-step overhead in that
interval; it excludes time queued before job start. Phase gains and whole-job
gains are different. Default protection state is unprobed, not inferred from
treatment metadata.

Existing caches are neither used nor erased. OS page caches are not flushed;
the populated treatment copy may warm them. Hosted machines can differ. These
are reference measurements, not a universal OS/CPU or production saving.
The historical 52.928-second preparation in run 42565069560 preceded three
same-runner loops and was not a preparation mean. Run 42752473286 is also a
historical reference, not a simultaneous control for this batch.

Execution temporarily uses the already registered `storage-ab.yml` manual entry,
with `[skip ci]` measurement/restore commits to avoid unrelated push matrices.
The reproducible template is `.github/storage-threeway-workflow.yml`, outside
the auto-discovered workflows directory; synthetic checks use that template.
Restore its exact original content immediately after the batch, before offline
analysis. Do not enable/disable workflows, change master, or create a PR.
Per-trial artifacts and the aggregate have seven-day GitHub retention; preserve
the downloaded reports/evidence in the session for durability.
