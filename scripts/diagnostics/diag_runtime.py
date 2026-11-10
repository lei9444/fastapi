"""Opt-in, process-local buffering. No installation or patching on import."""

import atexit
import builtins
import functools
import importlib
import importlib.machinery
import inspect
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

TRACE = None


class Span:
    def __init__(self, trace, kind, detail):
        self.trace = trace
        self.kind = kind
        self.detail = detail

    def __enter__(self):
        self.trace.ensure_process()
        stack = self.trace.stack()
        self.record = {
            "id": self.trace.next_id(),
            "parent": stack[-1] if stack else None,
            "kind": self.kind,
            "nodeid": self.trace.nodeid,
            "thread": threading.get_ident(),
            "start_ns": time.monotonic_ns(),
            "cpu_start_ns": time.process_time_ns(),
            **self.detail,
        }
        stack.append(self.record["id"])
        return self.record

    def __exit__(self, exception_type, exception, traceback):
        self.record["end_ns"] = time.monotonic_ns()
        self.record["cpu_end_ns"] = time.process_time_ns()
        self.record["error"] = exception_type.__name__ if exception_type else ""
        if self.trace.stack().pop() != self.record["id"]:
            raise RuntimeError("Diagnostic span nesting changed.")
        self.trace.spans.append(self.record)
        return False


class Recorder:
    def __init__(self, directory, entry):
        self.directory = Path(directory)
        self.pid = os.getpid()
        self.entry = entry
        self.process_key = f"{self.pid}-{entry[0]}"
        self.role = "python"
        self.worker = ""
        self.nodeid = ""
        self.call_token = os.environ.get("FASTAPI_DIAG_CALL", "")
        self.target_module = os.environ.get("FASTAPI_DIAG_MODULE", "")
        self.target_function = os.environ.get("FASTAPI_DIAG_FUNCTION", "")
        if self.call_token:
            self.role = "telemetry-child"
        self.spans = []
        self.events = []
        self.sequence = 0
        self.local = threading.local()
        self.flushed = False
        self.patches = []
        self.process = None

    def ensure_process(self):
        if self.pid != os.getpid():
            inherited = self.process_key
            self.__init__(self.directory, (time.monotonic_ns(), time.process_time_ns()))
            self.role = "fork-child"
            self.event("fork_observed", inherited_process=inherited)

    def stack(self):
        if not hasattr(self.local, "stack"):
            self.local.stack = []
        return self.local.stack

    def next_id(self):
        self.sequence += 1
        return self.sequence

    def span(self, kind, **detail):
        return Span(self, kind, detail)

    def event(self, kind, **detail):
        self.ensure_process()
        self.events.append({
            "kind": kind, "at_ns": time.monotonic_ns(),
            "cpu_ns": time.process_time_ns(), **detail,
        })

    def io(self):
        if self.process is None:
            return None
        import psutil

        try:
            value = self.process.io_counters()
        except (psutil.Error, OSError) as exc:
            return {"unavailable": type(exc).__name__}
        return {name: getattr(value, name) for name in
                ("read_count", "write_count", "read_bytes", "write_bytes")}

    def flush(self, reason="atexit"):
        self.ensure_process()
        if self.flushed:
            return
        self.flushed = True
        start = time.monotonic_ns()
        self.event("flush_start", reason=reason, io=self.io())
        payload = {
            "schema": 1, "process": self.process_key, "role": self.role,
            "worker": self.worker, "call_token": self.call_token,
            "target_module": self.target_module, "target_function": self.target_function,
            "entry_ns": self.entry[0], "entry_cpu_ns": self.entry[1],
            "clock": time.get_clock_info("monotonic").implementation,
            "patches": self.patches, "spans": self.spans, "events": self.events,
            "open_spans": list(self.stack()), "flush_start_ns": start,
        }
        path = self.directory / f"trace-{self.process_key}.jsonl"
        with path.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, separators=(",", ":")) + "\n")
            stream.flush()
            stream.write(json.dumps({
                "write_finished_ns": time.monotonic_ns(),
                "buffered_spans": len(self.spans),
                "buffered_events": len(self.events),
            }) + "\n")


def wrap_method(owner, name, kind, after=None):
    original = getattr(owner, name)
    if getattr(original, "_fastapi_diagnostic", False) is True:
        raise RuntimeError(f"Diagnostic method already wrapped: {kind}")

    @functools.wraps(original)
    def measured(*args, **kwargs):
        with TRACE.span(kind):
            value = original(*args, **kwargs)
            if after is not None:
                after(args, kwargs, value)
            return value

    measured._fastapi_diagnostic = True
    setattr(owner, name, measured)
    TRACE.patches.append(kind)


def record_core(args, kwargs, result):
    coverage = args[0]
    collector = coverage._collector
    TRACE.event(
        "coverage_core",
        tracer=collector.tracer_name(),
        tracer_module=collector.core.tracer_class.__module__,
        dynamic_context=coverage.config.dynamic_context,
        branch=coverage.config.branch,
    )


def patch_coverage(module):
    for name in ("start", "stop", "save", "combine", "report",
                 "xml_report", "json_report", "_atexit"):
        wrap_method(module.Coverage, name, f"coverage.{name}",
                    record_core if name == "start" else None)


def patch_cov_engine(module):
    for name in ("CovController", "Central", "DistMaster", "DistWorker"):
        owner = getattr(module, name)
        for method in ("start", "finish", "summary", "testnodedown"):
            if method in owner.__dict__:
                wrap_method(owner, method, f"pytest_cov.{name}.{method}")


PATCHERS = {
    "coverage.control": patch_coverage,
}


class DiagnosticLoader:
    def __init__(self, loader, name):
        self.loader = loader
        self.name = name

    def create_module(self, spec):
        return self.loader.create_module(spec)

    def exec_module(self, module):
        self.loader.exec_module(module)
        PATCHERS[self.name](module)

    def __getattr__(self, name):
        return getattr(self.loader, name)


class DiagnosticFinder:
    def find_spec(self, fullname, path=None, target=None):
        if fullname not in PATCHERS:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot instrument the pinned module {fullname}.")
        spec.loader = DiagnosticLoader(spec.loader, fullname)
        return spec


def telemetry_run(original):
    @functools.wraps(original)
    def measured(*args, **kwargs):
        command = args[0] if args else kwargs.get("args")
        if not (
            isinstance(command, (list, tuple)) and len(command) == 6
            and list(command[1:3]) == ["-m", "tests.test_telemetry._subprocess"]
        ):
            return original(*args, **kwargs)
        if "env" not in kwargs:
            raise RuntimeError("Telemetry subprocess lost its isolated environment.")
        token = f"{TRACE.process_key}-{TRACE.next_id()}"
        env = dict(kwargs["env"])
        env.update({
            "FASTAPI_DIAG_CALL": token,
            "FASTAPI_DIAG_MODULE": command[3],
            "FASTAPI_DIAG_FUNCTION": command[4],
        })
        forwarded = {**kwargs, "env": env}
        with TRACE.span("telemetry_parent_wait", call_token=token,
                        target_module=command[3], target_function=command[4]) as span:
            result = original(*args, **forwarded)
            span["returncode"] = result.returncode
            return result
    return measured


def instrument_child():
    original_import = importlib.import_module
    original_unwrap = inspect.unwrap
    original_builtin = builtins.__import__

    @functools.wraps(original_builtin)
    def builtin_import(*args, **kwargs):
        if not getattr(TRACE.local, "import_scope", False) or getattr(TRACE.local, "import_depth", 0):
            return original_builtin(*args, **kwargs)
        TRACE.local.import_depth = 1
        try:
            with TRACE.span("child_import", requested_module=args[0] if args else kwargs.get("name")):
                return original_builtin(*args, **kwargs)
        finally:
            TRACE.local.import_depth = 0

    @functools.wraps(original_import)
    def imported(name, package=None):
        if name != TRACE.target_module:
            return original_import(name, package)
        with TRACE.span("telemetry_target_import") as span:
            span["io_before"] = TRACE.io()
            TRACE.local.import_scope = True
            try:
                return original_import(name, package)
            finally:
                TRACE.local.import_scope = False
                span["io_after"] = TRACE.io()

    @functools.wraps(original_unwrap)
    def unwrapped(*args, **kwargs):
        function = original_unwrap(*args, **kwargs)
        if (getattr(function, "__module__", None), getattr(function, "__name__", None)) != (
            TRACE.target_module, TRACE.target_function
        ):
            return function

        @functools.wraps(function)
        def called(*call_args, **call_kwargs):
            with TRACE.span("telemetry_function") as span:
                span["io_before"] = TRACE.io()
                TRACE.local.import_scope = True
                try:
                    return function(*call_args, **call_kwargs)
                finally:
                    TRACE.local.import_scope = False
                    span["io_after"] = TRACE.io()
        return called

    importlib.import_module = imported
    inspect.unwrap = unwrapped
    builtins.__import__ = builtin_import
    TRACE.patches.append("telemetry-child-entry")


def activate(entry):
    global TRACE
    if TRACE is not None:
        raise RuntimeError("Diagnostics activated more than once.")
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("Real diagnostics are restricted to the authorized hosted job.")
    directory = Path(os.environ["FASTAPI_DIAG_DIR"])
    if not directory.is_dir():
        raise RuntimeError("Diagnostic output directory must be prepared explicitly.")
    TRACE = Recorder(directory, entry)
    atexit.register(TRACE.flush)
    sys.meta_path.insert(0, DiagnosticFinder())
    if any(name in sys.modules for name in PATCHERS):
        raise RuntimeError("Diagnostic bootstrap was loaded after a target module.")
    subprocess.run = telemetry_run(subprocess.run)
    original_exit = os._exit

    @functools.wraps(original_exit)
    def exited(code):
        TRACE.event("os_exit", code=code)
        TRACE.flush("os._exit")
        original_exit(code)

    os._exit = exited
    if TRACE.role == "telemetry-child":
        instrument_child()
    import psutil

    TRACE.process = psutil.Process()
    TRACE.event("bootstrap_ready", io=TRACE.io())
