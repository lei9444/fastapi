"""Post-workload metadata only; no backend sample, benchmark or settings changes."""

import importlib
import json
import os
import platform
import shutil
import sqlite3
import struct
import subprocess
import sys
import sysconfig
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def binary_architectures(path):
    with Path(path).open("rb") as stream:
        header = stream.read(4096)
        if header[:2] == b"MZ" and len(header) >= 64:
            offset = struct.unpack_from("<I", header, 0x3C)[0]
            stream.seek(offset)
            pe = stream.read(6)
            if len(pe) != 6 or pe[:4] != b"PE\0\0":
                raise ValueError("Invalid native PE header.")
            machine = struct.unpack_from("<H", pe, 4)[0]
            return {"format": "PE", "architectures": [{0xAA64: "arm64", 0x8664: "x64"}.get(machine, hex(machine))]}
        thin = {b"\xcf\xfa\xed\xfe": "<", b"\xfe\xed\xfa\xcf": ">"}
        fat = {
            b"\xca\xfe\xba\xbe": (">", 20), b"\xbe\xba\xfe\xca": ("<", 20),
            b"\xca\xfe\xba\xbf": (">", 32), b"\xbf\xba\xfe\xca": ("<", 32),
        }
        cpus = {0x0100000C: "arm64", 0x01000007: "x64"}
        if header[:4] in thin and len(header) >= 8:
            cpu = struct.unpack_from(thin[header[:4]] + "I", header, 4)[0]
            return {"format": "Mach-O", "architectures": [cpus.get(cpu, hex(cpu))]}
        if header[:4] in fat and len(header) >= 8:
            endian, size = fat[header[:4]]
            count = struct.unpack_from(endian + "I", header, 4)[0]
            if not 1 <= count <= 32 or len(header) < 8 + count * size:
                raise ValueError("Invalid universal Mach-O header.")
            values = [struct.unpack_from(endian + "I", header, 8 + i * size)[0] for i in range(count)]
            return {"format": "Mach-O universal", "architectures": [cpus.get(cpu, hex(cpu)) for cpu in values]}
    raise ValueError("Unrecognized native executable format: " + Path(path).name)


def cpu_model():
    if sys.platform == "darwin":
        command = ["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"]
    else:
        command = [
            "pwsh", "-NoProfile", "-Command",
            "(Get-CimInstance Win32_Processor | Select-Object -ExpandProperty Name) -join '; '",
        ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        return {"available": False, "reason": type(exc).__name__}
    return {"available": True, "model": result.stdout.strip()}


def filesystem(path):
    import psutil

    physical = Path(path).resolve()
    try:
        matches = [
            part for part in psutil.disk_partitions(all=True)
            if physical.is_relative_to(Path(part.mountpoint))
        ]
        if not matches:
            return {"available": False, "reason": "No matching mount in psutil.disk_partitions."}
        mount = max(matches, key=lambda part: len(part.mountpoint))
        usage = psutil.disk_usage(str(physical))
    except (psutil.Error, OSError) as exc:
        return {"available": False, "reason": type(exc).__name__}
    return {
        "available": True, "filesystem": mount.fstype, "mountpoint": mount.mountpoint,
        "total_bytes": usage.total, "free_bytes": usage.free,
    }


def collect(bash):
    import psutil

    if (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
        or platform.machine().lower() not in {"arm64", "aarch64"}
        or struct.calcsize("P") != 8
    ):
        raise RuntimeError("ARM metadata requires the authorized native hosted workload.")
    native = {}
    for name in (
        "coverage.tracer", "_sqlite3", "pydantic_core._pydantic_core",
        "httptools.parser.parser", "httptools.parser.url_parser",
    ):
        module = importlib.import_module(name)
        path = Path(module.__file__)
        facts = binary_architectures(path)
        if "arm64" not in facts["architectures"]:
            raise RuntimeError(f"The installed native extension is not ARM64: {name}")
        native[name] = {"filename": path.name, **facts}
    packages = {}
    for name in (
        "pytest", "pytest-xdist", "pytest-cov", "pytest-timeout", "coverage", "psutil",
        "starlette", "httpx2", "httpx", "anyio", "trio", "uvicorn", "httptools", "uvloop",
        "colorama", "pydantic", "pydantic-core", "opentelemetry-sdk", "logfire", "sentry-sdk",
        "build", "pip", "pdm-backend",
    ):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    with sqlite3.connect(":memory:") as connection:
        sqlite_options = [row[0] for row in connection.execute("PRAGMA compile_options")]
    bash_version = subprocess.check_output([bash, "--version"], text=True).splitlines()[0]
    uv_version = subprocess.check_output(["uv", "--version"], text=True).strip()
    uv_path = shutil.which("uv")
    if uv_path is None:
        raise RuntimeError("The actual uv executable is unavailable.")
    uv_binary = binary_architectures(uv_path)
    if not uv_version.startswith("uv 0.13.5") or "arm64" not in uv_binary["architectures"]:
        raise RuntimeError("uv must remain the pinned native ARM64 tool.")
    return {
        "collection_scope": "post-workload metadata; no synthetic TestClient or event-loop sample",
        "python": {
            "version": platform.python_version(), "machine": platform.machine(),
            "interpreter_platform": sysconfig.get_platform(), "compiler": platform.python_compiler(),
            "build": platform.python_build(), "pointer_bits": struct.calcsize("P") * 8,
            "executable": sys.executable,
            "configuration": {key: sysconfig.get_config_var(key) for key in
                              ("Py_DEBUG", "Py_GIL_DISABLED", "WITH_PYMALLOC", "Py_ENABLE_SHARED",
                               "CC", "CFLAGS", "CONFIG_ARGS", "PY_CFLAGS", "PY_CFLAGS_NODIST", "LDFLAGS")},
            "binary": binary_architectures(sys.executable),
        },
        "sqlite": {"version": sqlite3.sqlite_version, "threadsafety": sqlite3.threadsafety,
                   "compile_options": sqlite_options},
        "bash": {"path": bash, "version": bash_version, "binary": binary_architectures(bash)},
        "uv": {"path": uv_path, "version": uv_version, "binary": uv_binary},
        "packages": packages, "native_extensions": native,
        "resources": {"cpu_model": cpu_model(), "logical_cpus": psutil.cpu_count(),
                      "physical_cpus": psutil.cpu_count(logical=False),
                      "memory_bytes": psutil.virtual_memory().total},
        "workspace_filesystem": filesystem(Path.cwd()),
        "os": {"system": platform.system(), "release": platform.release(),
               "image_os": os.environ.get("ImageOS"), "image_version": os.environ.get("ImageVersion")},
        "limitations": [
            "Installed native modules are inspected after the test; presence is not proof of request-path use.",
            "Backend rows in the raw runtime trace, not metadata imports, identify actual TestClient execution.",
            "Build flags and CPU model are descriptive, not a controlled hardware comparison.",
            "Filesystem capacity and I/O counters do not measure disk or protection-software waiting.",
        ],
    }
