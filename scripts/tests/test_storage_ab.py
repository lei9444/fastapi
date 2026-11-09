import csv
import json
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

import pytest

from scripts import platform_ci as common
from scripts import storage_ab as ab
from scripts.tests.test_isolated_platform_ci import (
    sample_audit,
    sample_result,
    write_build,
    write_test_evidence,
)

SHA = "a" * 40


@pytest.fixture(autouse=True)
def isolate_summary(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)


def expected(layout: str = "default", trial: int = 1) -> dict:
    return {
        "layout": layout,
        "trial": trial,
        "commit": SHA,
        "run_id": "123",
        "run_attempt": "1",
    }


def storage_record(layout: str, trial: int) -> dict:
    refs = layout == "refs-devdrive"
    drive = "Q:" if refs else "C:"

    def path(name: str) -> dict:
        physical = drive + "\\ab\\" + name
        return {
            "logical": physical,
            "physical": physical,
            "drive": drive,
            "filesystem": "ReFS" if refs else "NTFS",
            "is_dev_drive": refs,
            "exists": True,
        }

    paths = {
        name: path(name)
        for name in (
            "workspace",
            "venv",
            "uv_cache",
            "pip_cache",
            "temp",
            "tmp",
            "wheelhouse",
        )
    }
    paths["tmp"] = paths["temp"].copy()
    protection = {
        "available": True,
        "realtime_protection_enabled": True,
        "performance_mode_status": 1,
        "performance_mode": "Disabled",
    }
    return {
        "requested_layout": layout,
        "trial": trial,
        "applied_layout": layout,
        "status": "applied",
        "applied": True,
        "comparable": True,
        "reason": "",
        "context": {
            "commit": SHA,
            "run_id": "123",
            "run_attempt": "1",
            "repository": "lei9444/fastapi",
            "github_actions": "true",
            "runner_environment": "github-hosted",
            "runner_os": "Windows",
            "runner_arch": "ARM64",
            "os_arch": "Arm64",
            "process_arch": "Arm64",
            "is_windows": True,
        },
        "python": {"version": "3.13.17", "platform": "win-arm64", "pointer_bits": 64},
        "eligible_disk_count": 1,
        "selected_disk": {
            "Number": 1,
            "BusType": "NVMe",
            "PartitionStyle": "RAW",
            "NumberOfPartitions": 0,
            "Size": 100 * 1024**3,
            "IsBoot": False,
            "IsSystem": False,
            "IsOffline": False,
            "IsReadOnly": False,
            "eligible": True,
            "reasons": [],
        }
        if refs
        else None,
        "mutation_attempted": refs,
        "volume_verified": refs,
        "workspace_migrated": refs,
        "backup_retained": refs,
        "backup_path": "C:\\work\\repo.backup" if refs else None,
        "copy": {"verified": True, "source_digest": "e" * 64, "target_digest": "e" * 64}
        if refs
        else None,
        "initial_cache_state": {
            name: {"fresh": True, "entries": 0}
            for name in ("uv_cache", "pip_cache", "temp", "wheelhouse")
        },
        "paths": paths,
        "environment": {
            "UV_CACHE_DIR": paths["uv_cache"]["physical"],
            "PIP_CACHE_DIR": paths["pip_cache"]["physical"],
            "TEMP": paths["temp"]["physical"],
            "TMP": paths["temp"]["physical"],
        },
        "protection_before": deepcopy(protection),
        "protection_after": deepcopy(protection),
        "postcheck": {
            "logical_workspace_unchanged": True,
            "paths": paths,
            "uv": {
                "version": "uv 0.13.5 (fixture aarch64-pc-windows-msvc)",
                "architecture": "ARM64",
            },
            "protection": deepcopy(protection),
            "backup_retained": refs,
        },
        "timings": {
            "inspection_seconds": 1.0,
            "creation_seconds": 5.0 if refs else None,
            "migration_seconds": 20.0 if refs else None,
            "configuration_seconds": 1.0 if refs else 0.5,
            "postcheck_seconds": 3.0 if refs else 0.5,
            "total_seconds": 30.0 if refs else 2.0,
        },
    }


def write_trial(root: Path, layout: str, number: int) -> Path:
    folder = root / f"storage-ab-{layout}-{number}-1"
    common_data = expected(layout, number)
    ab.isolated.write_json(folder / "storage.json", storage_record(layout, number))
    ab.isolated.write_json(
        folder / "trial.json",
        {**common_data, "backup_retained_at_end": layout == "refs-devdrive"},
    )
    measurement = folder / "measurement"
    setup = sample_result("windows-arm64")
    setup.commit = SHA
    setup.dependency_setup_seconds = 10.0 if layout == "refs-devdrive" else 20.0
    ab.isolated.write_json(measurement / "setup.json", asdict(setup))
    ab.isolated.write_json(
        measurement / "inputs.json", {**common_data, "uv_lock_sha256": "b" * 64}
    )
    ab.isolated.write_json(measurement / "audit.json", sample_audit(1))
    result = sample_result("windows-arm64", 1)
    result.commit = SHA
    result.build_seconds = 7.0
    result.test_seconds = 100.0
    common.write_result(result, measurement / "loop-1")
    write_build(measurement / "loop-1")
    write_test_evidence(measurement / "loop-1")
    (measurement / "dependencies.log").write_text(
        "Installed locked packages.\n", encoding="utf-8"
    )
    return folder


def jobs() -> dict:
    return {
        "jobs": [
            {
                "name": f"Storage A/B ({layout}, trial {number})",
                "run_id": 123,
                "run_attempt": 1,
                "head_sha": SHA,
                "status": "completed",
                "conclusion": "success",
                "started_at": "2026-11-09T12:00:00Z",
                "completed_at": "2026-11-09T12:02:50Z"
                if layout == "refs-devdrive"
                else "2026-11-09T12:02:30Z",
            }
            for layout in ab.LAYOUTS
            for number in ab.TRIALS
        ]
    }


@pytest.mark.parametrize("layout", ab.LAYOUTS)
def test_valid_layout_has_actual_paths_and_not_inferred_protection(layout: str):
    data = storage_record(layout, 1)
    ab.validate_applied_storage(data, expected(layout), postcheck=True)
    assert data["postcheck"]["protection"]["performance_mode"] == "Disabled"


@pytest.mark.parametrize(
    "problem",
    [
        "unapplied",
        "not_comparable",
        "wrong_layout",
        "warm_cache",
        "not_devdrive",
        "wrong_fs",
        "no_backup",
        "bad_copy",
        "system_disk",
        "unknown_mode",
        "missing_storage_time",
        "uv_x64",
        "wrong_trial",
        "local",
        "self_hosted",
        "different_volume",
    ],
)
def test_invalid_treatment_is_not_a_valid_refs_sample(problem: str):
    data = storage_record("refs-devdrive", 1)
    if problem == "unapplied":
        data["applied"] = False
    elif problem == "not_comparable":
        data["comparable"] = False
    elif problem == "wrong_layout":
        data["applied_layout"] = "default"
    elif problem == "warm_cache":
        data["initial_cache_state"]["uv_cache"]["entries"] = 1
    elif problem == "not_devdrive":
        data["postcheck"]["paths"]["venv"]["is_dev_drive"] = False
    elif problem == "wrong_fs":
        data["postcheck"]["paths"]["uv_cache"]["filesystem"] = "NTFS"
    elif problem == "no_backup":
        data["backup_retained"] = False
    elif problem == "bad_copy":
        data["copy"]["target_digest"] = "different"
    elif problem == "system_disk":
        data["selected_disk"]["IsSystem"] = True
    elif problem == "unknown_mode":
        data["protection_before"]["performance_mode_status"] = None
    elif problem == "missing_storage_time":
        data["timings"]["total_seconds"] = None
    elif problem == "uv_x64":
        data["postcheck"]["uv"]["architecture"] = "X64"
    elif problem == "wrong_trial":
        data["trial"] = 2
    elif problem == "local":
        data["context"]["github_actions"] = "false"
    elif problem == "self_hosted":
        data["context"]["runner_environment"] = "self-hosted"
    else:
        data["postcheck"]["paths"]["pip_cache"]["drive"] = "Z:"
    with pytest.raises(ValueError):
        ab.validate_applied_storage(data, expected("refs-devdrive"), postcheck=True)


def test_control_must_not_initialize_storage():
    data = storage_record("default", 1)
    data["mutation_attempted"] = True
    with pytest.raises(ValueError, match="never initialize"):
        ab.validate_applied_storage(data, expected(), postcheck=True)


def test_local_measurement_entry_is_rejected(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "false")
    with pytest.raises(ValueError, match="hosted native"):
        ab.require_hosted_arm()


@pytest.mark.parametrize(
    "problem",
    ["missing", "duplicate", "wrong_sha", "wrong_attempt", "no_end", "negative"],
)
def test_job_wall_metadata_must_be_real_and_match(problem: str):
    data = jobs()
    if problem == "missing":
        data["jobs"] = []
    elif problem == "duplicate":
        data["jobs"].append(data["jobs"][0].copy())
    elif problem == "wrong_sha":
        data["jobs"][0]["head_sha"] = "wrong"
    elif problem == "wrong_attempt":
        data["jobs"][0]["run_attempt"] = 2
    elif problem == "no_end":
        data["jobs"][0]["completed_at"] = None
    else:
        data["jobs"][0]["completed_at"] = "2026-11-09T11:59:00Z"
    with pytest.raises(ValueError):
        ab.job_wall_time(data, ab.Trial("default", 1, SHA, "123", "1"))


def test_full_job_delta_includes_migration_not_just_faster_preparation(tmp_path: Path):
    for layout in ab.LAYOUTS:
        for number in ab.TRIALS:
            write_trial(tmp_path, layout, number)
    job_file = tmp_path / "jobs.json"
    ab.isolated.write_json(job_file, jobs())
    output = tmp_path / "report"
    assert ab.aggregate(tmp_path, output, job_file, SHA, "123", "1", "success") == 0
    report = ab.load_json(output / "report.json")
    assert len(report["trials"]) == 6 and report["success"] is True
    comparison = report["comparison"]
    assert comparison["setup_seconds_reduction_percent"] == 50.0
    assert comparison["job_wall_seconds_delta"] == 20.0
    assert comparison["job_wall_seconds_reduction_percent"] < 0
    assert report["summaries"][1]["mean_storage_total_seconds"] == 30.0
    with (output / "trials.csv").open(newline="", encoding="utf-8") as stream:
        assert len(list(csv.DictReader(stream))) == 6


def test_one_missing_refs_trial_blocks_refs_mean_and_all_gains(tmp_path: Path):
    for layout in ab.LAYOUTS:
        for number in ab.TRIALS:
            if (layout, number) != ("refs-devdrive", 2):
                write_trial(tmp_path, layout, number)
    job_file = tmp_path / "jobs.json"
    ab.isolated.write_json(job_file, jobs())
    output = tmp_path / "report"
    assert ab.aggregate(tmp_path, output, job_file, SHA, "123", "1", "failure") == 1
    report = ab.load_json(output / "report.json")
    assert len(report["trials"]) == 6
    assert report["summaries"][0]["mean_setup_seconds"] == 20.0
    assert report["summaries"][1]["valid_trials"] == 2
    assert report["summaries"][1]["mean_setup_seconds"] is None
    assert report["comparison"]["job_wall_seconds_delta"] is None


def test_failed_job_keeps_observed_wall_time_but_is_not_valid(tmp_path: Path):
    write_trial(tmp_path, "default", 1)
    data = jobs()
    data["jobs"][0]["conclusion"] = "failure"
    result, _ = ab.collect_trial(
        tmp_path, ab.Trial("default", 1, SHA, "123", "1"), data
    )
    assert result.job_wall_seconds == 150.0
    assert result.status == "incomplete"
    assert "failure" in result.error


def test_wheelhouse_mismatch_is_not_comparable(tmp_path: Path):
    rows = []
    for layout in ab.LAYOUTS:
        for number in ab.TRIALS:
            write_trial(tmp_path, layout, number)
            row, _ = ab.collect_trial(
                tmp_path, ab.Trial(layout, number, SHA, "123", "1"), jobs()
            )
            rows.append(row)
    rows[-1].wheelhouse_sha256 = "different"
    summaries, comparison = ab.build_summary(rows)
    assert comparison["comparable"] is False
    assert all(row["mean_job_wall_seconds"] is None for row in summaries)


def test_unapplied_refs_keeps_storage_cost_and_never_uses_control_fallback(
    tmp_path: Path,
):
    folder = tmp_path / "storage-ab-refs-devdrive-1-1"
    data = storage_record("refs-devdrive", 1)
    data.update(
        applied=False,
        comparable=False,
        applied_layout="default",
        status="not_applied",
        reason="eligible_raw_nvme_count_0",
    )
    ab.isolated.write_json(folder / "storage.json", data)
    row, _ = ab.collect_trial(
        tmp_path, ab.Trial("refs-devdrive", 1, SHA, "123", "1"), jobs()
    )
    assert row.status == "incomplete"
    assert row.storage_total_seconds == 30.0
    assert row.setup_seconds is None
    assert row.applied_layout == "default"
    assert "eligible_raw_nvme_count_0" in row.error
