import argparse
import csv
import hashlib
import json
import math
import os
import platform
import re
import shutil
import struct
import sys
import sysconfig
import time
import xml.etree.ElementTree as ET
from base64 import urlsafe_b64encode
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from email.parser import Parser
from importlib import import_module
from importlib.metadata import distribution
from pathlib import Path
from statistics import mean

from scripts import isolated_platform_ci as isolated
from scripts import platform_ci as common

LAYOUTS = ("default", "refs-devdrive")
TRIALS = (1, 2, 3)
UV_VERSION = "0.13.5"
HTTPTOOLS_VERSION = "0.9.0"
HTTPTOOLS_TAG = "cp313-cp313-win_arm64"
HTTPTOOLS_EXTENSIONS = ("httptools.parser.parser", "httptools.parser.url_parser")


@dataclass
class Trial:
    layout: str
    trial: int
    commit: str
    run_id: str
    run_attempt: str
    status: str = "incomplete"
    applied_layout: str = ""
    comparable: bool = False
    storage_inspection_seconds: float | None = None
    storage_creation_seconds: float | None = None
    storage_migration_seconds: float | None = None
    storage_configuration_seconds: float | None = None
    storage_postcheck_seconds: float | None = None
    storage_total_seconds: float | None = None
    setup_seconds: float | None = None
    build_seconds: float | None = None
    test_seconds: float | None = None
    job_wall_seconds: float | None = None
    job_conclusion: str = ""
    tests: int | None = None
    passed: int | None = None
    failures: int | None = None
    errors: int | None = None
    skipped: int | None = None
    coverage_percent: float | None = None
    workspace_filesystem: str = ""
    workspace_physical: str = ""
    uv_cache_physical: str = ""
    pip_cache_physical: str = ""
    temp_physical: str = ""
    venv_physical: str = ""
    uv_version: str = ""
    uv_lock_sha256: str = ""
    wheelhouse_sha256: str = ""
    realtime_protection: bool | None = None
    performance_mode_status: int | None = None
    performance_mode: str = ""
    uv_hardlink_fallback: bool | None = None
    httptools_version: str = ""
    httptools_installation: str = ""
    httptools_wheel_tag: str = ""
    error: str = ""


def load_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain an object.")
    return data


def require_hosted_arm() -> None:
    if (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("RUNNER_OS") != "Windows"
        or os.environ.get("RUNNER_ARCH") != "ARM64"
        or sys.platform != "win32"
        or sysconfig.get_platform() != "win-arm64"
        or platform.python_version() != "3.13.17"
    ):
        raise ValueError(
            "Storage A/B workload commands require hosted native Windows ARM64 Python."
        )


def identity() -> dict[str, str | int]:
    layout = os.environ["STORAGE_LAYOUT"]
    raw_trial = os.environ["STORAGE_TRIAL"]
    if layout not in LAYOUTS or raw_trial not in {"1", "2", "3"}:
        raise ValueError("Unexpected storage A/B layout or trial.")
    return {
        "layout": layout,
        "trial": int(raw_trial),
        "commit": os.environ["GITHUB_SHA"],
        "run_id": os.environ["GITHUB_RUN_ID"],
        "run_attempt": os.environ["GITHUB_RUN_ATTEMPT"],
    }


def check_storage_identity(storage: dict, expected: dict) -> None:
    context = storage["context"]
    if (
        storage["requested_layout"] != expected["layout"]
        or type(storage["trial"]) is not int
        or storage["trial"] != expected["trial"]
        or context["commit"] != expected["commit"]
        or context["run_id"] != expected["run_id"]
        or context["run_attempt"] != expected["run_attempt"]
        or context["repository"] != "lei9444/fastapi"
        or context["github_actions"] != "true"
        or context["runner_environment"] != "github-hosted"
        or context["runner_os"] != "Windows"
        or context["runner_arch"] != "ARM64"
        or context["os_arch"].lower() != "arm64"
        or context["process_arch"].lower() != "arm64"
        or context["is_windows"] is not True
    ):
        raise ValueError(
            "Storage evidence has an unexpected identity or runner context."
        )


def validate_applied_storage(storage: dict, expected: dict, postcheck: bool) -> None:
    check_storage_identity(storage, expected)
    if (
        storage["python"]["version"] != "3.13.17"
        or storage["python"]["platform"] != "win-arm64"
        or storage["python"]["pointer_bits"] != 64
    ):
        raise ValueError(
            "Storage preparation did not use the pinned native interpreter."
        )
    if (
        storage.get("applied") is not True
        or storage.get("comparable") is not True
        or storage.get("applied_layout") != expected["layout"]
        or storage.get("status") != "applied"
    ):
        raise ValueError(
            storage.get("reason") or "Requested layout was not applied/comparable."
        )
    phases = ["inspection", "configuration", "total"]
    if expected["layout"] == "refs-devdrive":
        phases += ["creation", "migration"]
    if postcheck:
        phases.append("postcheck")
    for phase in phases:
        metric(storage["timings"].get(phase + "_seconds"), f"storage {phase}")
    for name in ("uv_cache", "pip_cache", "temp", "wheelhouse"):
        initial = storage["initial_cache_state"][name]
        if (
            initial["fresh"] is not True
            or type(initial["entries"]) is not int
            or initial["entries"] != 0
        ):
            raise ValueError(
                "Every trial must begin with fresh empty cache/temp directories."
            )
    if expected["layout"] == "default":
        if (
            storage["mutation_attempted"]
            or storage["volume_verified"]
            or storage["workspace_migrated"]
        ):
            raise ValueError(
                "The default-layout control must never initialize or migrate storage."
            )
    elif (
        storage["eligible_disk_count"] != 1
        or storage["volume_verified"] is not True
        or storage["workspace_migrated"] is not True
        or storage["backup_retained"] is not True
        or storage["copy"]["verified"] is not True
        or storage["copy"]["source_digest"] != storage["copy"]["target_digest"]
    ):
        raise ValueError(
            "ReFS requires verified volume, complete copy, junction, and retained backup."
        )
    if expected["layout"] == "refs-devdrive":
        disk = storage["selected_disk"]
        if (
            disk["eligible"] is not True
            or disk["reasons"] != []
            or disk["BusType"] != "NVMe"
            or disk["PartitionStyle"] != "RAW"
            or type(disk["NumberOfPartitions"]) is not int
            or disk["NumberOfPartitions"] != 0
            or type(disk["Size"]) is not int
            or disk["Size"] < 50 * 1024**3
            or any(
                disk[name] is not False
                for name in ("IsBoot", "IsSystem", "IsOffline", "IsReadOnly")
            )
            or not re.fullmatch(r"[0-9a-f]{64}", storage["copy"]["source_digest"])
        ):
            raise ValueError(
                "The reported treatment disk/copy did not satisfy its safety guards."
            )
    before = storage["protection_before"]
    after = storage["protection_after"]
    if (
        before["available"] is not True
        or after["available"] is not True
        or type(before["realtime_protection_enabled"]) is not bool
        or after["realtime_protection_enabled"] != before["realtime_protection_enabled"]
        or type(before["performance_mode_status"]) is not int
        or before["performance_mode_status"] not in (0, 1)
        or type(after["performance_mode_status"]) is not int
        or after["performance_mode_status"] not in (0, 1)
    ):
        raise ValueError(
            "Protection evidence is missing or real-time protection changed."
        )
    if not postcheck:
        return
    check = storage["postcheck"]
    if (
        check["logical_workspace_unchanged"] is not True
        or not re.match(rf"^uv {re.escape(UV_VERSION)}(?:\s|$)", check["uv"]["version"])
        or check["uv"]["architecture"] != "ARM64"
    ):
        raise ValueError("Native uv or logical-workspace verification failed.")
    paths = check["paths"]
    workspace_drive = paths["workspace"]["drive"]
    for name in (
        "workspace",
        "venv",
        "uv_cache",
        "pip_cache",
        "temp",
        "tmp",
        "wheelhouse",
    ):
        info = paths[name]
        if info["exists"] is not True or info["drive"] != workspace_drive:
            raise ValueError(f"{name} is missing or not physically colocated.")
        if expected["layout"] == "refs-devdrive" and (
            info["filesystem"] != "ReFS" or info["is_dev_drive"] is not True
        ):
            raise ValueError(f"{name} is not on an actual ReFS Dev Drive.")
    if expected["layout"] == "refs-devdrive" and check["backup_retained"] is not True:
        raise ValueError("Original workspace backup was not retained.")
    if (
        check["protection"]["available"] is not True
        or check["protection"]["realtime_protection_enabled"]
        != before["realtime_protection_enabled"]
        or type(check["protection"]["performance_mode_status"]) is not int
        or check["protection"]["performance_mode_status"] not in (0, 1)
    ):
        raise ValueError("Post-preparation protection evidence is incomplete.")


def pe_machine(data: bytes) -> int:
    if len(data) < 64 or data[:2] != b"MZ":
        raise ValueError("The installed httptools extension is not a PE binary.")
    offset = struct.unpack_from("<I", data, 0x3C)[0]
    if offset + 6 > len(data) or data[offset : offset + 4] != b"PE\0\0":
        raise ValueError("The installed httptools extension has an invalid PE header.")
    return struct.unpack_from("<H", data, offset + 4)[0]


def validate_httptools(data: dict, log: str) -> None:
    if (
        data["version"] != HTTPTOOLS_VERSION
        or data["installer"] != "uv"
        or data["wheel_tags"] != [HTTPTOOLS_TAG]
        or data["root_is_purelib"] != "false"
        or data["uv_build_metadata_present"] is not False
        or data["direct_url_present"] is not False
        or data["workload_venv"] is not True
    ):
        raise ValueError("httptools must be the pinned prebuilt native ARM64 wheel.")
    extensions = data["extensions"]
    if (
        len(extensions) != len(HTTPTOOLS_EXTENSIONS)
        or {item["module"] for item in extensions} != set(HTTPTOOLS_EXTENSIONS)
        or any(
            item["pe_machine"] != 0xAA64
            or item["record_matches"] is not True
            or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
            for item in extensions
        )
    ):
        raise ValueError(
            "httptools native extensions or installed RECORD hashes differ."
        )
    if re.search(r"(?m)^\s*(?:Building|Built) httptools(?:==|\s|@)", log):
        raise ValueError("The dependency log shows a source build of httptools.")
    if not re.search(
        rf"(?m)^\s*\+\s+httptools=={re.escape(HTTPTOOLS_VERSION)}\s*$", log
    ):
        raise ValueError("The cold installation of httptools is missing from its log.")


def audit_httptools(output: Path) -> int:
    dist = distribution("httptools")
    prefix = Path(sys.prefix).resolve()
    files = dist.files
    if files is None:
        raise ValueError("httptools installed RECORD is unavailable.")
    recorded = {}
    for item in files:
        location = dist.locate_file(item)
        if not isinstance(location, Path):
            raise ValueError("httptools RECORD must describe local installed files.")
        recorded[location.resolve()] = item
    extensions = []
    for name in HTTPTOOLS_EXTENSIONS:
        module = import_module(name)
        filename = getattr(module, "__file__", None)
        if not isinstance(filename, str):
            raise ValueError(f"{name} has no installed binary path.")
        path = Path(filename).resolve()
        if not path.is_relative_to(prefix) or path.suffix.lower() != ".pyd":
            raise ValueError(f"{name} is not a native workload-venv extension.")
        binary = path.read_bytes()
        digest = hashlib.sha256(binary)
        item = recorded.get(path)
        expected_hash = item.hash if item is not None else None
        extensions.append(
            {
                "module": name,
                "path": str(path.relative_to(prefix)),
                "pe_machine": pe_machine(binary),
                "sha256": digest.hexdigest(),
                "record_matches": expected_hash is not None
                and expected_hash.mode == "sha256"
                and expected_hash.value
                == urlsafe_b64encode(digest.digest()).rstrip(b"=").decode("ascii"),
            }
        )
    wheel_text = dist.read_text("WHEEL")
    installer = dist.read_text("INSTALLER")
    if wheel_text is None or installer is None:
        raise ValueError("httptools WHEEL/INSTALLER metadata is unavailable.")
    wheel = Parser().parsestr(wheel_text)
    evidence = {
        "version": dist.version,
        "installer": installer.strip(),
        "wheel_tags": wheel.get_all("Tag", []),
        "root_is_purelib": wheel.get("Root-Is-Purelib"),
        "wheel_metadata": wheel_text,
        "uv_build_metadata_present": dist.read_text("uv_build.json") is not None,
        "direct_url_present": dist.read_text("direct_url.json") is not None,
        "workload_venv": prefix == Path(".venv").resolve(),
        "extensions": extensions,
    }
    isolated.write_json(output / "httptools.json", evidence)
    validate_httptools(evidence, (output / "dependencies.log").read_text("utf-8"))
    print(json.dumps(evidence, indent=2))
    return 0


def prepare_trial(output: Path, wheelhouse: Path, storage_path: Path) -> int:
    expected = identity()
    storage = load_json(storage_path)
    validate_applied_storage(storage, expected, postcheck=False)
    setup = isolated.new_setup()
    common.record_interpreter(setup)
    common.verify_architecture(setup)
    isolated.write_json(output / "setup.json", asdict(setup))
    isolated.write_json(
        output / "inputs.json",
        {
            **expected,
            "uv_lock_sha256": hashlib.sha256(Path("uv.lock").read_bytes()).hexdigest(),
            "cache_policy": "fresh-per-trial-physical-directories",
        },
    )
    wheelhouse.mkdir(parents=True, exist_ok=True)
    python = str(isolated.workload_python())
    env = {
        **os.environ,
        "UV_PYTHON": sys.executable,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }
    commands = (
        (
            [
                "uv",
                "sync",
                "--locked",
                "--no-dev",
                "--group",
                "tests",
                "--extra",
                "all",
            ],
            "dependencies.log",
        ),
        (
            [
                "uv",
                "pip",
                "install",
                "--python",
                python,
                "--requirement",
                ".github/isolated-build-requirements.txt",
            ],
            "build-tools.log",
        ),
        (
            [
                python,
                "-m",
                "pip",
                "download",
                "--only-binary=:all:",
                "--dest",
                str(wheelhouse),
                f"pdm-backend=={isolated.TOOLS['pdm-backend']}",
            ],
            "wheelhouse.log",
        ),
        (
            [
                python,
                "-m",
                "scripts.storage_ab",
                "audit",
                "--output",
                str(output),
                "--wheelhouse",
                str(wheelhouse),
                "--storage",
                str(storage_path),
            ],
            "audit.log",
        ),
    )
    started = time.perf_counter()
    for command, log_name in commands:
        code, _ = common.run_command(command, output / log_name, env)
        if code != 0:
            break
    setup = common.read_result(output / "setup.json")
    if code == 0:
        try:
            isolated.validate_audit(load_json(output / "audit.json"), 1)
        except (OSError, ValueError, TypeError) as exc:
            code = 1
            setup.error = f"Invalid preparation audit: {exc}"
            print(setup.error, file=sys.stderr)
    setup.dependency_setup_seconds = round(time.perf_counter() - started, 3)
    setup.setup_exit_code = code
    setup.status = "setup_succeeded" if code == 0 else "setup_failed"
    if code != 0 and not setup.error:
        setup.error = f"Preparation exited {code}; see {log_name}."
    isolated.write_json(output / "setup.json", asdict(setup))
    return int(code != 0)


def finalize_trial(output: Path, bundle: Path, storage_path: Path) -> int:
    expected = identity()
    storage = load_json(storage_path)
    check_storage_identity(storage, expected)
    bundle.mkdir(parents=True, exist_ok=True)
    if output.exists():
        target = bundle / "measurement"
        if target.exists():
            raise ValueError("Artifact staging target already exists.")
        shutil.copytree(output, target)
        for path in target.glob("loop-*/junit.xml"):
            tree = ET.parse(path)
            for suite in tree.iter("testsuite"):
                suite.attrib.pop("hostname", None)
            tree.write(path, encoding="utf-8", xml_declaration=True)
    backup_retained = None
    if storage["requested_layout"] == "refs-devdrive" and storage.get("backup_path"):
        backup_retained = Path(storage["backup_path"]).is_dir()
    isolated.write_json(
        bundle / "trial.json",
        {**expected, "backup_retained_at_end": backup_retained},
    )
    return 0


def metric(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Invalid measured {name}.")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"Invalid measured {name}.")
    return float(value)


def job_wall_time(jobs: dict, expected: Trial) -> tuple[float, str]:
    name = f"Storage A/B ({expected.layout}, trial {expected.trial})"
    matches = [job for job in jobs["jobs"] if job["name"] == name]
    if len(matches) != 1:
        raise ValueError("Expected exactly one workload job metadata entry.")
    job = matches[0]
    if (
        str(job["run_id"]) != expected.run_id
        or str(job["run_attempt"]) != expected.run_attempt
        or job["head_sha"] != expected.commit
        or job["status"] != "completed"
    ):
        raise ValueError("Job metadata does not match the completed trial.")
    if any(
        not isinstance(job.get(name), str) or job[name].startswith("0001-")
        for name in ("started_at", "completed_at")
    ):
        raise ValueError("Job start/end timestamps are unavailable.")
    start = datetime.fromisoformat(job["started_at"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(job["completed_at"].replace("Z", "+00:00"))
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("Job timestamps must include their timezone.")
    elapsed = metric((end - start).total_seconds(), "job wall time")
    return elapsed, job["conclusion"]


def collect_trial(root: Path, trial: Trial, jobs: dict) -> tuple[Trial, dict]:
    folder = root / f"storage-ab-{trial.layout}-{trial.trial}-{trial.run_attempt}"
    expected = {
        "layout": trial.layout,
        "trial": trial.trial,
        "commit": trial.commit,
        "run_id": trial.run_id,
        "run_attempt": trial.run_attempt,
    }
    storage = {}
    errors = []
    try:
        trial.job_wall_seconds, trial.job_conclusion = job_wall_time(jobs, trial)
        if trial.job_conclusion != "success":
            errors.append(f"Workload job concluded {trial.job_conclusion}.")
    except (ValueError, KeyError, TypeError) as exc:
        errors.append(str(exc))
    try:
        storage = load_json(folder / "storage.json")
        check_storage_identity(storage, expected)
        trial.applied_layout = storage.get("applied_layout") or ""
        for key in (
            "inspection",
            "creation",
            "migration",
            "configuration",
            "postcheck",
            "total",
        ):
            value = storage["timings"].get(key + "_seconds")
            if value is not None:
                setattr(
                    trial, f"storage_{key}_seconds", metric(value, f"storage {key}")
                )
        validate_applied_storage(storage, expected, postcheck=True)
        trial.comparable = True
        paths = storage["postcheck"]["paths"]
        trial.workspace_filesystem = paths["workspace"]["filesystem"]
        trial.workspace_physical = paths["workspace"]["physical"]
        trial.venv_physical = paths["venv"]["physical"]
        trial.uv_cache_physical = paths["uv_cache"]["physical"]
        trial.pip_cache_physical = paths["pip_cache"]["physical"]
        trial.temp_physical = paths["temp"]["physical"]
        trial.uv_version = storage["postcheck"]["uv"]["version"]
        protection = storage["postcheck"]["protection"]
        trial.realtime_protection = protection["realtime_protection_enabled"]
        trial.performance_mode_status = protection["performance_mode_status"]
        trial.performance_mode = protection["performance_mode"]
        final = load_json(folder / "trial.json")
        if any(final.get(key) != value for key, value in expected.items()):
            raise ValueError("Final trial identity does not match.")
        if (
            trial.layout == "refs-devdrive"
            and final["backup_retained_at_end"] is not True
        ):
            raise ValueError("Backup is not retained at the end of the workload.")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        errors.append(str(exc))
    try:
        output = folder / "measurement"
        inputs = load_json(output / "inputs.json")
        if any(inputs.get(key) != value for key, value in expected.items()):
            raise ValueError("Measurement identity does not match its layout/trial.")
        trial.uv_lock_sha256 = inputs["uv_lock_sha256"]
        if not re.fullmatch(r"[0-9a-f]{64}", trial.uv_lock_sha256):
            raise ValueError("Lock-file fingerprint is missing.")
        audit = isolated.validate_audit(load_json(output / "audit.json"), 1)
        trial.wheelhouse_sha256 = hashlib.sha256(
            json.dumps(audit["wheelhouse"], sort_keys=True).encode()
        ).hexdigest()
        identity_result = common.Result(
            platform="windows-arm64",
            repetition=0,
            commit=trial.commit,
            run_id=trial.run_id,
            run_attempt=trial.run_attempt,
        )
        setup = common.read_result(output / "setup.json")
        isolated.check_identity(setup, identity_result, 0)
        trial.setup_seconds = metric(setup.dependency_setup_seconds, "preparation")
        if setup.status != "setup_succeeded" or setup.setup_exit_code != 0:
            raise ValueError(setup.error or "Preparation failed.")
        dependency_log = (output / "dependencies.log").read_text(encoding="utf-8")
        httptools = load_json(output / "httptools.json")
        validate_httptools(httptools, dependency_log)
        trial.httptools_version = httptools["version"]
        trial.httptools_installation = "prebuilt-wheel"
        trial.httptools_wheel_tag = httptools["wheel_tags"][0]
        result = isolated.collect_loop(output / "loop-1", identity_result, 1)
        trial.build_seconds = result.build_seconds
        trial.test_seconds = result.test_seconds
        for name in (
            "tests",
            "passed",
            "failures",
            "errors",
            "skipped",
            "coverage_percent",
        ):
            setattr(trial, name, getattr(result, name))
        if result.status != "passed":
            raise ValueError(result.error or f"Measurement is {result.status}.")
        trial.uv_hardlink_fallback = "Failed to hardlink" in dependency_log
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append(str(exc))
    trial.error = "; ".join(errors)
    trial.status = "passed" if not errors and trial.comparable else "incomplete"
    return trial, storage


def build_summary(trials: list[Trial]) -> tuple[list[dict], dict]:
    summaries = []
    metrics = (
        "storage_total_seconds",
        "setup_seconds",
        "build_seconds",
        "test_seconds",
        "job_wall_seconds",
    )
    for layout in LAYOUTS:
        rows = [row for row in trials if row.layout == layout]
        valid = [row for row in rows if row.status == "passed" and row.comparable]
        complete = (
            len(rows) == len(valid) == 3
            and {row.trial for row in rows} == set(TRIALS)
            and all(getattr(row, name) is not None for row in valid for name in metrics)
        )
        summary = {
            "layout": layout,
            "valid_trials": len(valid),
            "expected_trials": 3,
            "status": "passed" if complete else "incomplete",
        }
        for name in metrics:
            values = [getattr(row, name) for row in valid]
            summary["mean_" + name] = mean(values) if complete else None
        summaries.append(summary)
    comparable = all(row["status"] == "passed" for row in summaries)
    if comparable and (
        len({row.uv_lock_sha256 for row in trials}) != 1
        or len({row.wheelhouse_sha256 for row in trials}) != 1
        or len({row.uv_version for row in trials}) != 1
        or len({row.httptools_version for row in trials}) != 1
    ):
        comparable = False
        for summary in summaries:
            summary["status"] = "input_mismatch"
            for name in metrics:
                summary["mean_" + name] = None
    comparison = {
        "comparable": comparable,
        "reason": ""
        if comparable
        else "Both groups need three valid successful trials and identical tool/lock/wheelhouse inputs.",
    }
    for name in metrics:
        if comparable:
            baseline = metric(summaries[0]["mean_" + name], name)
            treatment = metric(summaries[1]["mean_" + name], name)
            comparison[name + "_delta"] = treatment - baseline
            comparison[name + "_reduction_percent"] = (
                (baseline - treatment) / baseline * 100 if baseline > 0 else None
            )
        else:
            comparison[name + "_delta"] = None
            comparison[name + "_reduction_percent"] = None
    return summaries, comparison


def aggregate(
    source: Path,
    output: Path,
    job_file: Path,
    commit: str,
    run_id: str,
    attempt: str,
    matrix_result: str,
) -> int:
    try:
        jobs = load_json(job_file)
    except (OSError, ValueError) as exc:
        jobs = {"jobs": [], "error": str(exc)}
    trials = []
    storage_details = {}
    for layout in LAYOUTS:
        for number in TRIALS:
            trial, storage = collect_trial(
                source, Trial(layout, number, commit, run_id, attempt), jobs
            )
            trials.append(trial)
            storage_details[f"{layout}-{number}"] = storage
    summaries, comparison = build_summary(trials)
    success = matrix_result == "success" and comparison["comparable"]
    lines = [
        "# Windows ARM64 storage A/B",
        "",
        f"Commit `{commit}`; run `{run_id}`, attempt `{attempt}`.",
        f"Overall: **{'passed' if success else 'incomplete / not comparable'}**.",
        "",
        "Three fresh standard windows-11-arm jobs per group. Each prepares dependencies once "
        "and runs one isolated FastAPI build plus the unchanged full test suite. "
        "Both groups use new empty, physically colocated uv/pip cache and TEMP/TMP directories; "
        "image caches are neither reused nor erased. OS page caches are not flushed.",
        "",
        "| Layout | Valid trials | Mean storage overhead (s) | Mean preparation (s) | Mean build (s) | Mean tests (s) | Mean full job (s) |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for summary in summaries:
        values = (
            summary["layout"],
            f"{summary['valid_trials']}/3",
            summary["mean_storage_total_seconds"],
            summary["mean_setup_seconds"],
            summary["mean_build_seconds"],
            summary["mean_test_seconds"],
            summary["mean_job_wall_seconds"],
        )
        lines.append("| " + " | ".join(common.cell(v) for v in values) + " |")
    if comparison["comparable"]:
        lines += [
            "",
            "ReFS minus default full-job mean: "
            f"**{comparison['job_wall_seconds_delta']:.2f} s**; full-job reduction: "
            f"**{comparison['job_wall_seconds_reduction_percent']:.2f}%**.",
            "Preparation-only reduction: "
            f"**{comparison['setup_seconds_reduction_percent']:.2f}%**. "
            "Positive reduction means faster; storage setup/migration is not hidden.",
        ]
    else:
        lines += ["", comparison["reason"]]
    lines += [
        "",
        "| Requested | Trial | Applied | Status | Storage (s) | Preparation (s) | Build (s) | Tests (s) | Full job (s) | Passed / skipped | Coverage % |",
        "|---|---:|---|---|---:|---:|---:|---:|---:|---|---:|",
    ]
    for trial in trials:
        values = (
            trial.layout,
            trial.trial,
            trial.applied_layout,
            trial.status,
            trial.storage_total_seconds,
            trial.setup_seconds,
            trial.build_seconds,
            trial.test_seconds,
            trial.job_wall_seconds,
            f"{common.cell(trial.passed)} / {common.cell(trial.skipped)}",
            trial.coverage_percent,
        )
        lines.append("| " + " | ".join(common.cell(v) for v in values) + " |")
    lines += [
        "",
        "| Layout | Trial | httptools | Installation | Installed wheel tag |",
        "|---|---:|---|---|---|",
    ]
    for trial in trials:
        values = (
            trial.layout,
            trial.trial,
            trial.httptools_version,
            trial.httptools_installation,
            trial.httptools_wheel_tag,
        )
        lines.append("| " + " | ".join(common.cell(v) for v in values) + " |")
    lines += [
        "",
        "| Layout | Trial | Inspection | Create volume | Copy/migrate | Configure paths | Postcheck | Workspace filesystem | Performance mode setting |",
        "|---|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for trial in trials:
        values = (
            trial.layout,
            trial.trial,
            trial.storage_inspection_seconds,
            trial.storage_creation_seconds,
            trial.storage_migration_seconds,
            trial.storage_configuration_seconds,
            trial.storage_postcheck_seconds,
            trial.workspace_filesystem,
            trial.performance_mode,
        )
        lines.append("| " + " | ".join(common.cell(v) for v in values) + " |")
    lines += [
        "",
        "Storage overhead is measured inside the guarded storage and postcheck scripts, "
        "including populated-workspace copy/verification. Full-job wall time is the GitHub "
        "workload job's completed_at minus started_at, including runner provisioning, checkout, "
        "tool setup, checks, migration, artifact staging/upload and post steps within that "
        "interval; time before started_at (queueing) is excluded. It is not a sum of payload timers.",
        "",
        f"Both groups use locked httptools {HTTPTOOLS_VERSION}. The existing timed preparation "
        "also audits its installed WHEEL/INSTALLER metadata, uv source-build marker, imported "
        "ARM64 PE extensions and RECORD hashes against the cold installation log. It does not "
        "download or prewarm dependencies separately. The build remains "
        "build 1.6.1 / pip 25.0.1 / PDM 2.5.0, using the offline wheelhouse and default "
        "isolated sdist-to-wheel flow. Python is native 3.13.17; uv is pinned to 0.13.5.",
        "",
        "No agent, base Python, VS/toolcache, GITHUB_WORKSPACE or RUNNER_TEMP is moved. "
        "No Defender exclusions, protection, trust, filter or tamper policies are changed. "
        "ReFS and Dev Drive status are verified, not inferred. PerformanceModeStatus is read "
        "from Get-MpPreference: 0 means Enabled and 1 means Disabled. This setting alone does "
        "not prove asynchronous scanning occurred on a particular volume. Missing or unapplied "
        "treatments are not valid ReFS samples.",
        "",
        "This is a bounded workflow-level Python experiment, not an image-wide fix or a Rust "
        "benchmark. Different fresh hosted machines and populated-copy filesystem warming can "
        "affect results. Three samples do not establish a universal gain. Normal four-platform "
        "CI storage and loops=3 remain unchanged; enabling ReFS by default requires a later decision.",
        "Both groups include storage audits and cold-cache configuration added for this experiment. "
        "Their full-job difference is not a measured production saving for the original CI; "
        "subtracting these checks cannot establish that unmeasured result. Earlier httptools "
        "0.7.1 runs are historical references, not a simultaneous version A/B.",
        "",
        f"Download `storage-ab-report-{attempt}` for `report.csv` (group means), `trials.csv` "
        "(all six trials), `comparison.json`, `report.json` and this report. Each trial artifact "
        "retains storage/path/security metadata and workload evidence, not caches, venvs or backups. "
        "Artifacts expire after seven days.",
    ]
    for trial in trials:
        if trial.error:
            lines += [
                "",
                f"**{trial.layout} #{trial.trial}:** {common.cell(trial.error)}",
            ]
    isolated.publish_summary(output, "\n".join(lines) + "\n")
    for filename, rows, columns in (
        ("report.csv", summaries, list(summaries[0])),
        (
            "trials.csv",
            [asdict(row) for row in trials],
            [field.name for field in fields(Trial)],
        ),
    ):
        with (output / filename).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
    isolated.write_json(output / "comparison.json", comparison)
    isolated.write_json(
        output / "report.json",
        {
            "success": success,
            "matrix_result": matrix_result,
            "summaries": summaries,
            "comparison": comparison,
            "trials": [asdict(row) for row in trials],
            "storage": storage_details,
        },
    )
    return int(not success)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Cold hosted Windows ARM64 storage A/B."
    )
    parser.add_argument(
        "command", choices=("prepare", "audit", "run", "finalize", "report")
    )
    parser.add_argument("--storage", type=Path)
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--output", type=Path, default=Path("storage-ab-results"))
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--input", type=Path, default=Path("storage-ab-artifacts"))
    parser.add_argument("--jobs", type=Path, default=Path("storage-ab-jobs.json"))
    args = parser.parse_args(argv)
    if args.command == "report":
        return aggregate(
            args.input,
            args.output,
            args.jobs,
            os.environ["GITHUB_SHA"],
            os.environ["GITHUB_RUN_ID"],
            os.environ["GITHUB_RUN_ATTEMPT"],
            os.environ["MATRIX_RESULT"],
        )
    require_hosted_arm()
    if args.storage is None:
        parser.error("--storage is required for workload commands")
    storage = load_json(args.storage)
    check_storage_identity(storage, identity())
    if args.command == "finalize":
        if args.bundle is None:
            parser.error("--bundle is required")
        return finalize_trial(args.output, args.bundle, args.storage)
    validate_applied_storage(storage, identity(), postcheck=args.command == "run")
    if args.wheelhouse is None:
        parser.error("--wheelhouse is required")
    if args.command == "audit":
        code = isolated.audit_runtime(args.output, args.wheelhouse, 1)
        return code if code != 0 else audit_httptools(args.output)
    if args.command == "prepare":
        return prepare_trial(args.output, args.wheelhouse, args.storage)
    return isolated.run_loops(isolated.new_setup(), 1, args.output, args.wheelhouse)


if __name__ == "__main__":
    sys.exit(main())
