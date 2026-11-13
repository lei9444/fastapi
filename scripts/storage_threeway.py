"""One bounded, contemporaneous hosted ARM64 storage/httptools comparison."""

import argparse
import csv
import ctypes
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from importlib.metadata import distributions
from pathlib import Path
from statistics import mean

import tomllib

from scripts import isolated_platform_ci as isolated
from scripts import platform_ci as common
from scripts import storage_ab as storage

CONDITIONS = {
    "A-default-0.7.1": ("default", "0.7.1"),
    "B-default-0.9.0": ("default", "0.9.0"),
    "C-devdrive-0.9.0": ("refs-devdrive", "0.9.0"),
}
LOCK_SHA = "6181ddfcce81fd63c6d18ecc43f608d2f96aa065973bb1d79964768a37dcf675"
OLD_LOCK_SHA = "5958d131f3f2853128009ea8596e27aa25091c99d9183ceb3083e00e61b8bef1"
OLD_ENTRY_SHA = "deb28c52b54fbeabd21cc84d7c851afe5d94c14a4bc7a4d6b37381c8e0e8e92c"
EXPECTED_FASTAPI = "0.162.2"
METRICS = (
    "storage_total_seconds",
    "cache_configuration_seconds",
    "setup_seconds",
    "build_seconds",
    "test_seconds",
    "measured_phase_seconds",
    "job_wall_seconds",
)
SHARED_FIELDS = (
    "common_input_sha256",
    "installed_common_sha256",
    "wheelhouse_sha256",
    "uv_version",
    "workers",
    "test_manifest_sha256",
)


@dataclass
class Trial(storage.Trial):
    condition: str = ""
    cache_configuration_seconds: float | None = None
    measured_phase_seconds: float | None = None
    workers: int | None = None
    common_input_sha256: str = ""
    installed_common_sha256: str = ""
    test_manifest_sha256: str = ""


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fingerprint(value: object) -> str:
    return sha(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def identity() -> dict:
    condition = os.environ["THREEWAY_CONDITION"]
    raw_trial = os.environ["STORAGE_TRIAL"]
    if condition not in CONDITIONS or raw_trial not in {"1", "2", "3"}:
        raise ValueError(
            "Unexpected condition/trial; only the approved 3 by 3 batch is supported."
        )
    layout, version = CONDITIONS[condition]
    if os.environ["STORAGE_LAYOUT"] != layout:
        raise ValueError("Condition and storage layout disagree.")
    return {
        "condition": condition,
        "layout": layout,
        "httptools": version,
        "trial": int(raw_trial),
        "commit": os.environ["GITHUB_SHA"],
        "run_id": os.environ["GITHUB_RUN_ID"],
        "run_attempt": os.environ["GITHUB_RUN_ATTEMPT"],
    }


def require_host() -> None:
    storage.require_hosted_arm()
    if (
        os.environ.get("GITHUB_REPOSITORY") != "lei9444/fastapi"
        or os.environ.get("STORAGE_RUNNER_ENVIRONMENT") != "github-hosted"
        or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or os.environ.get("LOOPS") != "1"
    ):
        raise ValueError(
            "This entrypoint only accepts the fork's explicit hosted manual batch."
        )


def selected_lock(original: bytes, old_entry: bytes, version: str) -> tuple[bytes, str]:
    original = original.replace(b"\r\n", b"\n")
    old_entry = old_entry.replace(b"\r\n", b"\n").rstrip(b"\n") + b"\n\n"
    if sha(original) != LOCK_SHA or sha(old_entry) != OLD_ENTRY_SHA:
        raise ValueError(
            "The preserved original lock or historical package entry changed."
        )
    original_doc = tomllib.loads(original.decode())
    entry = tomllib.loads(old_entry.decode())
    if len(entry["package"]) != 1 or entry["package"][0]["version"] != "0.7.1":
        raise ValueError("The historical file must contain exactly httptools 0.7.1.")
    text = original.decode()
    matches = [
        "[[package]]" + part
        for part in text.split("[[package]]")[1:]
        if part.lstrip().startswith('name = "httptools"\n')
    ]
    if len(matches) != 1 or version not in {"0.7.1", "0.9.0"}:
        raise ValueError("Unexpected lock package or requested version.")
    result = (
        text.replace(matches[0], old_entry.decode(), 1).encode()
        if version == "0.7.1"
        else original
    )
    changed_doc = tomllib.loads(result.decode())
    common_docs = []
    for document in (original_doc, changed_doc):
        packages = document.pop("package")
        target = [item for item in packages if item["name"] == "httptools"]
        if len(target) != 1:
            raise ValueError("Expected one httptools package entry.")
        document["package"] = [item for item in packages if item["name"] != "httptools"]
        common_docs.append(document)
    if common_docs[0] != common_docs[1]:
        raise ValueError(
            "The condition must not change any other lock entry or metadata."
        )
    expected_sha = OLD_LOCK_SHA if version == "0.7.1" else LOCK_SHA
    if sha(result) != expected_sha:
        raise ValueError("Selected lock does not match the preverified condition.")
    return result, fingerprint(common_docs[0])


def emit(path: str, values: dict[str, str]) -> None:
    with Path(os.environ[path]).open("a", encoding="utf-8") as stream:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise ValueError("Multiline output/environment path is not allowed.")
            stream.write(f"{key}={value}\n")


def initialize(bundle: Path) -> int:
    expected = identity()
    workspace = Path.cwd().resolve()
    if (
        workspace != Path(os.environ["GITHUB_WORKSPACE"]).resolve()
        or (workspace / ".venv").exists()
    ):
        raise ValueError("Expected an untouched checkout with no workload venv.")
    if Path(os.environ["GITHUB_WORKSPACE"]).is_junction():
        raise ValueError("Input selection must precede any treatment migration.")
    checkout_lock = Path("uv.lock").read_bytes()
    checkout_entry = Path(".github/httptools-0.7.1.lock").read_bytes()
    data, common_lock = selected_lock(
        checkout_lock, checkout_entry, expected["httptools"]
    )
    bundle.mkdir(parents=True, exist_ok=False)
    original = subprocess.check_output(
        [
            "git",
            "ls-tree",
            "HEAD",
            "fastapi",
            "tests",
            "docs_src",
            "scripts/tests",
            "pyproject.toml",
            "scripts/test.sh",
            "scripts/test-cov.sh",
            "scripts/platform_ci.py",
            "scripts/isolated_platform_ci.py",
            ".github/isolated-build-requirements.txt",
        ],
    ).decode("utf-8")
    Path("uv.lock").write_bytes(data)
    isolated.write_json(
        bundle / "inputs.json",
        {
            **expected,
            "original_lock_sha256": LOCK_SHA,
            "checkout_lock_sha256": sha(checkout_lock),
            "checkout_entry_sha256": sha(checkout_entry),
            "line_endings": "Canonical LF before preparation in every condition",
            "selected_lock_sha256": sha(data),
            "common_lock_sha256": common_lock,
            "unchanged_git_trees": original,
            "common_input_sha256": fingerprint(
                {"lock": common_lock, "trees": original}
            ),
            "uv": storage.UV_VERSION,
            "tools": isolated.TOOLS,
            "python": "3.13.17",
            "fastapi": EXPECTED_FASTAPI,
            "loops": 1,
            "worker_policy": "existing psutil physical CPUs, then logical CPUs, then 1",
        },
    )
    if expected["layout"] != "default":
        return 0
    started = time.perf_counter()
    root = (
        workspace.parent
        / f".threeway-{expected['run_id']}-{expected['run_attempt']}-{expected['condition']}-{expected['trial']}"
    )
    root.mkdir(exist_ok=False)
    paths = {}
    initial = {}
    for name in ("uv_cache", "pip_cache", "temp", "wheelhouse"):
        path = root / name.replace("_", "-")
        path.mkdir(exist_ok=False)
        if any(path.iterdir()):
            raise ValueError("The per-trial cache must start empty.")
        paths[name] = str(path)
        initial[name] = {"fresh": True, "entries": 0}
    environment = {
        "UV_CACHE_DIR": paths["uv_cache"],
        "PIP_CACHE_DIR": paths["pip_cache"],
        "TEMP": paths["temp"],
        "TMP": paths["temp"],
    }
    isolated.write_json(
        bundle / "default-layout.json",
        {
            **expected,
            "workspace": str(workspace),
            "paths": paths,
            "environment": environment,
            "initial_cache_state": initial,
            "cache_configuration_seconds": time.perf_counter() - started,
            "storage_invoked": False,
            "disk_discovery": False,
            "workspace_migrated": False,
            "protection_probed": False,
            "protection_operations": [],
        },
    )
    emit("GITHUB_ENV", environment)
    emit(
        "GITHUB_OUTPUT",
        {"workspace": str(workspace), "wheelhouse": paths["wheelhouse"]},
    )
    return 0


def check_identity(data: dict, expected: dict) -> None:
    if any(data.get(key) != value for key, value in expected.items()):
        raise ValueError("Evidence condition/run/commit identity does not match.")


def validate_layout(bundle: Path, expected: dict, postcheck: bool) -> dict:
    if expected["layout"] == "refs-devdrive":
        record = storage.load_json(bundle / "storage.json")
        storage.validate_applied_storage(record, expected, postcheck=postcheck)
        return record
    record = storage.load_json(bundle / "default-layout.json")
    check_identity(record, expected)
    if any(
        record[name] is not False
        for name in ("storage_invoked", "disk_discovery", "workspace_migrated")
    ):
        raise ValueError(
            "Default controls must not run storage discovery or migration."
        )
    if (bundle / "storage.json").exists():
        raise ValueError(
            "A default job unexpectedly contains a native storage receipt."
        )
    if any(
        item["fresh"] is not True or item["entries"] != 0
        for item in record["initial_cache_state"].values()
    ) or set(record["initial_cache_state"]) != {
        "uv_cache",
        "pip_cache",
        "temp",
        "wheelhouse",
    }:
        raise ValueError(
            "Default caches must be new and empty, just like treatment caches."
        )
    storage.metric(record["cache_configuration_seconds"], "default cache configuration")
    return record


def validate_installation(data: dict, log: str, version: str) -> None:
    if version == "0.9.0":
        storage.validate_httptools(data, log)
        return
    if (
        version != "0.7.1"
        or data["version"] != version
        or data["uv_build_metadata_present"] is not True
    ):
        raise ValueError("Condition A must prove a fresh httptools 0.7.1 source build.")
    for stage in ("Building", "Built"):
        if not re.search(rf"(?m)^\s*{stage} httptools(?:==0\.7\.1|\s|@)", log):
            raise ValueError(f"The cold log is missing {stage} httptools 0.7.1.")
    storage.validate_httptools_binary(data)
    if not re.search(r"(?m)^\s*\+\s+httptools==0\.7\.1\s*$", log):
        raise ValueError("The cold installation log does not contain httptools 0.7.1.")


def pe_header(path: Path) -> int:
    with path.open("rb") as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            raise ValueError("Expected a native PE executable.")
        stream.seek(struct.unpack_from("<I", header, 60)[0])
        signature = stream.read(6)
        if signature[:4] != b"PE\0\0":
            raise ValueError("Invalid PE signature.")
        return struct.unpack_from("<H", signature, 4)[0]


def path_facts(path: Path) -> dict:
    physical = path.resolve(strict=True)
    filesystem = ctypes.create_unicode_buffer(64)
    function = ctypes.WinDLL("kernel32", use_last_error=True).GetVolumeInformationW
    function.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_ulong,
    ]
    function.restype = ctypes.c_int
    if not function(
        physical.anchor, None, 0, None, None, None, filesystem, len(filesystem)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return {
        "physical": str(physical),
        "drive": physical.drive,
        "filesystem": filesystem.value,
        "is_dev_drive": os.path.isdevdrive(physical),
    }


def audit(output: Path, wheelhouse: Path, bundle: Path) -> int:
    expected = identity()
    inputs = storage.load_json(bundle / "inputs.json")
    check_identity(inputs, expected)
    if sha(Path("uv.lock").read_bytes()) != inputs["selected_lock_sha256"]:
        raise ValueError("The package manager changed the selected lock.")
    data = storage.read_httptools()
    isolated.write_json(output / "httptools.json", data)
    validate_installation(
        data, (output / "dependencies.log").read_text("utf-8"), expected["httptools"]
    )
    packages = {
        dist.metadata["Name"].lower().replace("_", "-"): dist.version
        for dist in distributions()
    }
    if packages.get("fastapi") != EXPECTED_FASTAPI:
        raise ValueError("The source is not the original FastAPI workload.")
    uv = shutil.which("uv")
    if uv is None or pe_header(Path(uv)) != 0xAA64:
        raise ValueError("uv must be native ARM64.")
    uv_version = subprocess.check_output([uv, "--version"], text=True).strip()
    if not uv_version.startswith("uv 0.13.5 "):
        raise ValueError("uv version drifted.")
    uv_cache = subprocess.check_output([uv, "cache", "dir"], text=True).strip()
    pip_cache = subprocess.check_output(
        [sys.executable, "-m", "pip", "cache", "dir"], text=True
    ).strip()
    paths = {
        "workspace": path_facts(Path.cwd()),
        "venv": path_facts(Path(sys.prefix)),
        "uv_cache": path_facts(Path(uv_cache)),
        "pip_cache": path_facts(Path(pip_cache)),
        "temp": path_facts(Path(os.environ["TEMP"])),
        "wheelhouse": path_facts(wheelhouse),
    }
    if (
        Path(uv_cache).resolve() != Path(os.environ["UV_CACHE_DIR"]).resolve()
        or Path(pip_cache).resolve() != Path(os.environ["PIP_CACHE_DIR"]).resolve()
    ):
        raise ValueError("The tools did not use the selected fresh caches.")
    if any(info["drive"] != paths["workspace"]["drive"] for info in paths.values()):
        raise ValueError("Workload and cold caches must share the selected volume.")
    wanted_fs = "ReFS" if expected["layout"] == "refs-devdrive" else "NTFS"
    if any(
        info["filesystem"] != wanted_fs
        or info["is_dev_drive"] != (expected["layout"] == "refs-devdrive")
        for info in paths.values()
    ):
        raise ValueError("Actual volume type does not match the requested condition.")
    isolated.write_json(
        output / "runtime.json",
        {
            **expected,
            "packages": packages,
            "uv_version": uv_version,
            "uv_pe_machine": 0xAA64,
            "python_pe_machine": pe_header(Path(sys.executable)),
            "interpreter_platform": sysconfig_platform(),
            "paths": paths,
            "common_installed_sha256": fingerprint(
                {
                    name: version
                    for name, version in packages.items()
                    if name != "httptools"
                }
            ),
            "lock_sha256": sha(Path("uv.lock").read_bytes()),
            "new_instrumentation": False,
            "base_python_or_agent_moved": False,
        },
    )
    return 0


def sysconfig_platform() -> str:
    import sysconfig

    return sysconfig.get_platform()


def prepare(output: Path, wheelhouse: Path, bundle: Path) -> int:
    expected = identity()
    validate_layout(bundle, expected, postcheck=False)
    inputs = storage.load_json(bundle / "inputs.json")
    check_identity(inputs, expected)
    if sha(Path("uv.lock").read_bytes()) != inputs["selected_lock_sha256"]:
        raise ValueError("Selected lock changed before preparation.")
    started = time.perf_counter()
    code = isolated.prepare(isolated.new_setup(), 1, output, wheelhouse)
    if code == 0:
        code, _ = common.run_command(
            [
                str(isolated.workload_python()),
                "-m",
                "scripts.storage_threeway",
                "audit",
                "--output",
                str(output),
                "--wheelhouse",
                str(wheelhouse),
                "--bundle",
                str(bundle),
            ],
            output / "installation-audit.log",
            dict(os.environ),
        )
    setup = common.read_result(output / "setup.json")
    setup.dependency_setup_seconds = round(time.perf_counter() - started, 6)
    setup.setup_exit_code = code
    setup.status = "setup_succeeded" if code == 0 else "setup_failed"
    if code and not setup.error:
        setup.error = (
            "Preparation or installation audit failed; see the preserved logs."
        )
    isolated.write_json(output / "setup.json", asdict(setup))
    return int(code != 0)


def finalize(output: Path, bundle: Path) -> int:
    expected = identity()
    check_identity(storage.load_json(bundle / "inputs.json"), expected)
    if output.exists():
        target = bundle / "measurement"
        shutil.copytree(output, target)
        for path in target.glob("loop-*/junit.xml"):
            tree = ET.parse(path)
            for suite in tree.iter("testsuite"):
                suite.attrib.pop("hostname", None)
            tree.write(path, encoding="utf-8", xml_declaration=True)
    retained = None
    if expected["layout"] == "refs-devdrive" and (bundle / "storage.json").exists():
        record = storage.load_json(bundle / "storage.json")
        if record.get("backup_path"):
            retained = Path(record["backup_path"]).is_dir()
    isolated.write_json(
        bundle / "trial.json", {**expected, "backup_retained_at_end": retained}
    )
    return 0


def job_time(jobs: dict, row: Trial) -> tuple[float, str]:
    name = f"Storage/httptools ({row.condition}, trial {row.trial})"
    matches = [job for job in jobs["jobs"] if job["name"] == name]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one completed job named {name}.")
    job = matches[0]
    if (
        str(job["run_id"]) != row.run_id
        or str(job["run_attempt"]) != row.run_attempt
        or job["head_sha"] != row.commit
        or job["status"] != "completed"
    ):
        raise ValueError("Job metadata does not identify this completed sample.")
    start = datetime.fromisoformat(job["started_at"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(job["completed_at"].replace("Z", "+00:00"))
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("Missing job timestamp timezone.")
    return storage.metric((end - start).total_seconds(), "job duration"), job[
        "conclusion"
    ]


def collect(root: Path, row: Trial, jobs: dict) -> Trial:
    expected = {
        "condition": row.condition,
        "layout": row.layout,
        "httptools": CONDITIONS[row.condition][1],
        "trial": row.trial,
        "commit": row.commit,
        "run_id": row.run_id,
        "run_attempt": row.run_attempt,
    }
    bundle = root / f"threeway-{row.condition}-{row.trial}-{row.run_attempt}"
    errors = []
    try:
        row.job_wall_seconds, row.job_conclusion = job_time(jobs, row)
        if row.job_conclusion != "success":
            errors.append(f"Job concluded {row.job_conclusion}.")
    except (KeyError, TypeError, ValueError) as exc:
        errors.append(str(exc))
    try:
        inputs = storage.load_json(bundle / "inputs.json")
        check_identity(inputs, expected)
        row.common_input_sha256 = inputs["common_input_sha256"]
        row.uv_lock_sha256 = inputs["selected_lock_sha256"]
        if row.uv_lock_sha256 != (
            OLD_LOCK_SHA if expected["httptools"] == "0.7.1" else LOCK_SHA
        ):
            raise ValueError("Unexpected selected lock hash.")
        layout = validate_layout(bundle, expected, postcheck=True)
        row.applied_layout = row.layout
        row.comparable = True
        final = storage.load_json(bundle / "trial.json")
        check_identity(final, expected)
        if row.layout == "refs-devdrive":
            if final["backup_retained_at_end"] is not True:
                raise ValueError(
                    "Treatment did not retain its original checkout backup."
                )
            for name in (
                "inspection",
                "creation",
                "migration",
                "configuration",
                "postcheck",
                "total",
            ):
                setattr(
                    row,
                    f"storage_{name}_seconds",
                    storage.metric(layout["timings"][name + "_seconds"], name),
                )
            row.cache_configuration_seconds = 0.0
            protection = layout["postcheck"]["protection"]
            row.realtime_protection = protection["realtime_protection_enabled"]
            row.performance_mode_status = protection["performance_mode_status"]
            row.performance_mode = protection["performance_mode"]
        else:
            row.storage_total_seconds = 0.0
            row.cache_configuration_seconds = layout["cache_configuration_seconds"]
    except (OSError, KeyError, TypeError, ValueError) as exc:
        errors.append(str(exc))
    output = bundle / "measurement"
    identity_result = common.Result(
        platform="windows-arm64",
        repetition=0,
        commit=row.commit,
        run_id=row.run_id,
        run_attempt=row.run_attempt,
    )
    try:
        setup = common.read_result(output / "setup.json")
        isolated.check_identity(setup, identity_result, 0)
        row.setup_seconds = storage.metric(
            setup.dependency_setup_seconds, "preparation"
        )
        if setup.status != "setup_succeeded" or setup.setup_exit_code != 0:
            raise ValueError(setup.error or "Preparation failed.")
        audit_record = isolated.validate_audit(
            storage.load_json(output / "audit.json"), 1
        )
        row.wheelhouse_sha256 = fingerprint(audit_record["wheelhouse"])
        runtime = storage.load_json(output / "runtime.json")
        check_identity(runtime, expected)
        if (
            runtime["python_pe_machine"] != 0xAA64
            or runtime["interpreter_platform"] != "win-arm64"
        ):
            raise ValueError("Runtime interpreter is not native ARM64.")
        row.installed_common_sha256 = runtime["common_installed_sha256"]
        row.uv_version = runtime["uv_version"]
        for name in ("workspace", "venv", "uv_cache", "pip_cache", "temp"):
            setattr(row, name + "_physical", runtime["paths"][name]["physical"])
        row.workspace_filesystem = runtime["paths"]["workspace"]["filesystem"]
        data = storage.load_json(output / "httptools.json")
        log = (output / "dependencies.log").read_text("utf-8")
        validate_installation(data, log, expected["httptools"])
        row.httptools_version = data["version"]
        row.httptools_installation = (
            "source-build" if data["uv_build_metadata_present"] else "prebuilt-wheel"
        )
        row.httptools_wheel_tag = data["wheel_tags"][0]
        row.uv_hardlink_fallback = "Failed to hardlink" in log
    except (OSError, KeyError, TypeError, ValueError) as exc:
        errors.append(str(exc))
    result = isolated.collect_loop(output / "loop-1", identity_result, 1)
    row.build_seconds, row.test_seconds, row.workers = (
        result.build_seconds,
        result.test_seconds,
        result.workers,
    )
    for name in (
        "tests",
        "passed",
        "failures",
        "errors",
        "skipped",
        "coverage_percent",
    ):
        setattr(row, name, getattr(result, name))
    if result.status != "passed":
        errors.append(result.error or "The full build/test did not pass.")
    else:
        try:
            manifest = [
                (
                    item.attrib["classname"],
                    item.attrib["name"],
                    next(
                        (
                            tag
                            for tag in ("failure", "error", "skipped")
                            if item.find(tag) is not None
                        ),
                        "passed",
                    ),
                )
                for item in ET.parse(output / "loop-1" / "junit.xml").findall(
                    ".//testcase"
                )
            ]
            row.test_manifest_sha256 = fingerprint(sorted(manifest))
        except (OSError, KeyError, ET.ParseError) as exc:
            errors.append(str(exc))
    if all(
        getattr(row, name) is not None
        for name in ("setup_seconds", "build_seconds", "test_seconds")
    ):
        row.measured_phase_seconds = sum(
            storage.metric(getattr(row, name), name)
            for name in ("setup_seconds", "build_seconds", "test_seconds")
        )
    row.error = "; ".join(errors)
    row.status = "passed" if not errors and row.comparable else "incomplete"
    return row


def summarize(rows: list[Trial]) -> tuple[list[dict], dict]:
    summaries = []
    for condition in CONDITIONS:
        group = [row for row in rows if row.condition == condition]
        valid = [row for row in group if row.status == "passed" and row.comparable]
        complete = len(group) == len(valid) == 3 and {row.trial for row in valid} == {
            1,
            2,
            3,
        }
        complete = complete and all(
            getattr(row, name) is not None for row in valid for name in METRICS
        )
        summary = {
            "condition": condition,
            "valid_trials": len(valid),
            "expected_trials": 3,
            "status": "passed" if complete else "incomplete",
        }
        for name in METRICS:
            summary["mean_" + name] = (
                mean([getattr(row, name) for row in valid]) if complete else None
            )
        summaries.append(summary)
    inconsistent = [
        name
        for name in SHARED_FIELDS
        if len({getattr(row, name) for row in rows if row.status == "passed"}) != 1
    ]
    consistent = not inconsistent
    for summary in summaries:
        summary["input_mismatches"] = ", ".join(inconsistent)
    if not consistent:
        for summary in summaries:
            if summary["status"] == "passed":
                summary["status"] = "input_mismatch"
                for name in METRICS:
                    summary["mean_" + name] = None
    comparisons = {}
    for label, first, second in (
        ("B-minus-A", 0, 1),
        ("C-minus-B", 1, 2),
        ("C-minus-A", 0, 2),
    ):
        before, after = summaries[first], summaries[second]
        comparable = consistent and before["status"] == after["status"] == "passed"
        values: dict = {
            "comparable": comparable,
            "reason": ""
            if comparable
            else "Incomplete groups or inconsistent inputs: " + ", ".join(inconsistent),
        }
        for name in METRICS:
            if comparable:
                baseline = storage.metric(before["mean_" + name], name)
                treatment = storage.metric(after["mean_" + name], name)
                values[name + "_delta"] = treatment - baseline
                values[name + "_reduction_percent"] = (
                    (baseline - treatment) / baseline * 100 if baseline > 0 else None
                )
            else:
                values[name + "_delta"] = None
                values[name + "_reduction_percent"] = None
        comparisons[label] = values
    return summaries, comparisons


def aggregate(source: Path, output: Path, jobs: Path) -> int:
    try:
        metadata = storage.load_json(jobs)
    except (OSError, ValueError) as exc:
        metadata = {"jobs": [], "error": str(exc)}
    rows = [
        collect(
            source,
            Trial(
                layout,
                number,
                os.environ["GITHUB_SHA"],
                os.environ["GITHUB_RUN_ID"],
                os.environ["GITHUB_RUN_ATTEMPT"],
                condition=condition,
            ),
            metadata,
        )
        for condition, (layout, _) in CONDITIONS.items()
        for number in (1, 2, 3)
    ]
    summaries, comparisons = summarize(rows)
    success = os.environ["MATRIX_RESULT"] == "success" and all(
        item["status"] == "passed" for item in summaries
    )
    lines = [
        "# Windows ARM64: storage and httptools reference comparison",
        "",
        f"Commit `{os.environ['GITHUB_SHA']}`; run `{os.environ['GITHUB_RUN_ID']}`; "
        f"attempt `{os.environ['GITHUB_RUN_ATTEMPT']}`. **{'passed' if success else 'incomplete / not comparable'}**.",
        "",
        "Three fresh free windows-11-arm jobs per condition, one preparation/build/full-test per job. "
        "This is one contemporaneous batch, not a reuse of historical means.",
        "",
        "| Condition | Valid | Storage s | Cold-cache config s | Prep s | Build s | Test s | Prep+build+test s | Full job s |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in summaries:
        values = [
            item["condition"],
            f"{item['valid_trials']}/3",
            *[item["mean_" + name] for name in METRICS],
        ]
        lines.append("| " + " | ".join(common.cell(value) for value in values) + " |")
    lines += [
        "",
        "| Condition | Trial | Status | Storage s | Prep s | Build s | Test s | Phase sum s | Full job s | Passed/skipped | Coverage % |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|---|---:|",
    ]
    for row in rows:
        values = [
            row.condition,
            row.trial,
            row.status,
            row.storage_total_seconds,
            row.setup_seconds,
            row.build_seconds,
            row.test_seconds,
            row.measured_phase_seconds,
            row.job_wall_seconds,
            f"{common.cell(row.passed)}/{common.cell(row.skipped)}",
            row.coverage_percent,
        ]
        lines.append("| " + " | ".join(common.cell(value) for value in values) + " |")
    lines += [
        "",
        "| Contrast | Prep change s | Phase-sum change s | Full-job change s | Full-job reduction % |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, item in comparisons.items():
        values = [
            name,
            item["setup_seconds_delta"],
            item["measured_phase_seconds_delta"],
            item["job_wall_seconds_delta"],
            item["job_wall_seconds_reduction_percent"],
        ]
        lines.append("| " + " | ".join(common.cell(value) for value in values) + " |")
    lines += [
        "",
        "## Interpretation and boundaries",
        "",
        "A = default/httptools 0.7.1; B = default/0.9.0; C = guarded ReFS Dev Drive/0.9.0. "
        "Only the httptools package record changes between the original locked dependency inputs. "
        "Python 3.13.17, uv 0.13.5, build 1.6.1, pip 25.0.1, PDM 2.5.0 and the original "
        "FastAPI 0.162.2 build/test helpers are shared; no HOBL upgrade or coverage optimization patch is applied.",
        "",
        "Preparation has the same boundary in every condition: uv sync (new venv/dependencies), "
        "build tools, offline backend wheelhouse, existing build audit and the common installation/path audit. "
        "The additional audit time is included, not silently excluded. All dependency caches start empty; "
        "image caches are neither reused nor erased. Page caches are not flushed.",
        "",
        "Default controls never invoke the native storage script, disk discovery, migration, legacy "
        "Get-Volume path sweeps or protection checks. Their storage time is 0 because that path is not run; "
        "small cold-cache directory setup is reported separately. A common lightweight Python path/tool "
        "audit remains inside preparation. Treatment alone runs the unchanged guarded RAW NVMe logic and "
        "postcheck; its storage time includes discovery, creation, migration, cache setup and postcheck. "
        "Populated copying can warm OS caches. Missing/ineligible treatment is a failure, not a default fallback.",
        "",
        "Build is the unchanged isolated sdist then wheel-from-sdist command with two venv+pip environments. "
        "Tests retain the original Git Bash, pytest-xdist physical-CPU worker policy, all tests plus scripts/tests, "
        "20s timeout and coverage/context/report behavior. Actual worker count and exact JUnit identity/outcome "
        "fingerprints must match before comparisons are emitted. Installed non-httptools versions are also checked.",
        "",
        "Prep+build+test excludes storage and cache configuration. Full-job time is GitHub completed_at minus "
        "started_at, including setup/checks/storage/staging/upload/post steps in that interval, not a sum of timers; "
        "queue time before started_at is excluded. A prep win is not a full-job win. "
        "All three successful complete samples are required for each arithmetic mean; no slow samples are removed.",
        "",
        "Source-build/prebuilt selection uses installed WHEEL/INSTALLER, uv_build.json presence, native ARM64 "
        "extension PE/RECORD checks and the cold installation log. Installed binary hashes are not archive hashes. "
        "No local/self-hosted disks or security settings are changed. Default protection state is unprobed, not "
        "assumed equal; treatment retains the original before/after evidence. No paid runners, retries or extra matrices.",
        "",
        "Historical 52.928s was one preparation before three same-runner loops in run 42565069560, "
        "not a three-trial prep mean. Run 42752473286 is historical, not this batch's baseline. "
        "These hosted reference results do not establish a universal OS/storage gain.",
        "",
        "Download the report artifact for all nine trials, arithmetic means, contrasts and raw job metadata; "
        "per-trial artifacts contain logs, input/installation/storage receipts, JUnit and coverage, not environments, "
        "caches, disk identifiers or backups. GitHub retention is seven days.",
    ]
    for row in rows:
        if row.error:
            lines += ["", f"**{row.condition}/{row.trial}:** {common.cell(row.error)}"]
    isolated.publish_summary(output, "\n".join(lines) + "\n")
    for name, records, columns in (
        (
            "trials.csv",
            [asdict(row) for row in rows],
            [item.name for item in fields(Trial)],
        ),
        ("report.csv", summaries, list(summaries[0])),
    ):
        with (output / name).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerows(records)
    isolated.write_json(
        output / "report.json",
        {
            "success": success,
            "summaries": summaries,
            "comparisons": comparisons,
            "trials": [asdict(row) for row in rows],
        },
    )
    return int(not success)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("initialize", "prepare", "audit", "run", "finalize", "report"),
    )
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--output", type=Path, default=Path("threeway-results"))
    parser.add_argument("--input", type=Path, default=Path("threeway-artifacts"))
    parser.add_argument("--jobs", type=Path, default=Path("threeway-jobs.json"))
    args = parser.parse_args()
    if args.command == "report":
        return aggregate(args.input, args.output, args.jobs)
    require_host()
    if args.bundle is None:
        parser.error("--bundle is required")
    if args.command == "initialize":
        return initialize(args.bundle)
    if args.command == "finalize":
        return finalize(args.output, args.bundle)
    if args.wheelhouse is None:
        parser.error("--wheelhouse is required")
    if args.command == "prepare":
        return prepare(args.output, args.wheelhouse, args.bundle)
    if args.command == "audit":
        return audit(args.output, args.wheelhouse, args.bundle)
    validate_layout(args.bundle, identity(), postcheck=True)
    return isolated.run_loops(isolated.new_setup(), 1, args.output, args.wheelhouse)


if __name__ == "__main__":
    sys.exit(main())
