import csv
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import isolated_platform_ci as isolated
from scripts import platform_ci as common

BUILD_LOG = """* Creating isolated environment: venv+pip...
* Installing packages in isolated environment:
  - pdm-backend
* Installed build dependency versions:
  - pdm-backend==2.5.0
* Building sdist...
* Building wheel from sdist
* Creating isolated environment: venv+pip...
* Installing packages in isolated environment:
  - pdm-backend
* Installed build dependency versions:
  - pdm-backend==2.5.0
"""


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.delenv("PIP_NO_INDEX", raising=False)


def sample_result(name: str = "linux-x64", iteration: int = 0) -> common.Result:
    arm = name.endswith("arm64")
    architecture = "arm64" if arm else "x64"
    return common.Result(
        platform=name,
        repetition=iteration,
        commit="commit",
        run_id="123",
        run_attempt="1",
        status="setup_succeeded" if iteration == 0 else "passed",
        runner_arch=architecture.upper(),
        expected_architecture=architecture,
        interpreter_platform="win-arm64" if arm else "win-amd64",
        machine="ARM64" if arm else "AMD64",
        python_version="3.13.17",
        pointer_bits=64,
        build_backend_version="2.5.0",
        dependency_setup_seconds=12.5 if iteration == 0 else None,
        setup_exit_code=0 if iteration == 0 else None,
        build_seconds=float(iteration) if iteration else None,
        build_exit_code=0 if iteration else None,
        test_seconds=4.0 * iteration if iteration else None,
        test_exit_code=0 if iteration else None,
    )


def sample_audit(loops: int) -> dict[str, object]:
    digest = hashlib.sha256(b"backend").hexdigest()
    return {
        "loops": loops,
        "tools": isolated.TOOLS.copy(),
        "python_version": "3.13.17",
        "fastapi_version": "0.162.2",
        "workload_venv": True,
        "pip_in_venv": True,
        "default_installer": "pip",
        "environment_backend": "venv+pip",
        "outer_pip": True,
        "creates_with_pip": False,
        "wheelhouse": {"pdm_backend-2.5.0-py3-none-any.whl": digest},
    }


def write_build(folder: Path) -> None:
    (folder / "dist").mkdir(parents=True, exist_ok=True)
    (folder / "dist" / "fastapi-1.0-py3-none-any.whl").write_bytes(b"wheel")
    (folder / "dist" / "fastapi-1.0.tar.gz").write_bytes(b"sdist")
    (folder / "build.log").write_text(BUILD_LOG, encoding="utf-8")


def write_test_evidence(folder: Path, failures: int = 0) -> None:
    (folder / "junit.xml").write_text(
        f'<testsuite tests="4" failures="{failures}" errors="0" skipped="1"/>',
        encoding="utf-8",
    )
    isolated.write_json(
        folder / "coverage.json",
        {"totals": {"percent_covered": 97.5, "covered_lines": 39, "missing_lines": 1}},
    )


def write_platform(
    root: Path,
    name: str,
    loops: int,
    indices: tuple[int, ...] | None = None,
) -> Path:
    folder = root / f"isolated-result-{name}-1"
    isolated.write_json(folder / "setup.json", asdict(sample_result(name)))
    isolated.write_json(folder / "audit.json", sample_audit(loops))
    for iteration in range(1, loops + 1) if indices is None else indices:
        target = folder / f"loop-{iteration}"
        common.write_result(sample_result(name, iteration), target)
        write_build(target)
        write_test_evidence(target)
    return folder


@pytest.mark.parametrize("raw", [None, "", "2", "0"])
def test_push_uses_default_three(raw: str | None):
    assert isolated.resolve_loops("push", raw) == 3


@pytest.mark.parametrize(
    ("raw", "expected"), [("3", 3), ("2", 2), (" 02 ", 2), (str(2**63), 2**63)]
)
def test_manual_positive_integer(raw: str, expected: int):
    assert isolated.resolve_loops("workflow_dispatch", raw) == expected


@pytest.mark.parametrize(
    "raw", [None, "", " ", "0", "-1", "2.0", "1.5", "2e0", "NaN", "true", "0x2"]
)
def test_invalid_manual_input_never_falls_back(raw: str | None):
    with pytest.raises(ValueError, match="positive integer"):
        isolated.resolve_loops("workflow_dispatch", raw)


def test_unknown_event_is_explicit():
    with pytest.raises(ValueError, match="Unsupported workflow event"):
        isolated.resolve_loops("schedule", "3")


def test_invalid_input_fails_before_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("LOOPS", "0")
    assert isolated.main(["prepare", "--output", str(tmp_path)]) == 1
    data = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert data["success"] is False
    assert "positive integer" in data["error"]
    assert not (tmp_path / "setup.json").exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("loops", 0),
        ("loops", "3"),
        ("python_version", "3.12.10"),
        ("workload_venv", False),
        ("pip_in_venv", False),
        ("default_installer", "uv"),
        ("environment_backend", "virtualenv+pip"),
        ("outer_pip", False),
        ("outer_pip", None),
        ("creates_with_pip", True),
        ("tools", {"build": "1.6.1", "pip": "26.0", "pdm-backend": "2.5.0"}),
        ("wheelhouse", {}),
    ],
)
def test_audit_rejects_unexpected_build_paths(field: str, value: object):
    audit = sample_audit(3)
    audit[field] = value
    with pytest.raises(ValueError):
        isolated.validate_audit(audit, 3)


def test_audit_accepts_verified_outer_pip_path():
    assert isolated.validate_audit(sample_audit(2), 2)["creates_with_pip"] is False


def install_fake_commands(
    monkeypatch: pytest.MonkeyPatch,
    output: Path,
    wheelhouse: Path,
    loops: int,
    fail_stage: str = "",
) -> list[list[str]]:
    calls: list[list[str]] = []
    ticks = iter([5.0, 17.5])
    monkeypatch.setattr(
        isolated, "time", SimpleNamespace(perf_counter=lambda: next(ticks))
    )
    monkeypatch.setattr(common, "record_interpreter", lambda result: None)

    def command(args: list[str], log: Path, env: dict[str, str]):
        calls.append(args)
        if args[1:3] == ["-m", "build"]:
            folder = Path(args[-1]).parent
            iteration = int(folder.name.removeprefix("loop-"))
            assert args == [
                str(isolated.workload_python()),
                "-m",
                "build",
                "--outdir",
                str(folder / "dist"),
            ]
            assert env["PIP_NO_INDEX"] == "1"
            assert env["PIP_FIND_LINKS"] == str(wheelhouse.resolve())
            write_build(folder)
            code = 1 if fail_stage == "build" and iteration == 2 else 0
            return code, float(iteration)
        if args[:2] == ["uv", "run"]:
            folder = Path(args[-1])
            iteration = int(folder.name.removeprefix("loop-"))
            assert args[4:7] == ["scripts/platform_ci.py", "test", "--output"]
            assert "PIP_NO_INDEX" not in env
            failed = fail_stage == "test" and iteration == 2
            write_test_evidence(folder, failures=int(failed))
            return int(failed), 4.0 * iteration
        if "download" in args:
            assert "--only-binary=:all:" in args
            assert args[-1] == "pdm-backend==2.5.0"
            wheelhouse.mkdir(parents=True, exist_ok=True)
            (wheelhouse / "pdm_backend-2.5.0-py3-none-any.whl").write_bytes(b"backend")
        if "audit" in args:
            isolated.write_json(output / "audit.json", sample_audit(loops))
        return 0, 0.1

    monkeypatch.setattr(common, "run_command", command)
    return calls


def test_preparation_failure_stops_before_loops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    calls: list[list[str]] = []
    ticks = iter([1.0, 3.0])
    monkeypatch.setattr(
        isolated, "time", SimpleNamespace(perf_counter=lambda: next(ticks))
    )
    monkeypatch.setattr(common, "record_interpreter", lambda result: None)

    def command(args: list[str], log: Path, env: dict[str, str]):
        calls.append(args)
        return 5, 2.0

    monkeypatch.setattr(common, "run_command", command)
    assert isolated.prepare(sample_result(), 3, tmp_path, tmp_path / "wheels") == 1
    result = common.read_result(tmp_path / "setup.json")
    assert result.status == "setup_failed"
    assert result.setup_exit_code == 5
    assert result.dependency_setup_seconds == 2.0
    assert len(calls) == 1
    assert list(tmp_path.glob("loop-*")) == []


def test_preparation_rejects_an_audit_for_different_loops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    wheelhouse = tmp_path / "wheelhouse"
    install_fake_commands(monkeypatch, tmp_path, wheelhouse, 3)
    assert isolated.prepare(sample_result(), 2, tmp_path, wheelhouse) == 1
    setup = common.read_result(tmp_path / "setup.json")
    assert setup.status == "setup_failed"
    assert "loops" in setup.error


@pytest.mark.parametrize("loops", [2, 3])
def test_sequential_loops_prepare_once_and_keep_distinct_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, loops: int
):
    output = tmp_path / "output"
    wheelhouse = tmp_path / "wheelhouse"
    calls = install_fake_commands(monkeypatch, output, wheelhouse, loops)
    expected = sample_result()
    assert isolated.prepare(expected, loops, output, wheelhouse) == 0
    assert isolated.run_loops(expected, loops, output, wheelhouse) == 0
    assert len(calls) == 4 + 2 * loops
    assert sum(args[:2] == ["uv", "sync"] for args in calls) == 1
    assert "--only-binary=:all:" not in calls[0]
    assert sum("download" in args for args in calls) == 1
    setup = common.read_result(output / "setup.json")
    assert setup.dependency_setup_seconds == 12.5
    for iteration in range(1, loops + 1):
        assert calls[4 + 2 * (iteration - 1)][1:3] == ["-m", "build"]
        assert calls[5 + 2 * (iteration - 1)][:2] == ["uv", "run"]
        result = common.read_result(output / f"loop-{iteration}" / "result.json")
        assert result.repetition == iteration and result.status == "passed"
        assert result.dependency_setup_seconds is None
        assert result.setup_exit_code is None
        assert result.build_seconds == iteration
        assert result.test_seconds == 4 * iteration


@pytest.mark.parametrize("stage", ["build", "test"])
def test_failed_loop_does_not_discard_later_loops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
):
    output = tmp_path / "output"
    wheelhouse = tmp_path / "wheelhouse"
    calls = install_fake_commands(monkeypatch, output, wheelhouse, 3, stage)
    expected = sample_result()
    assert isolated.prepare(expected, 3, output, wheelhouse) == 0
    assert isolated.run_loops(expected, 3, output, wheelhouse) == 1
    assert sum(args[1:3] == ["-m", "build"] for args in calls) == 3
    second = common.read_result(output / "loop-2" / "result.json")
    assert second.status == ("build_failed" if stage == "build" else "tests_failed")
    assert common.read_result(output / "loop-3" / "result.json").status == "passed"
    if stage == "build":
        assert second.test_exit_code is None
        assert sum(args[:2] == ["uv", "run"] for args in calls) == 2


@pytest.mark.parametrize(
    "old",
    [
        "Creating isolated environment: venv+pip",
        "- pdm-backend==2.5.0",
        "Building wheel from sdist",
    ],
)
def test_build_log_must_prove_the_default_isolated_path(tmp_path: Path, old: str):
    write_build(tmp_path)
    (tmp_path / "build.log").write_text(
        BUILD_LOG.replace(old, "unexpected"), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="two venv\\+pip"):
        isolated.validate_build(tmp_path)


@pytest.mark.parametrize("loops", [2, 3])
def test_report_means_and_one_time_preparation(tmp_path: Path, loops: int):
    for name in common.PLATFORMS:
        write_platform(tmp_path, name, loops)
    output = tmp_path / "report"
    assert (
        isolated.aggregate(tmp_path, output, "commit", "123", "1", loops, "success")
        == 0
    )
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["success"] is True
    assert len(report["platforms"]) == 4
    assert len(report["runs"]) == 4 * loops
    for summary in report["platforms"]:
        assert summary["successful_loops"] == loops
        assert summary["setup_seconds"] == 12.5
        assert summary["mean_build_seconds"] == (loops + 1) / 2
        assert summary["mean_test_seconds"] == 2 * (loops + 1)
        assert summary["missing_loops"] == ""
    with (output / "runs.csv").open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        assert reader.fieldnames is not None
        assert isolated.SETUP_FIELDS.isdisjoint(reader.fieldnames)
        assert len(list(reader)) == 4 * loops


@pytest.mark.parametrize(
    "failure", ["missing", "failed", "malformed", "wrong_commit", "repeated_setup"]
)
def test_failure_or_missing_loop_blocks_only_its_platform_mean(
    tmp_path: Path, failure: str
):
    for name in common.PLATFORMS:
        write_platform(
            tmp_path,
            name,
            3,
            (1, 3) if name == "linux-x64" and failure == "missing" else None,
        )
    folder = tmp_path / "isolated-result-linux-x64-1" / "loop-2"
    if failure == "malformed":
        (folder / "result.json").write_text("bad JSON", encoding="utf-8")
    elif failure != "missing":
        result = sample_result("linux-x64", 2)
        if failure == "failed":
            result.status = "tests_failed"
            result.test_exit_code = 1
        elif failure == "wrong_commit":
            result.commit = "different"
        else:
            result.dependency_setup_seconds = 12.5
        common.write_result(result, folder)
    output = tmp_path / "report"
    assert isolated.aggregate(tmp_path, output, "commit", "123", "1", 3, "failure") == 1
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    summaries = {row["platform"]: row for row in report["platforms"]}
    assert summaries["linux-x64"]["successful_loops"] == 2
    assert summaries["linux-x64"]["mean_build_seconds"] is None
    assert summaries["linux-x64"]["mean_test_seconds"] is None
    assert summaries["macos-arm64"]["mean_test_seconds"] == 8
    if failure == "missing":
        assert summaries["linux-x64"]["missing_loops"] == "2"


def test_setup_failure_preserves_its_single_measurement(tmp_path: Path):
    root = write_platform(tmp_path, "linux-x64", 3, ())
    setup = sample_result()
    setup.status = "setup_failed"
    setup.setup_exit_code = 1
    setup.error = "Backend wheel download failed."
    isolated.write_json(root / "setup.json", asdict(setup))
    summary, runs = isolated.collect_platform(tmp_path, sample_result(), 3)
    assert summary.setup_seconds == 12.5
    assert summary.setup_exit_code == 1
    assert summary.missing_loops == "1-3"
    assert summary.mean_build_seconds is None
    assert "Backend wheel download failed" in summary.error
    assert runs == []


def test_large_loop_count_has_bounded_missing_reporting(tmp_path: Path):
    loops = 10**12
    write_platform(tmp_path, "linux-x64", loops, (1, 2))
    summary, runs = isolated.collect_platform(tmp_path, sample_result(), loops)
    assert summary.loops == loops
    assert summary.missing_loops == f"3-{loops}"
    assert len(runs) == 2
    assert summary.mean_test_seconds is None


def test_matrix_failure_is_not_hidden(tmp_path: Path):
    for name in common.PLATFORMS:
        write_platform(tmp_path, name, 2)
    assert (
        isolated.aggregate(
            tmp_path, tmp_path / "report", "commit", "123", "1", 2, "failure"
        )
        == 1
    )


def test_old_attempt_is_not_reused(tmp_path: Path):
    write_platform(tmp_path, "linux-x64", 3)
    expected = sample_result()
    expected.run_attempt = "2"
    summary, runs = isolated.collect_platform(tmp_path, expected, 3)
    assert summary.missing_loops == "1-3"
    assert summary.successful_loops == 0
    assert runs == []
