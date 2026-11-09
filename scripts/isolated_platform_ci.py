import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, fields, replace
from importlib import import_module
from importlib.metadata import version
from pathlib import Path
from statistics import mean

from scripts import platform_ci as common

TOOLS = {"build": "1.6.1", "pip": "25.0.1", "pdm-backend": "2.5.0"}
SETUP_FIELDS = {"dependency_setup_seconds", "setup_exit_code"}


@dataclass
class Summary:
    platform: str
    commit: str
    run_id: str
    run_attempt: str
    loops: int
    status: str = "incomplete"
    setup_seconds: float | None = None
    setup_exit_code: int | None = None
    recorded_loops: int = 0
    successful_loops: int = 0
    missing_loops: str = ""
    mean_build_seconds: float | None = None
    mean_test_seconds: float | None = None
    mean_coverage_percent: float | None = None
    fastapi_version: str = ""
    error: str = ""


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def resolve_loops(event: str, raw: str | None) -> int:
    if event == "push":
        return 3
    if event != "workflow_dispatch":
        raise ValueError(f"Unsupported workflow event: {event!r}")
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9]+", raw.strip()):
        raise ValueError(f"loops must be a positive integer; got {raw!r}")
    loops = int(raw.strip())
    if loops <= 0:
        raise ValueError(f"loops must be a positive integer; got {raw!r}")
    return loops


def new_setup() -> common.Result:
    return common.Result(
        platform=os.environ["PLATFORM"],
        repetition=0,
        commit=os.environ["GITHUB_SHA"],
        run_id=os.environ["GITHUB_RUN_ID"],
        run_attempt=os.environ["GITHUB_RUN_ATTEMPT"],
        runner_label=os.environ["RUNNER_LABEL"],
        runner_os=os.environ["RUNNER_OS"],
        runner_arch=os.environ["RUNNER_ARCH"],
        image_os=os.environ.get("ImageOS", ""),
        image_version=os.environ.get("ImageVersion", ""),
        expected_architecture=os.environ["EXPECTED_ARCHITECTURE"],
    )


def check_identity(
    result: common.Result, expected: common.Result, iteration: int
) -> None:
    if (
        result.platform,
        result.repetition,
        result.commit,
        result.run_id,
        result.run_attempt,
    ) != (
        expected.platform,
        iteration,
        expected.commit,
        expected.run_id,
        expected.run_attempt,
    ):
        raise ValueError(
            "Evidence has the wrong platform, loop, commit, or run attempt."
        )


def workload_python() -> Path:
    return Path(".venv").resolve() / (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    )


def validate_audit(data: object, loops: int) -> dict[str, object]:
    if not isinstance(data, dict):
        raise ValueError("The build environment audit must be an object.")
    checks = {
        "loops": type(data.get("loops")) is int and data["loops"] == loops,
        "tools": data.get("tools") == TOOLS,
        "python_version": data.get("python_version") == "3.13.17",
        "workload_venv": data.get("workload_venv") is True,
        "pip_in_venv": data.get("pip_in_venv") is True,
        "default_installer": data.get("default_installer") == "pip",
        "environment_backend": data.get("environment_backend") == "venv+pip",
        "outer_pip": data.get("outer_pip") is True,
        "creates_with_pip": data.get("creates_with_pip") is False,
    }
    if failed := [name for name, valid in checks.items() if not valid]:
        raise ValueError("Unexpected build environment: " + ", ".join(failed))
    wheels = data.get("wheelhouse")
    if not isinstance(wheels, dict) or not wheels:
        raise ValueError("The offline wheelhouse manifest is missing.")
    if not all(
        isinstance(name, str)
        and name.endswith(".whl")
        and isinstance(digest, str)
        and re.fullmatch(r"[0-9a-f]{64}", digest)
        for name, digest in wheels.items()
    ):
        raise ValueError("The offline wheelhouse manifest is invalid.")
    if not any(
        name.startswith(f"pdm_backend-{TOOLS['pdm-backend']}-") for name in wheels
    ):
        raise ValueError("The pinned backend wheel is missing from the wheelhouse.")
    if not isinstance(data.get("fastapi_version"), str) or not data["fastapi_version"]:
        raise ValueError("The installed FastAPI version is missing.")
    return data


def audit_runtime(output: Path, wheelhouse: Path, loops: int) -> int:
    import psutil

    pip = import_module("pip")
    build_env = import_module("build.env")
    setup = common.read_result(output / "setup.json")
    common.record_interpreter(setup)
    common.verify_architecture(setup)
    setup.physical_cpus = psutil.cpu_count(logical=False)
    setup.workers = setup.physical_cpus or setup.logical_cpus or 1
    setup.memory_gib = round(psutil.virtual_memory().total / 1024**3, 2)
    setup.build_backend_version = version("pdm-backend")
    prefix = Path(sys.prefix).resolve()
    pip_file = getattr(pip, "__file__", None)
    # These pinned 1.6.1 selectors do not create an environment or run a build.
    backend = build_env._PipBackend()
    audit = {
        "loops": loops,
        "tools": {name: version(name) for name in TOOLS},
        "python_version": setup.python_version,
        "fastapi_version": version("fastapi"),
        "workload_venv": prefix == Path(".venv").resolve(),
        "pip_in_venv": isinstance(pip_file, str)
        and Path(pip_file).resolve().is_relative_to(prefix),
        "default_installer": build_env.DefaultIsolatedEnv().installer,
        "environment_backend": backend.display_name,
        "outer_pip": backend._has_valid_outer_pip is True,
        "creates_with_pip": not backend._has_valid_outer_pip,
        "wheelhouse": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(wheelhouse.glob("*.whl"))
        },
    }
    write_json(output / "audit.json", audit)
    validate_audit(audit, loops)
    write_json(output / "setup.json", asdict(setup))
    print(json.dumps(audit, indent=2))
    return 0


def prepare(
    setup: common.Result, loops: int, output: Path, wheelhouse: Path
) -> int:
    common.record_interpreter(setup)
    write_json(output / "setup.json", asdict(setup))
    try:
        common.verify_architecture(setup)
    except ValueError as exc:
        setup.status = "architecture_mismatch"
        setup.error = str(exc)
        write_json(output / "setup.json", asdict(setup))
        print(setup.error, file=sys.stderr)
        return 1
    wheelhouse.mkdir(parents=True, exist_ok=True)
    python = str(workload_python())
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
                f"pdm-backend=={TOOLS['pdm-backend']}",
            ],
            "wheelhouse.log",
        ),
        (
            [
                python,
                "-m",
                "scripts.isolated_platform_ci",
                "audit",
                "--output",
                str(output),
                "--wheelhouse",
                str(wheelhouse),
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
            validate_audit(
                json.loads((output / "audit.json").read_text(encoding="utf-8")),
                loops,
            )
        except (OSError, ValueError, TypeError) as exc:
            code = 1
            setup.error = f"Invalid preparation audit: {exc}"
            print(setup.error, file=sys.stderr)
    setup.dependency_setup_seconds = round(time.perf_counter() - started, 3)
    setup.setup_exit_code = code
    setup.status = "setup_succeeded" if code == 0 else "setup_failed"
    if code != 0 and not setup.error:
        setup.error = f"Preparation exited {code}; see {log_name}."
    write_json(output / "setup.json", asdict(setup))
    return int(code != 0)


def validate_build(output: Path) -> None:
    common.validate_distributions(output / "dist")
    log = (output / "build.log").read_text(encoding="utf-8")
    if (
        log.count("Creating isolated environment:") != 2
        or log.count("Creating isolated environment: venv+pip") != 2
        or log.count(f"- pdm-backend=={TOOLS['pdm-backend']}") != 2
        or log.count("Building wheel from sdist") != 1
    ):
        raise ValueError(
            "Build evidence must show two venv+pip environments, the pinned "
            "backend installed in both, and a wheel built from the sdist."
        )


def run_loops(
    expected: common.Result, loops: int, output: Path, wheelhouse: Path
) -> int:
    setup = common.read_result(output / "setup.json")
    check_identity(setup, expected, 0)
    if setup.status != "setup_succeeded" or setup.setup_exit_code != 0:
        raise ValueError("The workload environment has not been prepared successfully.")
    validate_audit(json.loads((output / "audit.json").read_text("utf-8")), loops)
    common.verify_architecture(setup)
    env = {**os.environ, "UV_PYTHON": sys.executable}
    build_env = {
        **env,
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_NO_INDEX": "1",
        "PIP_FIND_LINKS": str(wheelhouse.resolve()),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }
    succeeded = True
    for iteration in range(1, loops + 1):
        folder = output / f"loop-{iteration}"
        result = replace(
            setup,
            repetition=iteration,
            status="incomplete",
            dependency_setup_seconds=None,
            setup_exit_code=None,
            error="",
        )
        common.write_result(result, folder)
        result.build_exit_code, result.build_seconds = common.run_command(
            [
                str(workload_python()),
                "-m",
                "build",
                "--outdir",
                str(folder / "dist"),
            ],
            folder / "build.log",
            build_env,
        )
        if result.build_exit_code == 0:
            try:
                validate_build(folder)
            except (OSError, ValueError) as exc:
                result.error = str(exc)
        else:
            result.error = f"Isolated build exited {result.build_exit_code}."
        if result.error:
            result.status = "build_failed"
            common.write_result(result, folder)
            succeeded = False
            continue
        common.write_result(result, folder)
        code, elapsed = common.run_command(
            [
                "uv",
                "run",
                "--no-sync",
                "python",
                "scripts/platform_ci.py",
                "test",
                "--output",
                str(folder),
            ],
            folder / "tests.log",
            env,
        )
        result = common.read_result(folder / "result.json")
        result.test_exit_code = code
        result.test_seconds = elapsed
        if result.status != "architecture_mismatch":
            result.status = "passed" if code == 0 else "tests_failed"
        if code != 0 and not result.error:
            result.error = f"Test command exited {code}; see tests.log and junit.xml."
        common.write_result(result, folder)
        succeeded = succeeded and result.status == "passed"
    return int(not succeeded)


def missing_ranges(loops: int, observed: set[int]) -> str:
    ranges = []
    start = 1
    for iteration in sorted(observed):
        if not 1 <= iteration <= loops:
            raise ValueError("Observed loop is outside the requested range.")
        if start < iteration:
            end = iteration - 1
            ranges.append(str(start) if start == end else f"{start}-{end}")
        start = iteration + 1
    if start <= loops:
        ranges.append(str(start) if start == loops else f"{start}-{loops}")
    return ", ".join(ranges)


def collect_loop(
    folder: Path, expected: common.Result, iteration: int
) -> common.Result:
    result = common.Result(
        platform=expected.platform,
        repetition=iteration,
        commit=expected.commit,
        run_id=expected.run_id,
        run_attempt=expected.run_attempt,
    )
    try:
        saved = common.read_result(folder / "result.json")
        check_identity(saved, expected, iteration)
        result = saved
        if (
            result.setup_exit_code is not None
            or result.dependency_setup_seconds is not None
        ):
            raise ValueError(
                "Preparation must be recorded once, not repeated per loop."
            )
        if result.status not in {
            "passed",
            "build_failed",
            "tests_failed",
            "architecture_mismatch",
        }:
            raise ValueError("Loop did not finish recording its result.")
        if result.status == "passed":
            common.verify_architecture(result)
            if (
                result.build_exit_code != 0
                or result.test_exit_code != 0
                or result.build_seconds is None
                or result.test_seconds is None
                or result.build_seconds < 0
                or result.test_seconds < 0
                or result.build_backend_version != TOOLS["pdm-backend"]
            ):
                raise ValueError("Successful loop lacks successful command evidence.")
    except (OSError, ValueError, TypeError) as exc:
        result.status = "incomplete"
        result.error = str(exc)
        return result
    if result.build_exit_code == 0:
        try:
            validate_build(folder)
        except (OSError, ValueError) as exc:
            result.error = f"{result.error} Build evidence: {exc}".strip()
            if result.status == "passed":
                result.status = "incomplete"
    if result.test_exit_code is not None:
        for reader, name in (
            (common.read_junit, "junit.xml"),
            (common.read_coverage, "coverage.json"),
        ):
            try:
                reader(result, folder / name)
            except (OSError, ValueError, KeyError, TypeError, ET.ParseError) as exc:
                result.error = f"{result.error} {name}: {exc}".strip()
                if result.status == "passed":
                    result.status = "incomplete"
        if result.status == "passed" and (result.failures or result.errors):
            result.status = "tests_failed"
            result.error = "JUnit reports failures despite a successful command exit."
    return result


def collect_platform(
    source: Path, expected: common.Result, loops: int
) -> tuple[Summary, list[common.Result]]:
    root = source / f"isolated-result-{expected.platform}-{expected.run_attempt}"
    summary = Summary(
        platform=expected.platform,
        commit=expected.commit,
        run_id=expected.run_id,
        run_attempt=expected.run_attempt,
        loops=loops,
    )
    try:
        setup = common.read_result(root / "setup.json")
        check_identity(setup, expected, 0)
        summary.setup_seconds = setup.dependency_setup_seconds
        summary.setup_exit_code = setup.setup_exit_code
        if (
            setup.status != "setup_succeeded"
            or setup.setup_exit_code != 0
            or setup.dependency_setup_seconds is None
            or setup.dependency_setup_seconds < 0
        ):
            raise ValueError(setup.error or "Preparation did not succeed.")
        common.verify_architecture(setup)
        audit = validate_audit(
            json.loads((root / "audit.json").read_text(encoding="utf-8")), loops
        )
        summary.fastapi_version = str(audit["fastapi_version"])
    except (OSError, ValueError, TypeError) as exc:
        summary.error = str(exc)
    observed: set[int] = set()
    results = []
    for folder in sorted(root.glob("loop-*")):
        match = re.fullmatch(r"loop-([0-9]+)", folder.name)
        iteration = int(match[1]) if match else 0
        if not folder.is_dir() or not 1 <= iteration <= loops or iteration in observed:
            summary.error += f" Unexpected or duplicate loop directory: {folder.name}."
            continue
        observed.add(iteration)
        results.append(collect_loop(folder, expected, iteration))
    results.sort(key=lambda result: result.repetition)
    summary.recorded_loops = len(results)
    summary.successful_loops = sum(result.status == "passed" for result in results)
    summary.missing_loops = missing_ranges(loops, observed)
    builds = [r.build_seconds for r in results if r.build_seconds is not None]
    tests = [r.test_seconds for r in results if r.test_seconds is not None]
    coverage = [r.coverage_percent for r in results if r.coverage_percent is not None]
    if (
        not summary.error
        and not summary.missing_loops
        and summary.successful_loops
        == len(builds)
        == len(tests)
        == len(coverage)
        == loops
    ):
        summary.status = "passed"
        summary.mean_build_seconds = mean(builds)
        summary.mean_test_seconds = mean(tests)
        summary.mean_coverage_percent = mean(coverage)
    return summary, results


def publish_summary(output: Path, text: str) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.md").write_text(text, encoding="utf-8")
    if summary_path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary_path).open("a", encoding="utf-8") as stream:
            stream.write(text)
    print(text)


def aggregate(
    source: Path,
    output: Path,
    commit: str,
    run_id: str,
    attempt: str,
    loops: int,
    matrix_result: str,
) -> int:
    summaries = []
    results = []
    for name in common.PLATFORMS:
        expected = common.Result(
            platform=name,
            repetition=0,
            commit=commit,
            run_id=run_id,
            run_attempt=attempt,
        )
        summary, rows = collect_platform(source, expected, loops)
        summaries.append(summary)
        results.extend(rows)
    success = matrix_result == "success" and all(
        s.status == "passed" for s in summaries
    )
    lines = [
        "# Isolated build loop reference report",
        "",
        f"Commit: `{commit}`. Run: `{run_id}`, attempt: `{attempt}`. "
        f"Loops: **{loops}**.",
        f"Overall: **{'passed' if success else 'failed / incomplete'}**. "
        f"Matrix job result: `{matrix_result}`.",
        "",
        "Four workload jobs reuse one runner and workload venv per platform. "
        "Every loop runs a fresh build process followed by the existing xdist/coverage "
        "test command. No warm-up or first-loop exclusion is applied.",
        "",
        "| Platform | Successful loops | Status | Preparation once (s) | Mean isolated build (s) | Mean tests (s) | Mean coverage % | Missing loops |",
        "|---|---:|---|---:|---:|---:|---:|---|",
    ]
    for summary in summaries:
        values = (
            summary.platform,
            f"{summary.successful_loops}/{summary.loops}",
            summary.status,
            summary.setup_seconds,
            summary.mean_build_seconds,
            summary.mean_test_seconds,
            summary.mean_coverage_percent,
            summary.missing_loops,
        )
        lines.append("| " + " | ".join(common.cell(v) for v in values) + " |")
    lines += [
        "",
        "Preparation is measured once per platform, including the workload venv, "
        "test/build tools, offline backend wheelhouse, and environment audit. "
        "It is neither averaged nor divided among loops. A platform's means are "
        "shown only when preparation and every requested loop have valid successful evidence.",
        "",
        "| Platform | Loop | Status | Tests | Passed | Failures | Errors | Skipped | Coverage % | Build (s) | Tests (s) |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for result in results:
        values = (
            result.platform,
            result.repetition,
            result.status,
            result.tests,
            result.passed,
            result.failures,
            result.errors,
            result.skipped,
            result.coverage_percent,
            result.build_seconds,
            result.test_seconds,
        )
        lines.append("| " + " | ".join(common.cell(v) for v in values) + " |")
    lines += [
        "",
        "Build measures the native workload Python running `python -m build --outdir ...`: "
        "the default sdist then wheel-from-sdist flow, two temporary venv+pip environments, "
        "backend installation from the prepared wheelhouse, and normal cleanup. "
        "Pinned tools: build 1.6.1, outer pip 25.0.1, pdm-backend 2.5.0. "
        "The audited outer pip supplies installation via `pip --python`; "
        "no uv installer or ensurepip seeding is used. Build dependency downloads "
        "are disabled with PIP_NO_INDEX and local PIP_FIND_LINKS.",
        "",
        "Python is 3.13.17 and FastAPI comes from this checkout. Only build mechanics "
        "and loop structure align with HOBL: these are not fully matched conditions "
        "with the older local FastAPI 0.119.1 / Python 3.12.10 data. Tests still use "
        "the existing pytest-xdist/coverage suite, not HOBL's serial test selection. "
        "Hardware/worker counts differ; these free hosted-runner results are reference only.",
        "",
        f"Download `isolated-report-{attempt}` for `report.csv` (platform summaries "
        "with one preparation value), `runs.csv` (individual loops), `report.json`, "
        "and this report. Each platform artifact keeps setup/audit evidence plus "
        "distinct loop logs, wheel/sdist, JUnit, and coverage files. Venvs and the "
        "downloaded wheelhouse are not uploaded. Artifacts expire after seven days.",
    ]
    for summary in summaries:
        if summary.error:
            lines += ["", f"**{summary.platform}:** {common.cell(summary.error)}"]
    for result in results:
        if result.error:
            lines += [
                "",
                f"**{result.platform}, loop {result.repetition}:** "
                f"{common.cell(result.error)}",
            ]
    publish_summary(output, "\n".join(lines) + "\n")
    summary_rows = [asdict(summary) for summary in summaries]
    run_rows = [
        {
            name: value
            for name, value in asdict(result).items()
            if name not in SETUP_FIELDS
        }
        for result in results
    ]
    for filename, rows, columns in (
        ("report.csv", summary_rows, [field.name for field in fields(Summary)]),
        (
            "runs.csv",
            run_rows,
            [
                field.name
                for field in fields(common.Result)
                if field.name not in SETUP_FIELDS
            ],
        ),
    ):
        with (output / filename).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
    write_json(
        output / "report.json",
        {
            "success": success,
            "matrix_result": matrix_result,
            "loops": loops,
            "platforms": summary_rows,
            "runs": run_rows,
        },
    )
    return int(not success)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run sequential isolated build/test loops."
    )
    parser.add_argument("command", choices=("prepare", "run", "audit", "report"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--input", type=Path, default=Path("isolated-artifacts"))
    parser.add_argument("--wheelhouse", type=Path)
    args = parser.parse_args(argv)
    output = args.output or (
        Path("isolated-report")
        if args.command == "report"
        else Path("isolated-results") / os.environ["PLATFORM"]
    )
    try:
        loops = resolve_loops(
            os.environ["GITHUB_EVENT_NAME"], os.environ.get("LOOPS")
        )
    except ValueError as exc:
        write_json(output / "report.json", {"success": False, "error": str(exc)})
        publish_summary(output, f"# Invalid loops input\n\n{common.cell(str(exc))}\n")
        print(str(exc), file=sys.stderr)
        return 1
    if args.command == "report":
        return aggregate(
            args.input,
            output,
            os.environ["GITHUB_SHA"],
            os.environ["GITHUB_RUN_ID"],
            os.environ["GITHUB_RUN_ATTEMPT"],
            loops,
            os.environ["MATRIX_RESULT"],
        )
    wheelhouse = args.wheelhouse or (
        Path(os.environ["RUNNER_TEMP"]) / "isolated-wheelhouse"
    )
    if args.command == "audit":
        return audit_runtime(output, wheelhouse, loops)
    expected = new_setup()
    if args.command == "prepare":
        return prepare(expected, loops, output, wheelhouse)
    return run_loops(expected, loops, output, wheelhouse)


if __name__ == "__main__":
    sys.exit(main())
