"""Counts only real workload calls; no synthetic backend probes or library changes."""

import functools
import time


def qualified_type(value):
    if value is None:
        return "not_exposed"
    cls = type(value)
    return cls.__module__ + "." + cls.__qualname__


def counted(owner, name, operation, runtime, variant=None, input_units=None, output_units=None):
    original = getattr(owner, name)
    if getattr(original, "_fastapi_mechanism_counter", False) is True:
        return

    @functools.wraps(original)
    def measured(*args, **kwargs):
        current = runtime.TRACE
        detail = variant(args, kwargs) if variant else ""
        row = current.counter(operation, detail)
        row["calls"] += 1
        if input_units is not None:
            row["input_units"] += input_units(args, kwargs)
        started = time.monotonic_ns()
        cpu_started = time.process_time_ns()
        try:
            result = original(*args, **kwargs)
        except BaseException:
            row["errors"] += 1
            raise
        else:
            if output_units is not None:
                row["output_units"] += output_units(result)
            return result
        finally:
            row["wall_ns"] += time.monotonic_ns() - started
            row["cpu_ns"] += time.process_time_ns() - cpu_started

    measured._fastapi_mechanism_counter = True
    setattr(owner, name, measured)
    runtime.TRACE.patches.append(operation)


def connection_variant(args, kwargs):
    return "new_connection_and_initialization" if args[0].con is None else "already_connected"


def close_variant(args, kwargs):
    force = kwargs.get("force", args[1] if len(args) > 1 else False)
    if args[0].con is None:
        return "no_connection"
    return "close_connection" if force or not args[0].no_disk else "retained_in_memory"


def portal_facts(portal, function, args):
    scope = ""
    if (
        len(args) == 3 and isinstance(args[0], dict)
        and args[0].get("type") == "http"
        and getattr(args[1], "__module__", None) == "starlette.testclient"
        and getattr(args[2], "__module__", None) == "starlette.testclient"
    ):
        scope = "testclient_http_asgi"
    elif getattr(function, "__module__", None) == "starlette.testclient":
        name = getattr(function, "__qualname__", "")
        if name in {"WebSocketTestSession._run", "TestClient.lifespan"}:
            scope = "testclient_" + name
    if not scope:
        return None
    token = getattr(portal, "_token", None)
    backend = getattr(token, "backend_class", None)
    native = getattr(token, "native_token", None)
    return {
        "operation": scope,
        "portal_type": qualified_type(portal),
        "backend_class": (
            backend.__module__ + "." + backend.__qualname__ if backend else "not_exposed"
        ),
        "native_token_type": qualified_type(native),
        "selector_type": qualified_type(getattr(native, "_selector", None)),
        "proactor_type": qualified_type(getattr(native, "_proactor", None)),
    }


def patch_portal(module, runtime):
    owner = module.BlockingPortal
    original = owner._call_func
    if getattr(original, "_fastapi_mechanism_counter", False) is True:
        return

    @functools.wraps(original)
    async def called(self, func, args, kwargs, future):
        facts = portal_facts(self, func, args)
        if facts is not None:
            runtime.TRACE.observe(facts)
        return await original(self, func, args, kwargs, future)

    called._fastapi_mechanism_counter = True
    owner._call_func = called
    runtime.TRACE.patches.append("anyio.real_portal_dispatch")


def install(module, runtime):
    name = module.__name__
    if name == "coverage.sqlitedb":
        counted(module.SqliteDb, "_connect", "coverage.sqlitedb._connect", runtime,
                variant=connection_variant)
        counted(module.SqliteDb, "close", "coverage.sqlitedb.close", runtime,
                variant=close_variant)
    elif name == "coverage.sqldata":
        counted(module.CoverageData, "_init_db", "coverage.sqldata._init_db", runtime)
    elif name == "coverage.numbits":
        counted(module, "numbits_to_nums", "coverage.numbits.decode", runtime,
                input_units=lambda args, kwargs: len(args[0] if args else kwargs["numbits"]),
                output_units=len)
    elif name == "coverage.parser":
        counted(module.PythonParser, "parse_source", "coverage.parser.parse_source", runtime,
                input_units=lambda args, kwargs: len(args[0].text))
    elif name == "coverage.files":
        counted(module, "abs_file", "coverage.files.abs_file", runtime)
    elif name == "anyio.from_thread":
        patch_portal(module, runtime)
    elif name == "starlette.testclient":
        if getattr(module._TestClientTransport.handle_request, "_fastapi_mechanism_counter", False) is True:
            return
        counted(module._TestClientTransport, "handle_request", "testclient.handle_request", runtime)
        runtime.TRACE.observe({
            "operation": "testclient_transport_binding",
            "http_client_module": module.httpx.__name__,
            "transport_class": module._TestClientTransport.__module__ + "." + module._TestClientTransport.__qualname__,
        })
    else:
        raise RuntimeError("Unknown diagnostic mechanism module: " + name)
