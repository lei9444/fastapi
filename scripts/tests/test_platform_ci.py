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


def sample_result(name: str) -> platform_ci.Result:
    arm = name.endswith("arm64")
    architecture = "arm64" if arm else "x64"
    return platform_ci.Result(
        platform=name,
        status="passed",
        commit="test-commit",
        run_id="123",
        run_attempt="1",
        runner_arch=architecture.upper(),
        expected_architecture=architecture,
        interpreter_platform=f"win-{'arm64' if arm else 'amd64'}",
        machine="ARM64" if arm else "AMD64",
        pointer_bits=64,
        dependency_setup_seconds=1.25,
        setup_exit_code=0,
        test_seconds=3.5,
        test_exit_code=0,
    )


def write_evidence(root: Path, name: str) -> Path:
    output = root / f"platform-result-{name}-1"
    platform_ci.write_result(sample_result(name), output)
    (output / "junit.xml").write_text(
        '<testsuites><testsuite tests="4" failures="0" errors="0" skipped="1"/>'
        "</testsuites>",
        encoding="utf-8",
    )
    (output / "coverage.json").write_text(
        json.dumps(
            {
                "totals": {
                    "percent_covered": 97.5,
                    "covered_lines": 39,
                    "missing_lines": 1,
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
        write_evidence(tmp_path, name)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    output = tmp_path / "report"
    assert (
        platform_ci.aggregate(tmp_path, output, "test-commit", "123", "1", "success") == 0
    )
    with (output / "report.csv").open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert [r["platform"] for r in rows] == list(platform_ci.PLATFORMS)
    assert all(r["passed"] == "3" and r["skipped"] == "1" for r in rows)
    assert all(r["coverage_percent"] == "97.5" for r in rows)
    assert all(
        r["dependency_setup_seconds"] == "1.25" and r["test_seconds"] == "3.5"
        for r in rows
    )
    text = summary.read_text(encoding="utf-8")
    assert text == (output / "report.md").read_text(encoding="utf-8")
    assert "Reference only" in text
    assert "HOBL" in text
    assert (
        json.loads((output / "report.json").read_text(encoding="utf-8"))["success"] is True
    )


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
    collected = platform_ci.collect_result(tmp_path, "linux-x64", "test-commit", "123", "1")
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
    result = platform_ci.collect_result(tmp_path, "linux-x64", "test-commit", "123", "1")
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
    collected = platform_ci.collect_result(tmp_path, "linux-x64", "test-commit", "123", "1")
    assert collected.status == "tests_failed"
    assert (collected.tests, collected.passed, collected.failures, collected.errors) == (
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
    collected = platform_ci.collect_result(tmp_path, "linux-x64", "test-commit", "123", "1")
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
    output = tmp_path / "platform-result-windows-arm64-1"
    result = platform_ci.Result(
        platform="windows-arm64",
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
        write_evidence(tmp_path, name)
    assert (
        platform_ci.aggregate(
            tmp_path, tmp_path / "report", "test-commit", "123", "1", matrix_result
        )
        == 1
    )


def test_missing_artifacts_still_publish_all_platforms(tmp_path: Path):
    output = tmp_path / "report"
    assert (
        platform_ci.aggregate(tmp_path, output, "test-commit", "123", "1", "failure") == 1
    )
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["success"] is False
    assert len(report["results"]) == 4
    assert all(
        r["status"] == "incomplete" and r["tests"] is None for r in report["results"]
    )


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
        "uv", "sync", "--locked", "--no-dev", "--group", "tests", "--extra", "all"
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
    assert platform_ci.read_result(tmp_path / "result.json").status == "architecture_mismatch"
    assert not (tmp_path / "dependencies.log").exists()
