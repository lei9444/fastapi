"""Synthetic checks outside scripts/tests so the measured test set stays fixed."""

from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from scripts import storage_threeway as comparison
from scripts.tests.test_isolated_platform_ci import sample_result
from scripts.tests.test_storage_ab import httptools_record, storage_record, write_trial


def rows():
    return [
        comparison.Trial(
            layout,
            trial,
            "a" * 40,
            "123",
            "1",
            condition=condition,
            status="passed",
            comparable=True,
            storage_total_seconds=20.0 if layout == "refs-devdrive" else 0.0,
            cache_configuration_seconds=0.0 if layout == "refs-devdrive" else 0.01,
            setup_seconds=30.0 if version == "0.7.1" else 10.0,
            build_seconds=8.0,
            test_seconds=100.0,
            measured_phase_seconds=138.0 if version == "0.7.1" else 118.0,
            job_wall_seconds=170.0 if layout == "refs-devdrive" else 150.0,
            common_input_sha256="b" * 64,
            installed_common_sha256="c" * 64,
            wheelhouse_sha256="d" * 64,
            uv_version="uv 0.13.5 native",
            workers=4,
            test_manifest_sha256="e" * 64,
        )
        for condition, (layout, version) in comparison.CONDITIONS.items()
        for trial in (1, 2, 3)
    ]


def test_only_the_historical_package_entry_changes():
    original = Path("uv.lock").read_bytes()
    old = Path(".github/httptools-0.7.1.lock").read_bytes()
    selected, common_a = comparison.selected_lock(original, old, "0.7.1")
    unchanged, common_b = comparison.selected_lock(original, old, "0.9.0")
    assert comparison.sha(selected) == comparison.OLD_LOCK_SHA
    assert unchanged == original
    assert common_a == common_b
    assert comparison.selected_lock(
        original.replace(b"\n", b"\r\n"), old.replace(b"\n", b"\r\n"), "0.7.1"
    ) == (selected, common_a)
    with pytest.raises(ValueError, match="changed"):
        comparison.selected_lock(original + b"\n", old, "0.7.1")


def test_source_build_requires_both_origin_marker_and_cold_log():
    record = httptools_record()
    record.update(version="0.7.1", uv_build_metadata_present=True)
    log = "Building httptools==0.7.1\nBuilt httptools==0.7.1\n + httptools==0.7.1\n"
    comparison.validate_installation(record, log, "0.7.1")
    for change in ("marker", "build", "installed", "architecture"):
        bad = deepcopy(record)
        bad_log = log
        if change == "marker":
            bad["uv_build_metadata_present"] = False
        elif change == "build":
            bad_log = " + httptools==0.7.1\n"
        elif change == "installed":
            bad_log = "Building httptools==0.7.1\nBuilt httptools==0.7.1\n"
        else:
            bad["extensions"][0]["pe_machine"] = 0x8664
        with pytest.raises(ValueError):
            comparison.validate_installation(bad, bad_log, "0.7.1")


def test_prebuilt_version_never_accepts_source_compilation():
    record = httptools_record()
    comparison.validate_installation(record, " + httptools==0.9.0\n", "0.9.0")
    with pytest.raises(ValueError, match="source build"):
        comparison.validate_installation(
            record, "Building httptools==0.9.0\n + httptools==0.9.0\n", "0.9.0"
        )


def test_all_samples_contribute_to_arithmetic_means():
    samples = rows()
    samples[0].setup_seconds = 120.0
    samples[0].measured_phase_seconds = 228.0
    summaries, changes = comparison.summarize(samples)
    assert summaries[0]["mean_setup_seconds"] == 60.0
    assert summaries[0]["mean_measured_phase_seconds"] == 168.0
    assert changes["C-minus-B"]["setup_seconds_delta"] == 0.0
    assert changes["C-minus-B"]["job_wall_seconds_delta"] == 20.0
    assert changes["C-minus-B"]["job_wall_seconds_reduction_percent"] < 0


@pytest.mark.parametrize("problem", ["missing", "failed", "duplicate", "partial"])
def test_incomplete_group_has_no_mean_or_success_shaped_gain(problem):
    samples = rows()
    if problem == "missing":
        samples.pop(0)
    elif problem == "failed":
        samples[0].status = "incomplete"
    elif problem == "duplicate":
        samples[0].trial = 2
    else:
        samples[0].setup_seconds = None
    summaries, changes = comparison.summarize(samples)
    assert summaries[0]["mean_setup_seconds"] is None
    assert changes["B-minus-A"]["comparable"] is False
    assert changes["B-minus-A"]["setup_seconds_delta"] is None


@pytest.mark.parametrize(
    "field",
    [
        "common_input_sha256",
        "installed_common_sha256",
        "wheelhouse_sha256",
        "workers",
        "test_manifest_sha256",
    ],
)
def test_changed_workload_or_dependency_input_withholds_comparisons(field):
    samples = rows()
    setattr(samples[0], field, 2 if field == "workers" else "x")
    summaries, changes = comparison.summarize(samples)
    assert all(item["status"] == "input_mismatch" for item in summaries)
    assert not any(item["comparable"] for item in changes.values())


def test_default_control_bypasses_the_native_storage_validator(tmp_path):
    expected = {
        "condition": "A-default-0.7.1",
        "layout": "default",
        "httptools": "0.7.1",
        "trial": 1,
        "commit": "a" * 40,
        "run_id": "123",
        "run_attempt": "1",
    }
    record = {
        **expected,
        "storage_invoked": False,
        "disk_discovery": False,
        "workspace_migrated": False,
        "cache_configuration_seconds": 0.01,
        "initial_cache_state": {
            name: {"fresh": True, "entries": 0}
            for name in ("uv_cache", "pip_cache", "temp", "wheelhouse")
        },
    }
    comparison.isolated.write_json(tmp_path / "default-layout.json", record)
    with patch.object(
        comparison.storage,
        "validate_applied_storage",
        side_effect=AssertionError("Must not inspect native disks"),
    ):
        assert comparison.validate_layout(tmp_path, expected, True) == record
    (tmp_path / "storage.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="unexpectedly"):
        comparison.validate_layout(tmp_path, expected, False)


def test_local_entrypoint_refuses_before_effects(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "false")
    with pytest.raises(ValueError, match="hosted"):
        comparison.require_host()


def test_job_time_uses_actual_metadata_and_rejects_wrong_identity():
    row = rows()[0]
    job = {
        "name": f"Storage/httptools ({row.condition}, trial 1)",
        "run_id": 123,
        "run_attempt": 1,
        "head_sha": row.commit,
        "status": "completed",
        "conclusion": "failure",
        "started_at": "2026-11-13T10:00:00Z",
        "completed_at": "2026-11-13T10:03:00Z",
    }
    assert comparison.job_time({"jobs": [job]}, row) == (180.0, "failure")
    with pytest.raises(ValueError):
        comparison.job_time({"jobs": [job]}, replace(row, commit="z" * 40))


def test_workflow_is_exactly_one_manual_nine_trial_batch():
    workflow = yaml.load(
        Path(".github/storage-threeway-workflow.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert set(workflow["jobs"]) == {"trial", "report"}
    assert workflow["env"]["LOOPS"] == "1"
    job = workflow["jobs"]["trial"]
    assert job["runs-on"] == "windows-11-arm"
    assert job["strategy"]["matrix"] == {
        "condition": list(comparison.CONDITIONS),
        "trial": ["1", "2", "3"],
    }
    native = [step for step in job["steps"] if "storage_ab.ps1" in step.get("run", "")]
    assert len(native) == 2
    assert all(
        step["if"] == "${{ matrix.condition == 'C-devdrive-0.9.0' }}" for step in native
    )
    assert native[0]["working-directory"] == "${{ runner.temp }}"
    assert not any("uv pip install" in step.get("run", "") for step in job["steps"])


def test_preparation_timer_includes_both_original_work_and_common_audit(
    tmp_path, monkeypatch
):
    expected = {
        "condition": "B-default-0.9.0",
        "layout": "default",
        "httptools": "0.9.0",
        "trial": 1,
        "commit": "a" * 40,
        "run_id": "123",
        "run_attempt": "1",
        "selected_lock_sha256": comparison.LOCK_SHA,
    }
    comparison.isolated.write_json(tmp_path / "inputs.json", expected)
    output = tmp_path / "measurement"

    def prepare(*_):
        result = sample_result("windows-arm64")
        result.dependency_setup_seconds = 3.0
        comparison.isolated.write_json(output / "setup.json", asdict(result))
        return 0

    monkeypatch.setattr(
        comparison,
        "identity",
        lambda: {
            key: value
            for key, value in expected.items()
            if key != "selected_lock_sha256"
        },
    )
    monkeypatch.setattr(comparison, "validate_layout", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        comparison.isolated, "new_setup", lambda: sample_result("windows-arm64")
    )
    monkeypatch.setattr(comparison.isolated, "prepare", prepare)
    calls = []

    def audit(command, *_):
        calls.append(command)
        return 0, 2.0

    monkeypatch.setattr(comparison.common, "run_command", audit)
    with patch.object(comparison.time, "perf_counter", side_effect=[10.0, 15.5]):
        assert comparison.prepare(output, tmp_path / "wheelhouse", tmp_path) == 0
    assert calls[0][3:5] == ["audit", "--output"]
    assert (
        comparison.common.read_result(output / "setup.json").dependency_setup_seconds
        == 5.5
    )


def test_complete_synthetic_batch_aggregates_all_nine_artifacts(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    for key, value in {
        "GITHUB_SHA": "a" * 40,
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
        "MATRIX_RESULT": "success",
    }.items():
        monkeypatch.setenv(key, value)
    root = tmp_path / "artifacts"
    jobs = []
    for condition, (layout, version) in comparison.CONDITIONS.items():
        for number in (1, 2, 3):
            fixture = write_trial(tmp_path.parent / "f", layout, number)
            destination = root / f"threeway-{condition}-{number}-1"
            destination.parent.mkdir(exist_ok=True)
            fixture.rename(destination)
            expected = {
                "condition": condition,
                "layout": layout,
                "httptools": version,
                "trial": number,
                "commit": "a" * 40,
                "run_id": "123",
                "run_attempt": "1",
            }
            comparison.isolated.write_json(
                destination / "inputs.json",
                {
                    **expected,
                    "selected_lock_sha256": comparison.OLD_LOCK_SHA
                    if version == "0.7.1"
                    else comparison.LOCK_SHA,
                    "common_input_sha256": "f" * 64,
                },
            )
            comparison.isolated.write_json(
                destination / "trial.json",
                {
                    **expected,
                    "backup_retained_at_end": layout == "refs-devdrive",
                },
            )
            if layout == "default":
                (destination / "storage.json").unlink()
                comparison.isolated.write_json(
                    destination / "default-layout.json",
                    {
                        **expected,
                        "storage_invoked": False,
                        "disk_discovery": False,
                        "workspace_migrated": False,
                        "cache_configuration_seconds": 0.01,
                        "initial_cache_state": {
                            name: {"fresh": True, "entries": 0}
                            for name in ("uv_cache", "pip_cache", "temp", "wheelhouse")
                        },
                    },
                )
            runtime = {
                **expected,
                "python_pe_machine": 0xAA64,
                "interpreter_platform": "win-arm64",
                "common_installed_sha256": "c" * 64,
                "uv_version": "uv 0.13.5 native",
                "paths": storage_record(layout, number)["paths"],
            }
            comparison.isolated.write_json(
                destination / "measurement" / "runtime.json", runtime
            )
            if version == "0.7.1":
                evidence = httptools_record()
                evidence.update(version=version, uv_build_metadata_present=True)
                comparison.isolated.write_json(
                    destination / "measurement" / "httptools.json", evidence
                )
                (destination / "measurement" / "dependencies.log").write_text(
                    "Building httptools==0.7.1\nBuilt httptools==0.7.1\n + httptools==0.7.1\n",
                    encoding="utf-8",
                )
            jobs.append(
                {
                    "name": f"Storage/httptools ({condition}, trial {number})",
                    "run_id": 123,
                    "run_attempt": 1,
                    "head_sha": "a" * 40,
                    "status": "completed",
                    "conclusion": "success",
                    "started_at": "2026-11-13T10:00:00Z",
                    "completed_at": "2026-11-13T10:03:00Z",
                }
            )
    comparison.isolated.write_json(tmp_path / "jobs.json", {"jobs": jobs})
    assert comparison.aggregate(root, tmp_path / "report", tmp_path / "jobs.json") == 0
    result = comparison.storage.load_json(tmp_path / "report" / "report.json")
    assert len(result["trials"]) == 9
    assert all(row["status"] == "passed" for row in result["summaries"])
