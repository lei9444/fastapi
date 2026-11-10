"""Loaded only with PYTEST_PLUGINS in the diagnostic invocation."""

import os
from importlib.metadata import version

import pytest

import diag_runtime


def trace():
    if diag_runtime.TRACE is None:
        raise RuntimeError("The diagnostic .pth bootstrap did not run.")
    return diag_runtime.TRACE


@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    current = trace()
    worker = getattr(config, "workerinput", {}).get("workerid", "")
    current.worker = worker
    current.role = "worker" if worker else "controller"
    if os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS") != "2":
        raise RuntimeError("Diagnostics require exactly two workers.")
    if float(config.getini("timeout")) != 20:
        raise RuntimeError("The original test timeout changed.")
    current.event(
        "pytest_configure",
        versions={name: version(name) for name in
                  ("pytest", "pytest-xdist", "pytest-cov", "coverage", "pytest-timeout")},
        timeout_method=config._env_timeout_method,
        timeout=config._env_timeout,
        timeout_func_only=config._env_timeout_func_only,
    )


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_sessionstart(session):
    with trace().span("pytest_sessionstart"):
        return (yield)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_collection(session):
    with trace().span("pytest_collection"):
        return (yield)


@pytest.hookimpl
def pytest_collection_finish(session):
    trace().event("collection_finished", nodeids=[item.nodeid for item in session.items])


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtestloop(session):
    with trace().span("pytest_runtestloop"):
        return (yield)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    current = trace()
    previous = current.nodeid
    current.nodeid = item.nodeid
    try:
        with current.span("item_protocol"):
            return (yield)
    finally:
        current.nodeid = previous


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_setup(item):
    with trace().span("item_setup"):
        return (yield)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_call(item):
    with trace().span("item_call"):
        return (yield)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    with trace().span("item_teardown"):
        return (yield)


@pytest.hookimpl
def pytest_runtest_logreport(report):
    trace().event(
        "test_report", nodeid=report.nodeid, phase=report.when,
        outcome=report.outcome, reported_seconds=report.duration,
    )


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    with trace().span("pytest_sessionfinish", exitstatus=int(exitstatus)):
        return (yield)


@pytest.hookimpl(optionalhook=True, wrapper=True, tryfirst=True)
def pytest_configure_node(node):
    with trace().span("worker_configure", worker=node.gateway.id):
        return (yield)


@pytest.hookimpl(optionalhook=True, wrapper=True, tryfirst=True)
def pytest_testnodedown(node, error):
    with trace().span("worker_down", worker=node.gateway.id,
                      worker_error=bool(error)):
        return (yield)
