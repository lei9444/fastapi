"""Pure mock/synthetic checks, intentionally outside the measured test selection."""

import builtins
import importlib
import importlib.util
import inspect
import json
import os
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import analyze_diagnostic as analysis
import diag_runtime as runtime


def synthetic():
    counter = 0

    def span(kind, start, end, nodeid="", **detail):
        nonlocal counter
        counter += 1
        return {
            "id": counter, "parent": None, "kind": kind, "nodeid": nodeid,
            "start_ns": start, "end_ns": end, "cpu_start_ns": 0, "cpu_end_ns": 0,
            "error": "", **detail,
        }

    nodeids = [f"tests/test_synthetic.py::test_case[{i}]" for i in range(43)]
    workers = []
    children = []
    for number in range(2):
        name = f"gw{number}"
        worker = {
            "role": "worker", "worker": name, "clock": "synthetic",
            "entry_ns": 1, "call_token": "", "spans": [],
            "events": [
                {"kind": "pytest_configure", "at_ns": 2, "timeout": 20,
                 "timeout_func_only": False, "timeout_method": "thread"},
                {"kind": "collection_finished", "nodeids": nodeids},
                {"kind": "coverage_core", "tracer": "SyntheticTracer",
                 "tracer_module": "synthetic", "dynamic_context": None, "branch": False},
            ],
        }
        worker["spans"].append(span("pytest_collection", 2, 10))
        for i in range(number, 43, 2):
            start = 20 + i * 100
            token = f"child-{i}"
            worker["spans"].append(span("item_protocol", start, start + 90, nodeids[i]))
            worker["spans"].append(span(
                "pytest_timeout_set_timer", start, start + 1, nodeids[i],
                effective_method="thread", timeout_seconds=20,
            ))
            worker["spans"].append(span("item_setup", start + 1, start + 9, nodeids[i]))
            worker["spans"].append(span("item_call", start + 10, start + 80, nodeids[i]))
            worker["spans"].append(span("item_teardown", start + 80, start + 85, nodeids[i]))
            worker["spans"].append(span("pytest_timeout_cancel_timer", start + 85, start + 89, nodeids[i]))
            worker["spans"].append(span("telemetry_parent_wait", start + 10, start + 80, nodeids[i],
                                         call_token=token, target_module="synthetic", target_function="test_case"))
            children.append({
                "role": "telemetry-child", "worker": "", "clock": "synthetic",
                "call_token": token, "entry_ns": start + 20,
                "flush_start_ns": start + 70, "write_finished_ns": start + 75,
                "events": [], "spans": [
                    span("telemetry_target_import", start + 30, start + 40),
                    span("telemetry_function", start + 45, start + 60),
                    span("coverage._atexit", start + 61, start + 67),
                    span("coverage.save", start + 62, start + 66),
                ],
            })
        worker["spans"].append(span("pytest_cov.DistWorker.finish", 4400, 4450 + number))
        workers.append(worker)
    controller = {
        "role": "controller", "worker": "", "clock": "synthetic", "call_token": "",
        "events": [], "spans": [
            span("pytest_cov.DistMaster.finish", 4460, 4500),
            span("coverage.combine", 4470, 4490),
            span("pytest_cov.CovController.summary", 4505, 4600),
            span("coverage.report", 4510, 4520, report_purpose="total_only", report_returned_total=99.0),
            span("coverage.report", 4521, 4540, report_purpose="term_missing_skip_covered", report_returned_total=99.0),
            span("coverage.xml_report", 4540, 4570),
            span("coverage.json_report", 4570, 4590),
        ],
    }
    return [controller, *workers, *children], {"start_ns": 0, "end_ns": 4700}


class SyntheticChecks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.trace = runtime.Recorder(self.directory.name, (1, 0))
        self.patch = patch.object(runtime, "TRACE", self.trace)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_return_value_arguments_and_exception_are_preserved(self):
        sentinel = object()
        original = Mock(return_value=sentinel)
        owner = SimpleNamespace(method=original)
        runtime.wrap_method(owner, "method", "synthetic")
        self.assertIs(owner.method(3, value=4), sentinel)
        original.assert_called_once_with(3, value=4)
        failure = RuntimeError("expected")
        original.side_effect = failure
        with self.assertRaises(RuntimeError) as error:
            owner.method("next")
        self.assertIs(error.exception, failure)
        self.assertEqual(self.trace.spans[-1]["error"], "RuntimeError")

    def test_nested_span_union_is_not_a_sum(self):
        self.assertEqual(analysis.union_ns([(0, 10), (2, 5), (8, 15), (20, 25)]), 20)
        with self.assertRaises(ValueError):
            analysis.union_ns([(5, 1)])

    def test_telemetry_forwards_original_command_and_isolation(self):
        result = SimpleNamespace(returncode=0)
        original = Mock(return_value=result)
        command = ["python", "-m", "tests.test_telemetry._subprocess", "module", "function", "{}"]
        env = {"PYDANTIC_DISABLE_PLUGINS": "__all__", "PATH": "fixture"}
        measured = runtime.telemetry_run(original)
        self.assertIs(measured(command, env=env, capture_output=True, text=True, timeout=15), result)
        args, kwargs = original.call_args
        self.assertIs(args[0], command)
        self.assertEqual(kwargs["timeout"], 15)
        self.assertTrue(kwargs["capture_output"])
        self.assertEqual(kwargs["env"]["PYDANTIC_DISABLE_PLUGINS"], "__all__")
        self.assertEqual(kwargs["env"]["PATH"], env["PATH"])
        self.assertEqual(set(kwargs["env"]) - set(env),
                         {"FASTAPI_DIAG_CALL", "FASTAPI_DIAG_MODULE", "FASTAPI_DIAG_FUNCTION"})
        self.assertNotIn("FASTAPI_DIAG_CALL", env)

    def test_non_target_subprocess_is_untouched(self):
        original = Mock()
        args = ["python", "-c", "pass"]
        runtime.telemetry_run(original)(args, check=True)
        original.assert_called_once_with(args, check=True)
        self.assertFalse(self.trace.spans)

    def test_target_subprocess_error_is_not_swallowed(self):
        error = TimeoutError("fixture")
        original = Mock(side_effect=error)
        command = ["python", "-m", "tests.test_telemetry._subprocess", "m", "f", "{}"]
        with self.assertRaises(TimeoutError) as caught:
            runtime.telemetry_run(original)(command, env={}, timeout=15)
        self.assertIs(caught.exception, error)
        self.assertEqual(self.trace.spans[-1]["error"], "TimeoutError")

    def test_existing_decorator_keeps_its_environment_filter(self):
        path = Path(__file__).resolve().parents[2] / "tests" / "test_telemetry" / "_subprocess.py"
        spec = importlib.util.spec_from_file_location("original_telemetry_helper", path)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        original = Mock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
        with patch.object(helper.subprocess, "run", runtime.telemetry_run(original)):
            with patch.dict(os.environ, {
                "PATH": "fixture", "FASTAPI_OTEL_TEST": "remove", "OTEL_TEST": "remove",
                "LOGFIRE_TEST": "remove", "SENTRY_TEST": "remove",
            }, clear=True):
                def example(value):
                    raise AssertionError("The mock must not execute a child.")
                helper.run_in_subprocess(example)(value=3)
        args, kwargs = original.call_args
        self.assertEqual(args[0][1:3], ["-m", "tests.test_telemetry._subprocess"])
        self.assertEqual(json.loads(args[0][5]), {"value": 3})
        self.assertEqual(kwargs["timeout"], 15)
        self.assertTrue(kwargs["capture_output"])
        self.assertTrue(kwargs["text"])
        self.assertFalse(any(k.startswith(("FASTAPI_OTEL_", "OTEL_", "LOGFIRE_", "SENTRY_")) for k in kwargs["env"]))
        self.assertEqual(kwargs["env"]["PYDANTIC_DISABLE_PLUGINS"], "__all__")

    def test_child_wrapper_calls_original_function_once(self):
        self.trace.target_module = "fixture"
        self.trace.target_function = "function"
        expected = object()
        calls = []

        def function(*args, **kwargs):
            calls.append((args, kwargs))
            return expected

        function.__module__ = "fixture"
        original_import, original_unwrap = importlib.import_module, inspect.unwrap
        original_builtin = builtins.__import__
        try:
            runtime.instrument_child()
            measured = inspect.unwrap(function)
            self.assertIs(measured(1, value=2), expected)
            self.assertEqual(calls, [((1,), {"value": 2})])
            self.assertIs(inspect.unwrap(expected), expected)
        finally:
            importlib.import_module, inspect.unwrap = original_import, original_unwrap
            builtins.__import__ = original_builtin
        self.assertEqual(self.trace.spans[-1]["kind"], "telemetry_function")

    def test_buffer_is_written_once_at_flush(self):
        with self.trace.span("outer"):
            with self.trace.span("inner"):
                self.trace.event("point")
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])
        self.trace.flush()
        self.trace.flush()
        paths = list(Path(self.directory.name).glob("trace-*.jsonl"))
        self.assertEqual(len(paths), 1)
        data = analysis.load_traces(Path(self.directory.name))
        self.assertEqual(len(data[0]["spans"]), 2)

    def test_missing_trace_footer_is_explicit(self):
        path = Path(self.directory.name) / "trace-broken.jsonl"
        path.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            analysis.load_traces(path.parent)

    def test_synthetic_critical_path_partitions_wall(self):
        traces, invocation = synthetic()
        data = analysis.summarize(traces, invocation, "windows-x64")
        critical = data["critical_path"]
        fields = (
            "launch_through_first_item_seconds", "first_through_last_item_seconds",
            "last_item_through_last_worker_coverage_finish_seconds",
            "last_worker_coverage_finish_through_master_finish_seconds",
            "master_finish_through_report_finished_seconds",
            "report_finished_through_outer_return_seconds",
        )
        self.assertAlmostEqual(sum(critical[key] for key in fields), critical["invocation_seconds"])
        child = data["telemetry_subprocesses"][0]
        self.assertEqual(child["coverage_union_after_function_seconds"], 6e-9)
        self.assertEqual(len(data["telemetry_subprocesses"]), 43)

    def test_missing_child_and_duplicate_items_fail(self):
        traces, invocation = synthetic()
        traces.pop()
        with self.assertRaisesRegex(ValueError, "correlated telemetry child"):
            analysis.summarize(traces, invocation, "windows-x64")
        traces, invocation = synthetic()
        item = next(s for s in traces[1]["spans"] if s["kind"] == "item_protocol")
        traces[1]["spans"].append(item.copy())
        with self.assertRaisesRegex(ValueError, "duplicated|exactly once"):
            analysis.summarize(traces, invocation, "windows-x64")

    def test_missing_phase_is_not_treated_as_zero(self):
        traces, invocation = synthetic()
        traces[1]["spans"] = [s for s in traces[1]["spans"] if s["kind"] != "item_teardown"]
        with self.assertRaisesRegex(ValueError, "item_teardown"):
            analysis.summarize(traces, invocation, "windows-x64")

    def test_timeout_wrappers_use_registered_hooks_not_import_loaders(self):
        import pluggy
        import pytest

        import diag_plugin

        class Specification:
            @pytest.hookspec(firstresult=True)
            def pytest_timeout_set_timer(self, item, settings):
                pass

            @pytest.hookspec(firstresult=True)
            def pytest_timeout_cancel_timer(self, item):
                pass

        called = []

        class Original:
            @pytest.hookimpl
            def pytest_timeout_set_timer(self, item, settings):
                called.append(("set", item, settings))
                return True

            @pytest.hookimpl
            def pytest_timeout_cancel_timer(self, item):
                called.append(("cancel", item))
                return True

        manager = pluggy.PluginManager("pytest")
        manager.add_hookspecs(Specification)
        manager.register(Original())
        manager.register(SimpleNamespace(
            pytest_timeout_set_timer=diag_plugin.pytest_timeout_set_timer,
            pytest_timeout_cancel_timer=diag_plugin.pytest_timeout_cancel_timer,
        ))
        manager.check_pending()
        item = object()
        settings = SimpleNamespace(method="thread", timeout=20)
        self.assertIs(manager.hook.pytest_timeout_set_timer(item=item, settings=settings), True)
        self.assertIs(manager.hook.pytest_timeout_cancel_timer(item=item), True)
        self.assertEqual(called, [("set", item, settings), ("cancel", item)])
        self.assertEqual([s["kind"] for s in self.trace.spans],
                         ["pytest_timeout_set_timer", "pytest_timeout_cancel_timer"])

    def test_timeout_hook_exception_is_preserved(self):
        import pluggy
        import pytest

        import diag_plugin

        class Specification:
            @pytest.hookspec(firstresult=True)
            def pytest_timeout_cancel_timer(self, item):
                pass

        expected = RuntimeError("original cancellation failure")

        class Original:
            @pytest.hookimpl
            def pytest_timeout_cancel_timer(self, item):
                raise expected

        manager = pluggy.PluginManager("pytest")
        manager.add_hookspecs(Specification)
        manager.register(Original())
        manager.register(SimpleNamespace(pytest_timeout_cancel_timer=diag_plugin.pytest_timeout_cancel_timer))
        with self.assertRaises(RuntimeError) as caught:
            manager.hook.pytest_timeout_cancel_timer(item=object())
        self.assertIs(caught.exception, expected)
        self.assertEqual(self.trace.spans[-1]["error"], "RuntimeError")

    def test_existing_controller_instance_is_patched_after_registration(self):
        import diag_plugin

        calls = []

        class CovController:
            def finish(self):
                calls.append("base.finish")

            def summary(self, stream):
                calls.append(("summary", stream))
                return 99.0

        class Central(CovController):
            pass

        class DistMaster(CovController):
            def finish(self):
                calls.append("master.finish")

        class DistWorker(CovController):
            def finish(self):
                super().finish()
                calls.append("worker.finish")

        engine = SimpleNamespace(
            CovController=CovController, Central=Central,
            DistMaster=DistMaster, DistWorker=DistWorker,
        )
        timeout_plugin = object()
        existing_controller = DistMaster()
        config = SimpleNamespace(
            pluginmanager=SimpleNamespace(
                getplugin=lambda name: SimpleNamespace(cov_controller=existing_controller) if name == "_cov" else None,
                is_registered=lambda module: module is timeout_plugin,
            ),
            getini=lambda name: "20", _env_timeout_method="thread",
            _env_timeout=20, _env_timeout_func_only=False,
        )
        with patch.dict("sys.modules", {"pytest_cov.engine": engine, "pytest_timeout": timeout_plugin}):
            with patch.dict(os.environ, {"PYTEST_XDIST_AUTO_NUM_WORKERS": "2"}):
                with patch.object(diag_plugin, "version", return_value="fixture"):
                    diag_plugin.pytest_configure(config)
        existing_controller.finish()
        stream = object()
        self.assertEqual(existing_controller.summary(stream), 99.0)
        DistWorker().finish()
        self.assertEqual(calls, ["master.finish", ("summary", stream), "base.finish", "worker.finish"])
        self.assertEqual({s["kind"] for s in self.trace.spans}, {
            "pytest_cov.DistMaster.finish", "pytest_cov.CovController.summary",
            "pytest_cov.CovController.finish", "pytest_cov.DistWorker.finish",
        })

    def test_report_purposes_use_original_arguments_and_preserve_both_calls(self):
        calls = []

        class Coverage:
            def report(self, morfs=None, show_missing=None, ignore_errors=None, file=None,
                       skip_covered=None, precision=None, output_format=None):
                calls.append((morfs, show_missing, ignore_errors, file, skip_covered, precision, output_format))
                return 99.5

        original_signature = inspect.signature(Coverage.report)
        runtime.wrap_method(
            Coverage, "report", "coverage.report", after=runtime.report_result,
            details=runtime.report_details(original_signature),
        )
        total_sink, terminal_stream = object(), StringIO()
        coverage = Coverage()
        self.assertEqual(
            coverage.report(ignore_errors=True, file=total_sink, precision=2, output_format="total"),
            99.5,
        )
        self.assertEqual(
            coverage.report(None, True, True, terminal_stream, True, 2),
            99.5,
        )
        self.assertEqual(len(calls), 2)
        self.assertIs(calls[0][3], total_sink)
        self.assertIs(calls[1][3], terminal_stream)
        self.assertEqual([s["report_purpose"] for s in self.trace.spans],
                         ["total_only", "term_missing_skip_covered"])
        self.assertEqual([s["report_returned_total"] for s in self.trace.spans], [99.5, 99.5])
        self.assertEqual(inspect.signature(Coverage.report), original_signature)

    def test_missing_cancellation_or_report_label_is_rejected(self):
        traces, invocation = synthetic()
        traces[1]["spans"] = [s for s in traces[1]["spans"] if s["kind"] != "pytest_timeout_cancel_timer"]
        with self.assertRaisesRegex(ValueError, "cancellation"):
            analysis.summarize(traces, invocation, "windows-x64")
        traces, invocation = synthetic()
        report = next(s for s in traces[0]["spans"] if s["kind"] == "coverage.report")
        report.pop("report_purpose")
        with self.assertRaisesRegex(ValueError, "total-only"):
            analysis.summarize(traces, invocation, "windows-x64")

    def test_local_activation_is_rejected(self):
        with patch.object(runtime, "TRACE", None), patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}):
            with self.assertRaisesRegex(RuntimeError, "hosted"):
                runtime.activate((1, 0))

    def test_synthetic_trace_is_json_serializable(self):
        traces, invocation = synthetic()
        value = analysis.summarize(traces, invocation, "windows-x64")
        self.assertEqual(json.loads(json.dumps(value))["trace_count"], len(traces))


if __name__ == "__main__":
    unittest.main()
