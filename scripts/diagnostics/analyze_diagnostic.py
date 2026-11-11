import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path


def seconds(value):
    return value / 1_000_000_000


def duration(span):
    return seconds(span["end_ns"] - span["start_ns"])


def cpu_duration(span):
    return seconds(span["cpu_end_ns"] - span["cpu_start_ns"])


def union_ns(intervals):
    total = 0
    end = None
    for left, right in sorted(intervals):
        if right < left:
            raise ValueError("A diagnostic interval runs backwards.")
        if end is None or left > end:
            total += right - left
            end = right
        elif right > end:
            total += right - end
            end = right
    return total


def union(spans, start=None, end=None):
    intervals = []
    for span in spans:
        left, right = span["start_ns"], span["end_ns"]
        if start is not None:
            left = max(left, start)
        if end is not None:
            right = min(right, end)
        if left <= right:
            intervals.append((left, right))
    return seconds(union_ns(intervals))


def one(values, label):
    if len(values) != 1:
        raise ValueError(f"Expected exactly one {label}; observed {len(values)}.")
    return values[0]


def span_kind(data, kind):
    return [span for span in data["spans"] if span["kind"] == kind]


def load_traces(directory):
    traces = []
    for path in sorted(directory.glob("trace-*.jsonl")):
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) != 2:
            raise ValueError(f"Incomplete diagnostic trace: {path.name}")
        data, footer = [json.loads(line) for line in lines]
        if data["open_spans"] and data["role"] != "fork-child":
            raise ValueError(f"Unclosed diagnostic spans in {path.name}")
        if (
            footer["buffered_spans"] != len(data["spans"])
            or footer["buffered_events"] != len(data["events"])
        ):
            raise ValueError(f"Diagnostic footer disagrees with {path.name}")
        data["write_finished_ns"] = footer["write_finished_ns"]
        ids = {span["id"] for span in data["spans"]}
        if len(ids) != len(data["spans"]):
            raise ValueError("Duplicate diagnostic span identity.")
        for span in data["spans"]:
            if span["end_ns"] < span["start_ns"]:
                raise ValueError("Invalid monotonic diagnostic timing.")
            if span["parent"] is not None and span["parent"] not in ids:
                if data["role"] != "fork-child":
                    raise ValueError("Diagnostic parent span is missing.")
        traces.append(data)
    if not traces:
        raise ValueError("No diagnostic traces were recorded.")
    return traces


def summarize(traces, invocation, platform):
    controller = one([data for data in traces if data["role"] == "controller"], "controller")
    workers = sorted([data for data in traces if data["role"] == "worker"], key=lambda d: d["worker"])
    if len(workers) != 2 or {data["worker"] for data in workers} != {"gw0", "gw1"}:
        raise ValueError("The diagnostic must contain exactly two workers.")
    if len({data["clock"] for data in traces}) != 1:
        raise ValueError("Processes did not record the same host monotonic clock.")
    worker_rows = []
    protocols = []
    expected_collection = None
    phase_rows = []
    timeout_rows = []
    for worker in workers:
        collected = one(
            [e for e in worker["events"] if e["kind"] == "collection_finished"],
            "worker collection",
        )["nodeids"]
        if expected_collection is None:
            expected_collection = set(collected)
        if expected_collection != set(collected) or len(set(collected)) != len(collected):
            raise ValueError("The two worker collections disagree.")
        configured = one(
            [e for e in worker["events"] if e["kind"] == "pytest_configure"],
            "worker configuration",
        )
        if configured["timeout"] != 20 or configured["timeout_func_only"] is not False:
            raise ValueError("The original timeout semantics changed.")
        items = sorted(span_kind(worker, "item_protocol"), key=lambda s: s["start_ns"])
        if not items:
            raise ValueError("Worker item-protocol traces are missing.")
        for item in items:
            if item["nodeid"] not in expected_collection:
                raise ValueError("An executed item was not in the original collection.")
            protocols.append({**item, "worker": worker["worker"]})
        for phase in ("item_setup", "item_call", "item_teardown"):
            spans = span_kind(worker, phase)
            if phase != "item_call" and Counter(s["nodeid"] for s in spans) != Counter(s["nodeid"] for s in items):
                raise ValueError(f"Missing or duplicated {phase} trace.")
            for span in spans:
                phase_rows.append({
                    "worker": worker["worker"], "nodeid": span["nodeid"], "phase": phase,
                    "start_ns": span["start_ns"], "end_ns": span["end_ns"],
                    "seconds": duration(span), "error": span["error"],
                })
        timer = [s for s in worker["spans"] if s["kind"].startswith("pytest_timeout_")]
        for span in timer:
            timeout_rows.append({
                "worker": worker["worker"], "nodeid": span["nodeid"], "kind": span["kind"],
                "start_ns": span["start_ns"], "end_ns": span["end_ns"],
                "seconds": duration(span), "cpu_seconds": cpu_duration(span), "error": span["error"],
                "effective_method": span.get("effective_method", ""),
            })
        if Counter(s["nodeid"] for s in span_kind(worker, "pytest_timeout_set_timer")) != Counter(s["nodeid"] for s in items):
            raise ValueError("Missing or duplicated timeout set trace.")
        expected_items = {s["nodeid"] for s in items}
        cancels = span_kind(worker, "pytest_timeout_cancel_timer")
        if {s["nodeid"] for s in cancels} != expected_items:
            raise ValueError("Missing timeout cancellation trace.")
        if any(s["error"] for s in timer):
            raise ValueError("A measured timeout hook failed.")
        expected_method = "thread" if platform.startswith("windows-") else "signal"
        if any(
            s.get("effective_method") != expected_method or s.get("timeout_seconds") != 20
            for s in span_kind(worker, "pytest_timeout_set_timer")
        ):
            raise ValueError("Unexpected measured timeout method or limit.")
        collection = one(span_kind(worker, "pytest_collection"), "full worker collection span")
        finish = one(span_kind(worker, "pytest_cov.DistWorker.finish"), "worker coverage finish")
        worker_rows.append({
            "worker": worker["worker"], "items": len(items),
            "bootstrap_to_configured_seconds": seconds(configured["at_ns"] - worker["entry_ns"]),
            "collection_seconds": duration(collection),
            "first_item_ns": items[0]["start_ns"], "last_item_ns": items[-1]["end_ns"],
            "item_protocol_seconds": union(items),
            "setup_seconds": union(span_kind(worker, "item_setup")),
            "call_seconds": union(span_kind(worker, "item_call")),
            "teardown_seconds": union(span_kind(worker, "item_teardown")),
            "collection_cpu_seconds": cpu_duration(collection),
            "timeout_method": configured["timeout_method"],
            "timeout_set_seconds": union(span_kind(worker, "pytest_timeout_set_timer")),
            "timeout_set_cpu_seconds": sum(cpu_duration(s) for s in span_kind(worker, "pytest_timeout_set_timer")),
            "timeout_set_calls": len(span_kind(worker, "pytest_timeout_set_timer")),
            "timeout_cancel_including_join_seconds": union(span_kind(worker, "pytest_timeout_cancel_timer")),
            "timeout_cancel_cpu_seconds": sum(cpu_duration(s) for s in cancels),
            "timeout_cancel_calls": len(cancels),
            "between_item_unassigned_seconds": seconds(items[-1]["end_ns"] - items[0]["start_ns"]) - union(items),
            "coverage_finish_seconds": duration(finish),
            "coverage_finish_cpu_seconds": cpu_duration(finish),
            "coverage_finish_end_ns": finish["end_ns"],
        })
    if Counter(s["nodeid"] for s in protocols) != Counter(expected_collection):
        raise ValueError("Executed item protocols do not cover the collection exactly once.")
    first_item = min(s["start_ns"] for s in protocols)
    last_item = max(s["end_ns"] for s in protocols)
    last_finish = max(row["coverage_finish_end_ns"] for row in worker_rows)
    master_finish = one(span_kind(controller, "pytest_cov.DistMaster.finish"), "controller coverage finish")
    master_summary = one(span_kind(controller, "pytest_cov.CovController.summary"), "controller coverage summary")
    reports = sorted(span_kind(controller, "coverage.report"), key=lambda s: s["start_ns"])
    total_report = one([s for s in reports if s.get("report_purpose") == "total_only"], "total-only Coverage.report")
    terminal_report = one([s for s in reports if s.get("report_purpose") == "term_missing_skip_covered"], "configured terminal Coverage.report")
    if (
        len(reports) != 2 or any(s["error"] for s in reports)
        or total_report.get("report_returned_total") is None
        or total_report["report_returned_total"] != terminal_report.get("report_returned_total")
        or not (
            master_summary["start_ns"] <= total_report["start_ns"] <= total_report["end_ns"]
            <= terminal_report["start_ns"] <= terminal_report["end_ns"] <= master_summary["end_ns"]
        )
    ):
        raise ValueError("Coverage report purposes, ordering or returned totals disagree.")
    if not (last_item <= last_finish <= master_finish["end_ns"] <= master_summary["end_ns"] <= invocation["end_ns"]):
        raise ValueError("The observed critical-path boundaries do not match the invocation.")
    cores = [
        {"role": data["role"], "worker": data["worker"], **{k: e[k] for k in ("tracer", "tracer_module", "dynamic_context", "branch")}}
        for data in [controller, *workers] for e in data["events"] if e["kind"] == "coverage_core"
    ]
    if not all(any(e["worker"] == w["worker"] and e["role"] == "worker" for e in cores) for w in workers):
        raise ValueError("Actual worker coverage tracer evidence is missing.")
    subprocess_rows = []
    parents = [(worker, span) for worker in workers for span in span_kind(worker, "telemetry_parent_wait")]
    for worker, parent in parents:
        child = one([d for d in traces if d["role"] == "telemetry-child" and d["call_token"] == parent["call_token"]], "correlated telemetry child")
        imported = one(span_kind(child, "telemetry_target_import"), "child target import")
        function = one(span_kind(child, "telemetry_function"), "child function call")
        if not (parent["start_ns"] <= child["entry_ns"] <= imported["start_ns"] <= imported["end_ns"]
                <= function["start_ns"] <= function["end_ns"] <= child["write_finished_ns"] <= parent["end_ns"]):
            raise ValueError("A correlated child timeline is incomplete or inconsistent.")
        cov = [s for s in child["spans"] if s["kind"].startswith("coverage.")]
        imports = span_kind(child, "child_import")
        subprocess_rows.append({
            "worker": worker["worker"], "nodeid": parent["nodeid"], "call_token": parent["call_token"],
            "target_module": parent["target_module"], "target_function": parent["target_function"],
            "parent_start_ns": parent["start_ns"], "parent_end_ns": parent["end_ns"],
            "parent_wait_seconds": duration(parent),
            "parent_process_cpu_seconds": cpu_duration(parent),
            "launch_to_observable_pth_entry_seconds": seconds(child["entry_ns"] - parent["start_ns"]),
            "entry_to_target_import_seconds": seconds(imported["start_ns"] - child["entry_ns"]),
            "target_import_seconds": duration(imported),
            "target_import_cpu_seconds": seconds(imported["cpu_end_ns"] - imported["cpu_start_ns"]),
            "import_to_function_seconds": seconds(function["start_ns"] - imported["end_ns"]),
            "function_including_imports_sdk_and_requests_seconds": duration(function),
            "import_calls_inside_function_seconds": union(imports, function["start_ns"], function["end_ns"]),
            "function_other_work_unassigned_seconds": duration(function) - union(imports, function["start_ns"], function["end_ns"]),
            "function_cpu_seconds": seconds(function["cpu_end_ns"] - function["cpu_start_ns"]),
            "function_end_to_parent_return_seconds": seconds(parent["end_ns"] - function["end_ns"]),
            "coverage_union_after_function_seconds": union(cov, function["end_ns"], parent["end_ns"]),
            "trace_serialization_write_seconds": seconds(child["write_finished_ns"] - child["flush_start_ns"]),
            "import_io": json.dumps({"before": imported.get("io_before"), "after": imported.get("io_after")}),
            "function_io": json.dumps({"before": function.get("io_before"), "after": function.get("io_after")}),
            "posix_only": parent["target_function"] == "test_environment_export_initializes_after_fork",
        })
    common_children = [r for r in subprocess_rows if not r["posix_only"]]
    if len(common_children) != 43 or len(parents) != (43 if platform.startswith("windows-") else 44):
        raise ValueError("Expected 43 common isolated tests plus the POSIX-only fork case.")
    coverage_rows = [
        {
            "role": data["role"], "worker": data["worker"], "call_token": data["call_token"],
            "kind": span["kind"], "start_ns": span["start_ns"], "end_ns": span["end_ns"],
            "seconds": duration(span), "cpu_seconds": seconds(span["cpu_end_ns"] - span["cpu_start_ns"]),
            "error": span["error"],
            "report_purpose": span.get("report_purpose", ""),
            "report_output_format_argument": span.get("report_output_format_argument"),
            "report_show_missing_argument": span.get("report_show_missing_argument"),
            "report_skip_covered_argument": span.get("report_skip_covered_argument"),
            "report_stream_kind": span.get("report_stream_kind", ""),
            "report_returned_total": span.get("report_returned_total"),
        }
        for data in traces for span in data["spans"]
        if span["kind"].startswith(("coverage.", "pytest_cov."))
    ]
    critical = {
        "invocation_seconds": seconds(invocation["end_ns"] - invocation["start_ns"]),
        "launch_through_first_item_seconds": seconds(first_item - invocation["start_ns"]),
        "first_through_last_item_seconds": seconds(last_item - first_item),
        "last_item_through_last_worker_coverage_finish_seconds": seconds(last_finish - last_item),
        "last_worker_coverage_finish_through_master_finish_seconds": seconds(master_finish["end_ns"] - last_finish),
        "master_finish_through_report_finished_seconds": seconds(master_summary["end_ns"] - master_finish["end_ns"]),
        "report_finished_through_outer_return_seconds": seconds(invocation["end_ns"] - master_summary["end_ns"]),
        "last_item_worker": max(protocols, key=lambda s: s["end_ns"])["worker"],
        "last_coverage_worker": max(worker_rows, key=lambda r: r["coverage_finish_end_ns"])["worker"],
        "controller_coverage_finish_seconds": duration(master_finish),
        "controller_coverage_finish_cpu_seconds": cpu_duration(master_finish),
        "controller_coverage_summary_seconds": duration(master_summary),
        "controller_coverage_summary_cpu_seconds": cpu_duration(master_summary),
        "controller_combine_seconds": union(span_kind(controller, "coverage.combine")),
        "controller_xml_report_seconds": union(span_kind(controller, "coverage.xml_report")),
        "controller_json_report_seconds": union(span_kind(controller, "coverage.json_report")),
        "controller_report_calls_seconds": union(span_kind(controller, "coverage.report")),
        "controller_total_only_report_seconds": duration(total_report),
        "controller_total_only_report_cpu_seconds": cpu_duration(total_report),
        "controller_term_missing_report_seconds": duration(terminal_report),
        "controller_term_missing_report_cpu_seconds": cpu_duration(terminal_report),
    }
    active = union(protocols, first_item, last_item)
    critical["item_window_at_least_one_worker_active_seconds"] = active
    critical["item_window_neither_worker_in_protocol_seconds"] = seconds(last_item - first_item) - active
    critical["item_window_both_workers_active_seconds"] = sum(row["item_protocol_seconds"] for row in worker_rows) - active
    critical["item_window_exactly_one_worker_active_seconds"] = 2 * active - sum(row["item_protocol_seconds"] for row in worker_rows)
    return {
        "status": "validated", "platform": platform, "critical_path": critical, "workers": worker_rows,
        "coverage_cores": cores, "telemetry_subprocesses": subprocess_rows,
        "item_phases": phase_rows, "timeout_hooks": timeout_rows, "coverage_calls": coverage_rows,
        "item_protocols": protocols, "trace_count": len(traces),
        "notes": [
            "The six consecutive critical-path intervals partition outer wall time; nested call totals do not.",
            "CPU seconds are per-process and include threads, not child-process CPU. Wall minus CPU is not disk/Defender time.",
            "Child entry is observable .pth bootstrap, not OS process creation or Python interpreter entry.",
            "Function spans include imports inside the function, SDK work, requests and waits; not pure SDK-exclusive time.",
            "Child import spans observe outermost builtin import calls in the target/function thread, including cache hits; imports on other threads are not exhaustively traced.",
            "Exit residual includes shutdown, recorded coverage and diagnostic flush, pipes and process teardown.",
            "Coverage calls overlap/nest; use interval unions, not sum of all methods.",
            "Coverage.report purposes come from the original bound call arguments: total-only then the requested term-missing/skip-covered report. Neither call was removed or reused.",
        ],
    }


def save_csv(path, rows):
    if not rows:
        raise ValueError(f"Required diagnostic table is empty: {path.name}")
    columns = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def mechanisms(traces, platform):
    rows = []
    backends = []
    for data in traces:
        for row in data.get("operation_counters", []):
            if (
                type(row["calls"]) is not int or row["calls"] < 1
                or not 0 <= row["errors"] <= row["calls"]
                or min(row["wall_ns"], row["cpu_ns"], row["input_units"], row["output_units"]) < 0
            ):
                raise ValueError("Invalid aggregate operation measurement.")
            rows.append({
                "role": data["role"], "worker": data["worker"], "call_token": data["call_token"],
                "operation": row["operation"], "scope": row["scope"], "variant": row["variant"],
                "calls": row["calls"], "errors": row["errors"],
                "wall_seconds": seconds(row["wall_ns"]), "process_cpu_seconds": seconds(row["cpu_ns"]),
                "mean_wall_microseconds": row["wall_ns"] / row["calls"] / 1000,
                "input_units": row["input_units"], "output_units": row["output_units"],
            })
        backends.extend({
            "role": data["role"], "worker": data["worker"], "call_token": data["call_token"], **row,
        } for row in data.get("backend_observations", []))
    controller = one([data for data in traces if data["role"] == "controller"], "controller")
    required = {
        "coverage.sqlitedb._connect", "coverage.sqlitedb.close", "coverage.sqldata._init_db",
        "coverage.numbits.decode", "coverage.parser.parse_source", "coverage.files.abs_file",
    }
    if not required.issubset(set(controller["patches"])):
        raise ValueError("Some aggregate coverage hooks were not installed.")
    first_report = min(s["start_ns"] for s in span_kind(controller, "coverage.report"))
    installed_modules = {
        event["module"] for event in controller["events"]
        if event["kind"] == "mechanism_hooks_installed" and event["at_ns"] < first_report
    }
    if not {"coverage.sqlitedb", "coverage.sqldata", "coverage.numbits",
            "coverage.parser", "coverage.files"}.issubset(installed_modules):
        raise ValueError("Counter attachment before reporting is not evidenced.")
    for purpose in ("total_only", "term_missing_skip_covered"):
        observed = {
            row["operation"] for row in rows
            if row["role"] == "controller" and row["scope"] == "coverage.report:" + purpose
        }
        if not observed:
            raise ValueError("Coverage report operation counters are incomplete.")
    for worker in ("gw0", "gw1"):
        if not any(row["role"] == "worker" and row["worker"] == worker
                   and row["operation"] == "testclient_http_asgi" for row in backends):
            raise ValueError("Real TestClient backend dispatches were not observed on both workers.")
    if not any(row.get("http_client_module") == "httpx2" for row in backends):
        raise ValueError("The actual TestClient HTTP library binding was not observed.")
    return {"operation_counters": rows, "backend_observations": backends,
            "controller_counter_hooks": sorted(required),
            "zero_count_rule": "A missing operation row is zero only within an observed scope with its hook installed beforehand; otherwise it is unknown.",
            "scope": "Inclusive aggregate wrapper intervals; nested operations are not additive."}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    root = args.directory
    inputs = json.loads((root / "inputs.json").read_text("utf-8"))
    invocation = json.loads((root / "invocation.json").read_text("utf-8"))
    baseline = json.loads((root / "baseline-check.json").read_text("utf-8"))
    if (
        invocation["returncode"] != 0 or baseline["matched"] is not True
        or baseline.get("coverage_matched") is not True
    ):
        raise ValueError("The complete diagnostic test invocation did not match its baseline.")
    traces = load_traces(root / "traces")
    data = summarize(traces, invocation, inputs["platform"])
    data["implementation"] = mechanisms(traces, inputs["platform"])
    implementation = json.loads((root / "implementation.json").read_text("utf-8"))
    if implementation["python"]["machine"].lower() not in {"arm64", "aarch64"}:
        raise ValueError("Post-workload metadata does not confirm native ARM64.")
    for field in ("workers", "telemetry_subprocesses", "item_phases", "item_protocols", "timeout_hooks", "coverage_calls"):
        save_csv(root / (field + ".csv"), data[field])
    for field in ("operation_counters", "backend_observations"):
        save_csv(root / (field + ".csv"), data["implementation"][field])
    (root / "analysis.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    critical = data["critical_path"]
    lines = ["# Test diagnostic (one instrumented sample)", "", f"Platform: {inputs['platform']}", ""]
    lines += ["| Observed boundary/interval | Seconds |", "|---|---:|"]
    for key, value in critical.items():
        if isinstance(value, (int, float)):
            lines.append(f"| {key} | {value:.3f} |")
    lines += ["", *data["notes"]]
    markdown = "\n".join(lines) + "\n"
    (root / "report.md").write_text(markdown, encoding="utf-8")
    if path := __import__("os").environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(markdown)
    print(json.dumps({"critical_path": critical, "workers": data["workers"]}, indent=2))


if __name__ == "__main__":
    sys.exit(main())
