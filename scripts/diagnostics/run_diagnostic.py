import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts import platform_ci as common  # noqa: E402


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def manifest(path):
    rows = []
    tree = ET.parse(path)
    for case in tree.iter("testcase"):
        skipped = case.find("skipped")
        status = "passed"
        reason = ""
        if skipped is not None:
            status = "xfail" if skipped.get("type") == "pytest.xfail" else "skipped"
            reason = skipped.get("message", "")
        elif case.find("failure") is not None:
            status = "failed"
        elif case.find("error") is not None:
            status = "error"
        rows.append([case.get("classname", ""), case.attrib["name"], status, reason])
    return sorted(rows)


def manifest_hash(rows):
    return hashlib.sha256(json.dumps(rows, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def require_hosted():
    if (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
        or os.environ.get("PLATFORM") not in {"windows-x64", "linux-x64"}
        or platform.python_version() != "3.13.17"
    ):
        raise RuntimeError("The diagnostic workload requires the authorized hosted x64 job.")


def prepare():
    require_hosted()
    python = ROOT / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    for command in (
        ["uv", "sync", "--locked", "--no-dev", "--group", "tests", "--extra", "all"],
        ["uv", "pip", "install", "--python", str(python), "--requirement", ".github/isolated-build-requirements.txt"],
    ):
        subprocess.run(command, check=True)


def invoke(output):
    import psutil

    require_hosted()
    result = common.read_result(output / "result.json")
    common.record_interpreter(result)
    common.verify_architecture(result)
    result.physical_cpus = psutil.cpu_count(logical=False)
    result.memory_gib = round(psutil.virtual_memory().total / 1024**3, 2)
    result.workers = 2
    common.write_result(result, output)
    bash = common.find_bash()
    return subprocess.call(
        [
            bash, "scripts/test-cov.sh",
            f"--junitxml={(output / 'junit.xml').as_posix()}",
            f"--cov-report=json:{(output / 'coverage.json').as_posix()}",
            f"--cov-report=xml:{(output / 'coverage.xml').as_posix()}",
            "--cov-report=term-missing:skip-covered", "--durations=20",
        ],
        env={
            **os.environ,
            "PATH": str(Path(bash).parent) + os.pathsep + os.environ["PATH"],
            "CONTEXT": f"{result.platform}-r1-py{result.python_version}",
            "COVERAGE_FILE": str(output / ".coverage"),
            "PYTEST_XDIST_AUTO_NUM_WORKERS": "2",
        },
    )


def run(output):
    require_hosted()
    if output.exists():
        raise RuntimeError("Diagnostic output already exists; refusing a second sample.")
    output.mkdir()
    traces = output / "traces"
    traces.mkdir()
    python = ROOT / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    site = subprocess.check_output(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        text=True,
    ).strip()
    pth = Path(site) / "00_fastapi_diagnostic.pth"
    if pth.exists():
        raise RuntimeError("Diagnostic bootstrap already installed.")
    if os.environ.get("PYTEST_PLUGINS") or os.environ.get("FASTAPI_DIAG_ACTIVE"):
        raise RuntimeError("Unexpected pre-existing diagnostic/plugin override.")
    result = common.Result(
        platform=os.environ["PLATFORM"], repetition=1,
        commit=os.environ["GITHUB_SHA"], run_id=os.environ["GITHUB_RUN_ID"],
        run_attempt=os.environ["GITHUB_RUN_ATTEMPT"],
        runner_label=os.environ["RUNNER_LABEL"], runner_os=os.environ["RUNNER_OS"],
        runner_arch=os.environ["RUNNER_ARCH"], expected_architecture="x64",
        image_os=os.environ.get("ImageOS", ""), image_version=os.environ.get("ImageVersion", ""),
    )
    common.record_interpreter(result)
    common.verify_architecture(result)
    common.write_result(result, output)
    command = [
        "uv", "run", "--no-sync", "python",
        "scripts/diagnostics/run_diagnostic.py", "invoke", "--output", str(output),
    ]
    inputs = {
        "commit": result.commit, "baseline_commit": "4d047839db5185c24aa16e116c619edbfba6cf3a",
        "platform": result.platform, "run_id": result.run_id, "run_attempt": result.run_attempt,
        "workers": 2, "command": command, "clock": time.get_clock_info("monotonic").implementation,
        "source_hashes": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [
                ROOT / "uv.lock", ROOT / "pyproject.toml", ROOT / "scripts/test.sh",
                ROOT / "scripts/test-cov.sh", ROOT / "tests/test_telemetry/_subprocess.py",
                *sorted(Path(__file__).parent.glob("*.py")),
            ]
        },
    }
    write_json(output / "inputs.json", inputs)
    pth.write_text(str(Path(__file__).parent) + "\nimport diag_bootstrap\n", encoding="utf-8")
    env = {
        **os.environ, "FASTAPI_DIAG_ACTIVE": "1", "FASTAPI_DIAG_DIR": str(traces),
        "PYTEST_PLUGINS": "diag_plugin", "PYTEST_XDIST_AUTO_NUM_WORKERS": "2",
        "UV_PYTHON": str(python), "PYTHONUNBUFFERED": "1",
    }
    started = time.monotonic_ns()
    try:
        code, elapsed = common.run_command(command, output / "tests.log", env)
    finally:
        ended = time.monotonic_ns()
        pth.unlink()
    result = common.read_result(output / "result.json")
    result.test_exit_code = code
    result.test_seconds = elapsed
    result.status = "passed" if code == 0 else "tests_failed"
    common.write_result(result, output)
    write_json(output / "invocation.json", {"start_ns": started, "end_ns": ended, "returncode": code})
    if (output / "junit.xml").exists():
        rows = manifest(output / "junit.xml")
        write_json(output / "test-manifest.json", rows)
        expected = json.loads((Path(__file__).parent / "baseline.json").read_text("utf-8"))
        matched = manifest_hash(rows) == expected[result.platform]["manifest_sha256"]
        write_json(output / "baseline-check.json", {
            "matched": matched, "records": len(rows),
            "actual_sha256": manifest_hash(rows), **expected[result.platform],
        })
        tree = ET.parse(output / "junit.xml")
        for suite in tree.iter("testsuite"):
            suite.attrib.pop("hostname", None)
        tree.write(output / "junit.xml", encoding="utf-8", xml_declaration=True)
        if not matched:
            raise RuntimeError("Diagnostic test IDs/outcomes/skips differ from the baseline.")
    else:
        raise RuntimeError("Diagnostic JUnit evidence is missing.")
    if code != 0:
        raise RuntimeError(f"The single diagnostic test invocation failed with exit {code}.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "run", "invoke"))
    parser.add_argument("--output", type=Path, default=ROOT / "diagnostic-results")
    args = parser.parse_args()
    output = args.output.resolve()
    if args.command == "prepare":
        prepare()
        return 0
    if args.command == "invoke":
        return invoke(output)
    run(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
