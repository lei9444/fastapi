import argparse
import csv
import json
import math
import os
import platform
import shutil
import struct
import subprocess
import sys
import sysconfig
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import get_type_hints

PLATFORMS = ("windows-x64", "windows-arm64", "linux-x64", "macos-arm64")


@dataclass
class Result:
    platform: str
    status: str = "incomplete"
    commit: str = ""
    run_id: str = ""
    run_attempt: str = ""
    runner_label: str = ""
    runner_os: str = ""
    runner_arch: str = ""
    image_os: str = ""
    image_version: str = ""
    os_release: str = ""
    expected_architecture: str = ""
    python_version: str = ""
    interpreter_platform: str = ""
    machine: str = ""
    pointer_bits: int = 0
    logical_cpus: int | None = None
    physical_cpus: int | None = None
    workers: int | None = None
    memory_gib: float | None = None
    dependency_setup_seconds: float | None = None
    setup_exit_code: int | None = None
    test_seconds: float | None = None
    test_exit_code: int | None = None
    tests: int | None = None
    passed: int | None = None
    failures: int | None = None
    errors: int | None = None
    skipped: int | None = None
    coverage_percent: float | None = None
    covered_lines: int | None = None
    missing_lines: int | None = None
    error: str = ""


def write_result(result: Result, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "result.json").write_text(
        json.dumps(asdict(result), indent=2) + "\n", encoding="utf-8"
    )


def read_result(path: Path) -> Result:
    result = Result(**json.loads(path.read_text(encoding="utf-8")))
    for name, annotation in get_type_hints(Result).items():
        value = getattr(result, name)
        if isinstance(value, bool):
            raise ValueError(f"Invalid result field: {name}")
        if annotation == (float | None) and isinstance(value, int):
            continue
        if not isinstance(value, annotation) or (
            isinstance(value, float) and not math.isfinite(value)
        ):
            raise ValueError(f"Invalid result field: {name}")
    return result


def record_interpreter(result: Result) -> None:
    result.python_version = platform.python_version()
    result.interpreter_platform = sysconfig.get_platform()
    result.machine = platform.machine()
    result.pointer_bits = struct.calcsize("P") * 8
    result.os_release = platform.release()
    result.logical_cpus = os.cpu_count()


def verify_architecture(result: Result) -> None:
    aliases = {
        "amd64": "x64",
        "x86_64": "x64",
        "x64": "x64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }
    tag = result.interpreter_platform.lower()
    # Windows can report the host CPU even when Python runs under emulation.
    actual = (
        {"win-amd64": "x64", "win-arm64": "arm64"}.get(tag)
        if tag.startswith("win")
        else aliases.get(result.machine.lower())
    )
    if (
        actual != result.expected_architecture
        or result.pointer_bits != 64
        or aliases.get(result.runner_arch.lower()) != result.expected_architecture
    ):
        raise ValueError(
            f"Expected native {result.expected_architecture}; "
            f"interpreter={tag}, machine={result.machine}, "
            f"bits={result.pointer_bits}, runner={result.runner_arch}"
        )


def run_command(
    command: list[str], log: Path, env: dict[str, str]
) -> tuple[int, float]:
    started = time.perf_counter()
    with log.open("w", encoding="utf-8") as stream:
        try:
            with subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            ) as process:
                assert process.stdout is not None
                for line in process.stdout:
                    stream.write(line)
                    print(line, end="", flush=True)
                return_code = process.wait()
        except OSError as exc:
            message = f"Could not start {command[0]}: {exc}\n"
            stream.write(message)
            print(message, end="", file=sys.stderr)
            return_code = 127
    return return_code, round(time.perf_counter() - started, 3)


def run_platform(result: Result, output: Path) -> int:
    record_interpreter(result)
    write_result(result, output)
    try:
        verify_architecture(result)
    except ValueError as exc:
        result.status = "architecture_mismatch"
        result.error = str(exc)
        write_result(result, output)
        print(result.error, file=sys.stderr)
        return 1

    env = {**os.environ, "UV_PYTHON": sys.executable}
    result.setup_exit_code, result.dependency_setup_seconds = run_command(
        ["uv", "sync", "--locked", "--no-dev", "--group", "tests", "--extra", "all"],
        output / "dependencies.log",
        env,
    )
    if result.setup_exit_code != 0:
        result.status = "setup_failed"
        result.error = f"Dependency installation exited {result.setup_exit_code}."
        write_result(result, output)
        return 1
    write_result(result, output)

    return_code, elapsed = run_command(
        [
            "uv",
            "run",
            "--no-sync",
            "python",
            "scripts/platform_ci.py",
            "test",
            "--output",
            str(output),
        ],
        output / "tests.log",
        env,
    )
    result = read_result(output / "result.json")
    result.test_exit_code = return_code
    result.test_seconds = elapsed
    if result.status != "architecture_mismatch":
        result.status = "passed" if return_code == 0 else "tests_failed"
    if return_code != 0 and not result.error:
        result.error = (
            f"Test command exited {return_code}; see tests.log and junit.xml."
        )
    write_result(result, output)
    return int(return_code != 0)


def find_bash() -> str:
    if sys.platform == "win32":
        # A bare bash.exe can resolve to the System32 WSL launcher before PATH.
        if git := shutil.which("git"):
            bash = Path(git).parent.parent / "bin" / "bash.exe"
            if bash.is_file():
                return str(bash)
        raise FileNotFoundError("Git for Windows Bash was not found.")
    if bash_path := shutil.which("bash"):
        return bash_path
    raise FileNotFoundError("Bash was not found on PATH.")


def execute_tests(output: Path) -> int:
    import psutil

    result = read_result(output / "result.json")
    record_interpreter(result)
    result.physical_cpus = psutil.cpu_count(logical=False)
    result.memory_gib = round(psutil.virtual_memory().total / 1024**3, 2)
    result.workers = result.physical_cpus or result.logical_cpus or 1
    try:
        verify_architecture(result)
    except ValueError as exc:
        result.status = "architecture_mismatch"
        result.error = str(exc)
        write_result(result, output)
        print(result.error, file=sys.stderr)
        return 1
    write_result(result, output)
    bash = find_bash()
    return subprocess.call(
        [
            bash,
            "scripts/test-cov.sh",
            f"--junitxml={(output / 'junit.xml').as_posix()}",
            f"--cov-report=json:{(output / 'coverage.json').as_posix()}",
            f"--cov-report=xml:{(output / 'coverage.xml').as_posix()}",
            "--cov-report=term-missing:skip-covered",
            "--durations=20",
        ],
        env={
            **os.environ,
            "PATH": str(Path(bash).parent) + os.pathsep + os.environ["PATH"],
            "CONTEXT": f"{result.platform}-py{result.python_version}",
            "COVERAGE_FILE": str(output / ".coverage"),
            # Match xdist's psutil-based auto selection, and record the exact value.
            "PYTEST_XDIST_AUTO_NUM_WORKERS": str(result.workers),
        },
    )


def read_junit(result: Result, path: Path) -> None:
    root = ET.parse(path).getroot()
    if root.tag not in {"testsuite", "testsuites"}:
        raise ValueError("Unexpected JUnit root element.")
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    if not suites:
        raise ValueError("JUnit contains no test suites.")
    counts = {
        key: sum(int(suite.attrib[key]) for suite in suites)
        for key in ("tests", "failures", "errors", "skipped")
    }
    passed = counts["tests"] - sum(
        counts[key] for key in ("failures", "errors", "skipped")
    )
    if counts["tests"] <= 0 or min(*counts.values(), passed) < 0:
        raise ValueError("JUnit has empty or inconsistent test counts.")
    result.tests = counts["tests"]
    result.failures = counts["failures"]
    result.errors = counts["errors"]
    result.skipped = counts["skipped"]
    result.passed = passed


def read_coverage(result: Result, path: Path) -> None:
    totals = json.loads(path.read_text(encoding="utf-8"))["totals"]
    if (
        type(totals["percent_covered"]) not in (int, float)
        or type(totals["covered_lines"]) is not int
        or type(totals["missing_lines"]) is not int
    ):
        raise ValueError("Coverage totals must be numeric.")
    percent = float(totals["percent_covered"])
    covered = int(totals["covered_lines"])
    missing = int(totals["missing_lines"])
    if not 0 <= percent <= 100 or min(covered, missing) < 0 or covered + missing == 0:
        raise ValueError("Coverage totals are out of range.")
    result.coverage_percent = percent
    result.covered_lines = covered
    result.missing_lines = missing


def collect_result(
    source: Path, name: str, commit: str, run_id: str, attempt: str
) -> Result:
    output = source / f"platform-result-{name}-{attempt}"
    result = Result(platform=name, commit=commit, run_id=run_id, run_attempt=attempt)
    try:
        saved = read_result(output / "result.json")
        if (saved.platform, saved.commit, saved.run_id, saved.run_attempt) != (
            name,
            commit,
            run_id,
            attempt,
        ):
            raise ValueError(
                "Result does not belong to this platform, commit, or run attempt."
            )
        result = saved
        if result.status not in {
            "passed",
            "tests_failed",
            "setup_failed",
            "architecture_mismatch",
        }:
            raise ValueError("Platform did not finish recording its result.")
        if result.status == "passed":
            verify_architecture(result)
            if (
                result.setup_exit_code != 0
                or result.test_exit_code != 0
                or result.dependency_setup_seconds is None
                or result.test_seconds is None
                or result.dependency_setup_seconds < 0
                or result.test_seconds < 0
            ):
                raise ValueError(
                    "Successful result is missing successful command evidence."
                )
    except (OSError, ValueError, TypeError) as exc:
        result.status = "incomplete"
        result.error = str(exc)
        return result

    if result.test_exit_code is not None:
        for reader, filename in (
            (read_junit, "junit.xml"),
            (read_coverage, "coverage.json"),
        ):
            try:
                reader(result, output / filename)
            except (OSError, ValueError, KeyError, TypeError, ET.ParseError) as exc:
                result.error = f"{result.error} {filename}: {exc}".strip()
                if result.status == "passed":
                    result.status = "incomplete"
        if result.status == "passed" and (result.failures or result.errors):
            result.status = "tests_failed"
            result.error = "JUnit reports failures despite a successful command exit."
    return result


def cell(value: str | int | float | None) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value).replace("|", "\\|").replace("\n", " ")


def aggregate(
    source: Path,
    output: Path,
    commit: str,
    run_id: str,
    attempt: str,
    matrix_result: str,
) -> int:
    results = [
        collect_result(source, name, commit, run_id, attempt) for name in PLATFORMS
    ]
    success = matrix_result == "success" and all(r.status == "passed" for r in results)
    lines = [
        "# Platform CI reference report",
        "",
        f"Commit: `{commit}`. Run: `{run_id}`, attempt: `{attempt}`.",
        f"Overall: **{'passed' if success else 'failed / incomplete'}**. "
        f"Matrix job result: `{matrix_result}`.",
        "",
        "Reference only: one execution per platform on free standard GitHub-hosted "
        "runners with different hardware allocations. This is neither the HOBL "
        "build-plus-test baseline nor a controlled OS/CPU architecture benchmark.",
        "",
        "| Platform | Status | Tests | Passed | Failures | Errors | Skipped | Coverage % | Dependencies (s) | Tests (s) |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        values = (
            r.platform,
            r.status,
            r.tests,
            r.passed,
            r.failures,
            r.errors,
            r.skipped,
            r.coverage_percent,
            r.dependency_setup_seconds,
            r.test_seconds,
        )
        lines.append("| " + " | ".join(cell(v) for v in values) + " |")
    lines += [
        "",
        "Dependencies measures `uv sync --locked --no-dev --group tests --extra all` "
        "(including downloads/builds). Tests measures the test command, including "
        "interpreter/xdist startup and coverage reporting. Neither includes queueing, "
        "checkout, Python/uv setup actions, or artifact transfers. Dependency caching "
        "is disabled; hosted image/tool caches and network conditions can still differ.",
        "",
        "| Platform | Runner | Image version | OS release | Python | Interpreter / machine | CPUs logical / physical | Workers | RAM GiB |",
        "|---|---|---|---|---|---|---|---:|---:|",
    ]
    for r in results:
        environment = (
            r.platform,
            r.runner_label,
            f"{r.image_os} {r.image_version}".strip(),
            r.os_release,
            r.python_version,
            f"{r.interpreter_platform} / {r.machine}",
            f"{cell(r.logical_cpus)} / {cell(r.physical_cpus)}",
            r.workers,
            r.memory_gib,
        )
        lines.append("| " + " | ".join(cell(v) for v in environment) + " |")
    lines += [
        "",
        f"Download `platform-report-{attempt}` for `report.csv`, `report.json`, "
        "and this Markdown report. Per-platform evidence artifacts contain command "
        "logs, result metadata, JUnit XML, and coverage JSON/XML/database files.",
        "",
        "Coverage uses the upstream configuration (FastAPI, tests, and docs examples). "
        "This workflow does not replace the upstream combined 100%-coverage gate.",
    ]
    for r in results:
        if r.error:
            lines += ["", f"**{r.platform}:** {cell(r.error)}"]
    summary = "\n".join(lines) + "\n"
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.md").write_text(summary, encoding="utf-8")
    rows = [asdict(r) for r in results]
    with (output / "report.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=[f.name for f in fields(Result)])
        writer.writeheader()
        writer.writerows(rows)
    (output / "report.json").write_text(
        json.dumps(
            {"success": success, "matrix_result": matrix_result, "results": rows},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if summary_path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary_path).open("a", encoding="utf-8") as stream:
            stream.write(summary)
    print(summary)
    return int(not success)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run and report reference platform CI."
    )
    parser.add_argument("command", choices=("run", "test", "report"))
    parser.add_argument("--input", type=Path, default=Path("platform-artifacts"))
    parser.add_argument("--output", type=Path, default=Path("platform-report"))
    args = parser.parse_args()
    if args.command == "test":
        return execute_tests(args.output)
    if args.command == "report":
        return aggregate(
            args.input,
            args.output,
            os.environ["GITHUB_SHA"],
            os.environ["GITHUB_RUN_ID"],
            os.environ["GITHUB_RUN_ATTEMPT"],
            os.environ["MATRIX_RESULT"],
        )
    result = Result(
        platform=os.environ["PLATFORM"],
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
    return run_platform(result, Path("platform-results") / result.platform)


if __name__ == "__main__":
    sys.exit(main())
