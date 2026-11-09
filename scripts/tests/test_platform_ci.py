import csv
import json
import os
import sys
from pathlib import Path

import pytest

from scripts import platform_ci


@pytest.fixture(autouse=True)
def isolate_job_summary(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)


def sample_result(name: str, repetition: int = 1) -> platform_ci.Result:
    arm = name.endswith("arm64")
    architecture = "arm64" if arm else "x64"
    return platform_ci.Result(
        platform=name,
        repetition=repetition,
        status="passed",
        commit="test-commit",
        run_id="123",
        run_attempt="1",
        runner_arch=architecture.upper(),
        expected_architecture=architecture,
        interpreter_platform=f"win-{'arm64' if arm else 'amd64'}",
        machine="ARM64" if arm else "AMD64",
        pointer_bits=64,
        dependency_setup_seconds=1.25 * repetition,
        setup_exit_code=0,
        build_seconds=0.75 * repetition,
        build_exit_code=0,
        build_backend_version="2.5.0",
        test_seconds=3.5 * repetition,
        test_exit_code=0,
    )


def write_evidence(root: Path, name: str, repetition: int = 1) -> Path:
    output = root / f"platform-result-{name}-{repetition}-1"
    platform_ci.write_result(sample_result(name, repetition), output)
    (output / "dist").mkdir()
    (output / "dist" / "fastapi-1.0-py3-none-any.whl").write_bytes(b"wheel")
    (output / "dist" / "fastapi-1.0.tar.gz").write_bytes(b"sdist")
    (output / "junit.xml").write_text(
        '<testsuites><testsuite tests="4" failures="0" errors="0" skipped="1"/>'
        "</testsuites>",
        encoding="utf-8",
    )
    (output / "coverage.json").write_text(
        json.dumps(
            {
                "totals": {
                    "percent_covered": 100 - 2.5 * repetition,
                    "covered_lines": 40 - repetition,
                    "missing_lines": repetition,
                }
            }
        ),
        encoding="utf-8",
    )
    return output


@pytest.mark.parametrize(
    ("tag", "machine", "runner", "expected"),
    [
        ("win-arm64", "ARM64", "ARM64", "arm64"),
        ("win-amd64", "AMD64", "X64", "x64"),
        ("macosx-15.0-arm64", "arm64", "ARM64", "arm64"),
        ("linux-x86_64", "x86_64", "X64", "x64"),
    ],
)
def test_native_architectures(tag: str, machine: str, runner: str, expected: str):
    result = platform_ci.Result(
        platform="test",
        repetition=1,
        interpreter_platform=tag,
        machine=machine,
        runner_arch=runner,
        expected_architecture=expected,
        pointer_bits=64,
    )
    platform_ci.verify_architecture(result)


@pytest.mark.parametrize(
    ("tag", "machine", "runner", "bits"),
    [
        ("win-amd64", "ARM64", "ARM64", 64),
        ("win-arm64", "ARM64", "X64", 64),
        ("win-arm64", "ARM64", "ARM64", 32),
        ("macosx-15.0-universal2", "x86_64", "ARM64", 64),
    ],
)
def test_rejects_emulated_or_wrong_architecture(
    tag: str, machine: str, runner: str, bits: int
):
    result = sample_result("windows-arm64")
    result.interpreter_platform = tag
    result.machine = machine
    result.runner_arch = runner
    result.pointer_bits = bits
    with pytest.raises(ValueError, match="Expected native arm64"):
        platform_ci.verify_architecture(result)


def test_successful_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for name in platform_ci.PLATFORMS:
        for repetition in platform_ci.REPETITIONS:
            write_evidence(tmp_path, name, repetition)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    output = tmp_path / "report"
    assert (
        platform_ci.aggregate(tmp_path, output, "test-commit", "123", "1", "success")
        == 0
    )
    with (output / "report.csv").open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert [r["platform"] for r in rows] == list(platform_ci.PLATFORMS)
    assert all(r["successful_runs"] == r["expected_runs"] == "3" for r in rows)
    assert all(r["mean_coverage_percent"] == "95.0" for r in rows)
    assert all(
        r["mean_dependency_setup_seconds"] == "2.5"
        and r["mean_build_seconds"] == "1.5"
        and r["mean_test_seconds"] == "7.0"
        for r in rows
    )
    with (output / "runs.csv").open(newline="", encoding="utf-8") as stream:
        runs = list(csv.DictReader(stream))
    assert [(r["platform"], r["repetition"]) for r in runs] == [
        (name, str(repetition))
        for name in platform_ci.PLATFORMS
        for repetition in platform_ci.REPETITIONS
    ]
    assert all(r["passed"] == "3" and r["skipped"] == "1" for r in runs)
    assert [r["test_seconds"] for r in runs[:3]] == ["3.5", "7.0", "10.5"]
    text = summary.read_text(encoding="utf-8")
    assert text == (output / "report.md").read_text(encoding="utf-8")
    assert "Reference only" in text
    assert "HOBL" in text
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["success"] is True
    assert report["repetitions_per_platform"] == 3
    assert len(report["averages"]) == 4
    assert len(report["results"]) == 12


@pytest.mark.parametrize("filename", ["result.json", "junit.xml", "coverage.json"])
def test_missing_evidence_fails_report(tmp_path: Path, filename: str):
    output = write_evidence(tmp_path, "windows-arm64")
    (output / filename).unlink()
    result = platform_ci.collect_result(
        tmp_path, "windows-arm64", "test-commit", "123", "1"
    )
    assert result.status == "incomplete"
    assert filename in result.error


@pytest.mark.parametrize("filename", ["result.json", "junit.xml", "coverage.json"])
def test_malformed_evidence_is_explicit(tmp_path: Path, filename: str):
    output = write_evidence(tmp_path, "windows-arm64")
    (output / filename).write_text("not valid", encoding="utf-8")
    result = platform_ci.collect_result(
        tmp_path, "windows-arm64", "test-commit", "123", "1"
    )
    assert result.status == "incomplete"
    assert result.error


@pytest.mark.parametrize("field", ["platform", "commit", "run_id", "run_attempt"])
def test_rejects_mismatched_results(tmp_path: Path, field: str):
    output = write_evidence(tmp_path, "linux-x64")
    result = sample_result("linux-x64")
    setattr(result, field, "different")
    platform_ci.write_result(result, output)
    collected = platform_ci.collect_result(
        tmp_path, "linux-x64", "test-commit", "123", "1"
    )
    assert collected.status == "incomplete"
    assert "does not belong" in collected.error


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("test_seconds", "3.5"),
        ("test_seconds", float("nan")),
        ("test_exit_code", False),
        ("runner_arch", []),
        ("error", {}),
    ],
)
def test_invalid_result_field_types_fail(tmp_path: Path, field: str, value: object):
    output = write_evidence(tmp_path, "linux-x64")
    path = output / "result.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data[field] = value
    path.write_text(json.dumps(data), encoding="utf-8")
    result = platform_ci.collect_result(
        tmp_path, "linux-x64", "test-commit", "123", "1"
    )
    assert result.status == "incomplete"
    assert field in result.error


def test_failed_tests_keep_counts_and_coverage(tmp_path: Path):
    output = write_evidence(tmp_path, "linux-x64")
    result = sample_result("linux-x64")
    result.status = "tests_failed"
    result.test_exit_code = 1
    platform_ci.write_result(result, output)
    (output / "junit.xml").write_text(
        '<testsuite tests="5" failures="1" errors="1" skipped="1"/>', encoding="utf-8"
    )
    collected = platform_ci.collect_result(
        tmp_path, "linux-x64", "test-commit", "123", "1"
    )
    assert collected.status == "tests_failed"
    assert (
        collected.tests,
        collected.passed,
        collected.failures,
        collected.errors,
    ) == (
        5,
        2,
        1,
        1,
    )
    assert collected.coverage_percent == 97.5


def test_junit_failure_overrides_zero_exit_code(tmp_path: Path):
    output = write_evidence(tmp_path, "linux-x64")
    (output / "junit.xml").write_text(
        '<testsuite tests="2" failures="1" errors="0" skipped="0"/>', encoding="utf-8"
    )
    collected = platform_ci.collect_result(
        tmp_path, "linux-x64", "test-commit", "123", "1"
    )
    assert collected.status == "tests_failed"
    assert "despite a successful command exit" in collected.error


@pytest.mark.parametrize(
    "xml",
    [
        "<testsuites/>",
        '<testsuite tests="0" failures="0" errors="0" skipped="0"/>',
        '<testsuite tests="1" failures="2" errors="0" skipped="0"/>',
        "<testsuite/>",
    ],
)
def test_invalid_junit_counts_are_not_success(tmp_path: Path, xml: str):
    output = write_evidence(tmp_path, "linux-x64")
    (output / "junit.xml").write_text(xml, encoding="utf-8")
    assert (
        platform_ci.collect_result(
            tmp_path, "linux-x64", "test-commit", "123", "1"
        ).status
        == "incomplete"
    )


def test_failed_setup_does_not_invent_test_results(tmp_path: Path):
    output = tmp_path / "platform-result-windows-arm64-1-1"
    result = platform_ci.Result(
        platform="windows-arm64",
        repetition=1,
        status="setup_failed",
        commit="test-commit",
        run_id="123",
        run_attempt="1",
        setup_exit_code=1,
        dependency_setup_seconds=5,
        error="Dependency installation exited 1.",
    )
    platform_ci.write_result(result, output)
    collected = platform_ci.collect_result(
        tmp_path, "windows-arm64", "test-commit", "123", "1"
    )
    assert collected.status == "setup_failed"
    assert collected.tests is None
    assert collected.coverage_percent is None
    assert collected.test_seconds is None


@pytest.mark.parametrize("matrix_result", ["failure", "cancelled", "skipped"])
def test_matrix_failure_is_not_hidden(tmp_path: Path, matrix_result: str):
    for name in platform_ci.PLATFORMS:
        for repetition in platform_ci.REPETITIONS:
            write_evidence(tmp_path, name, repetition)
    assert (
        platform_ci.aggregate(
            tmp_path, tmp_path / "report", "test-commit", "123", "1", matrix_result
        )
        == 1
    )


def test_missing_artifacts_still_publish_all_platforms(tmp_path: Path):
    output = tmp_path / "report"
    assert (
        platform_ci.aggregate(tmp_path, output, "test-commit", "123", "1", "failure")
        == 1
    )
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["success"] is False
    assert len(report["results"]) == 12
    assert all(
        r["status"] == "incomplete" and r["tests"] is None for r in report["results"]
    )
    assert len(report["averages"]) == 4
    assert all(
        r["status"] == "incomplete"
        and r["successful_runs"] == 0
        and r["expected_runs"] == 3
        and r["mean_test_seconds"] is None
        for r in report["averages"]
    )


@pytest.mark.parametrize(
    "failure",
    ["missing", "build_failed", "tests_failed", "setup_failed", "wrong_repetition"],
)
def test_incomplete_platform_does_not_average_only_successes(
    tmp_path: Path, failure: str
):
    for name in platform_ci.PLATFORMS:
        for repetition in platform_ci.REPETITIONS:
            write_evidence(tmp_path, name, repetition)
    evidence = tmp_path / "platform-result-linux-x64-2-1"
    if failure == "missing":
        (evidence / "result.json").unlink()
    else:
        result = sample_result("linux-x64", 2)
        if failure == "wrong_repetition":
            result.repetition = 1
        elif failure == "setup_failed":
            result.status = "setup_failed"
            result.setup_exit_code = 1
            result.build_exit_code = None
            result.build_seconds = None
            result.test_exit_code = None
            result.test_seconds = None
        elif failure == "build_failed":
            result.status = "build_failed"
            result.build_exit_code = 1
            result.test_exit_code = None
            result.test_seconds = None
        else:
            result.status = "tests_failed"
            result.test_exit_code = 1
        platform_ci.write_result(result, evidence)
    output = tmp_path / "report"
    assert (
        platform_ci.aggregate(tmp_path, output, "test-commit", "123", "1", "failure")
        == 1
    )
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    averages = {row["platform"]: row for row in report["averages"]}
    affected = averages["linux-x64"]
    assert affected["status"] == "incomplete"
    assert affected["successful_runs"] == 2
    assert affected["expected_runs"] == 3
    assert affected["mean_dependency_setup_seconds"] is None
    assert affected["mean_build_seconds"] is None
    assert affected["mean_test_seconds"] is None
    assert affected["mean_coverage_percent"] is None
    assert averages["macos-arm64"]["mean_test_seconds"] == 7.0
    with (output / "report.csv").open(newline="", encoding="utf-8") as stream:
        rows = {row["platform"]: row for row in csv.DictReader(stream)}
    assert rows["linux-x64"]["mean_test_seconds"] == ""


@pytest.mark.parametrize("pattern", ["*.whl", "*.tar.gz"])
@pytest.mark.parametrize("damage", ["missing", "empty", "duplicate"])
def test_invalid_build_artifacts_are_explicit(
    tmp_path: Path, pattern: str, damage: str
):
    output = write_evidence(tmp_path, "linux-x64")
    artifact = next((output / "dist").glob(pattern))
    if damage == "missing":
        artifact.unlink()
    elif damage == "empty":
        artifact.write_bytes(b"")
    else:
        artifact.with_name("another-" + artifact.name).write_bytes(b"duplicate")
    result = platform_ci.collect_result(
        tmp_path, "linux-x64", "test-commit", "123", "1"
    )
    assert result.status == "incomplete"
    assert pattern in result.error
    assert "Build artifacts" in result.error


def test_build_failure_stops_before_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    calls: list[list[str]] = []
    outcomes = iter([(0, 1.0), (0, 0.5), (7, 3.0)])

    def command(command: list[str], log: Path, env: dict[str, str]):
        calls.append(command)
        return next(outcomes)

    monkeypatch.setattr(platform_ci, "record_interpreter", lambda result: None)
    monkeypatch.setattr(platform_ci, "run_command", command)
    initial = sample_result("windows-arm64")
    initial.test_exit_code = None
    initial.test_seconds = None
    assert platform_ci.run_platform(initial, tmp_path) == 1
    result = platform_ci.read_result(tmp_path / "result.json")
    assert result.status == "build_failed"
    assert result.setup_exit_code == 0
    assert result.dependency_setup_seconds == 1.5
    assert result.build_exit_code == 7
    assert result.build_seconds == 3.0
    assert result.test_exit_code is None
    assert len(calls) == 3
    assert calls[1][-1] == ".github/platform-build-requirements.txt"
    assert calls[2][5] == "build"


def test_build_is_offline_with_prepared_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    output = write_evidence(tmp_path, "windows-arm64")
    calls: list[list[str]] = []

    def command(command: list[str]):
        calls.append(command)
        return 0

    monkeypatch.setattr(platform_ci, "record_interpreter", lambda result: None)
    monkeypatch.setattr(platform_ci, "version", lambda package: "2.5.0")
    monkeypatch.setattr(platform_ci.subprocess, "call", command)
    assert platform_ci.execute_build(output) == 0
    assert calls == [
        [
            "uv",
            "build",
            "--sdist",
            "--wheel",
            "--no-build-isolation",
            "--offline",
            "--python",
            sys.executable,
            "--out-dir",
            str(output / "dist"),
        ]
    ]
    assert (
        platform_ci.read_result(output / "result.json").build_backend_version
        == "2.5.0"
    )


def test_repetition_identity_is_required(tmp_path: Path):
    output = write_evidence(tmp_path, "linux-x64")
    path = output / "result.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["repetition"]
    path.write_text(json.dumps(data), encoding="utf-8")
    result = platform_ci.collect_result(
        tmp_path, "linux-x64", "test-commit", "123", "1"
    )
    assert result.status == "incomplete"
    assert "repetition" in result.error


def test_command_failure_records_duration_and_log(tmp_path: Path):
    log = tmp_path / "command.log"
    code, elapsed = platform_ci.run_command(
        [sys.executable, "-c", "print('test command'); raise SystemExit(7)"],
        log,
        dict(os.environ),
    )
    assert code == 7
    assert elapsed >= 0
    assert "test command" in log.read_text(encoding="utf-8")


def test_command_not_found_is_explicit(tmp_path: Path):
    log = tmp_path / "command.log"
    code, _ = platform_ci.run_command(
        [str(tmp_path / "missing-command")], log, dict(os.environ)
    )
    assert code == 127
    assert "Could not start" in log.read_text(encoding="utf-8")


def test_setup_failure_stops_before_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    calls: list[list[str]] = []

    def command(command: list[str], log: Path, env: dict[str, str]):
        calls.append(command)
        assert env["UV_PYTHON"] == sys.executable
        return 2, 1.5

    monkeypatch.setattr(platform_ci, "record_interpreter", lambda result: None)
    monkeypatch.setattr(platform_ci, "run_command", command)
    assert platform_ci.run_platform(sample_result("windows-arm64"), tmp_path) == 1
    assert len(calls) == 1
    assert calls[0] == [
        "uv",
        "sync",
        "--locked",
        "--no-dev",
        "--group",
        "tests",
        "--extra",
        "all",
    ]
    result = platform_ci.read_result(tmp_path / "result.json")
    assert result.status == "setup_failed"
    assert result.dependency_setup_seconds == 1.5


def test_architecture_failure_stops_before_installation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    result = sample_result("windows-arm64")
    result.interpreter_platform = "win-amd64"
    monkeypatch.setattr(platform_ci, "record_interpreter", lambda result: None)
    assert platform_ci.run_platform(result, tmp_path) == 1
    assert (
        platform_ci.read_result(tmp_path / "result.json").status
        == "architecture_mismatch"
    )
    assert not (tmp_path / "dependencies.log").exists()


def test_windows_uses_git_bash_not_wsl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    git = tmp_path / "Git" / "cmd" / "git.exe"
    bash = tmp_path / "Git" / "bin" / "bash.exe"
    bash.parent.mkdir(parents=True)
    bash.touch()
    executables = {"git": str(git), "bash": "C:\\Windows\\System32\\bash.exe"}
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(platform_ci.shutil, "which", executables.get)
    assert platform_ci.find_bash() == str(bash)


@pytest.mark.parametrize("git", [None, "missing-git.exe"])
def test_windows_missing_git_bash_is_explicit(
    monkeypatch: pytest.MonkeyPatch, git: str | None
):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(platform_ci.shutil, "which", lambda name: git)
    with pytest.raises(FileNotFoundError, match="Git for Windows Bash"):
        platform_ci.find_bash()


def test_posix_bash_lookup(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(platform_ci.shutil, "which", lambda name: "/bin/bash")
    assert platform_ci.find_bash() == "/bin/bash"
    monkeypatch.setattr(platform_ci.shutil, "which", lambda name: None)
    with pytest.raises(FileNotFoundError, match="Bash was not found"):
        platform_ci.find_bash()
