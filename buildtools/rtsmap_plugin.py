"""pytest plugin used only at build time: records which sqlglot lines and which
data files each test touches.

Context "" holds everything that ran outside a test (imports, collection).
Uses sys.monitoring (Python 3.12+), so each line is recorded once per test and
then disabled until the next test re-enables events.
"""

import json
import os
import sys

import pytest

_ROOT = os.path.realpath(os.environ["RTS_ROOT"])
_SRC = os.path.join(_ROOT, "sqlglot") + os.sep
_OUT = os.environ["RTS_OUT"]

_mon = sys.monitoring
_TOOL = _mon.COVERAGE_ID
_DISABLE = _mon.DISABLE

_current = ""
_lines: dict[str, dict[str, set[int]]] = {"": {}}
_opens: dict[str, set[str]] = {"": set()}
_file_cache: dict[str, str | None] = {}


def _rel(filename: str) -> str | None:
    rel = _file_cache.get(filename, 0)
    if rel == 0:
        real = os.path.realpath(filename)
        rel = real[len(_ROOT) + 1 :] if real.startswith(_SRC) else None
        _file_cache[filename] = rel
    return rel


def _on_line(code, line):
    rel = _rel(code.co_filename)
    if rel is not None:
        _lines[_current].setdefault(rel, set()).add(line)
    return _DISABLE



def _audit(event, args):
    if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        try:
            path = os.path.realpath(os.fsdecode(args[0]))
        except Exception:
            return
        if path.startswith(_ROOT + os.sep) and not path.endswith((".py", ".pyc")):
            _opens[_current].add(path[len(_ROOT) + 1 :])


import tempfile

# Pools sized from the CPU count (test_optimizer's ProcessPoolExecutor) would
# spawn one interpreter per host core in every map process. Two workers run
# the same code, so coverage is unchanged and the build stays fast.
os.cpu_count = lambda: 2
if hasattr(os, "process_cpu_count"):
    os.process_cpu_count = lambda: 2

_CHILD_DIR = tempfile.mkdtemp(prefix="rtschild")
os.environ["RTS_CHILD_DIR"] = _CHILD_DIR
os.environ["RTS_PARENT_PID"] = str(os.getpid())

_mon.use_tool_id(_TOOL, "rtsmap")
_mon.register_callback(_TOOL, _mon.events.LINE, _on_line)
_mon.set_events(_TOOL, _mon.events.LINE)
sys.addaudithook(_audit)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    global _current
    _current = item.nodeid
    _lines.setdefault(_current, {})
    _opens.setdefault(_current, set())
    _mon.restart_events()
    yield
    _current = ""
    _mon.restart_events()


def pytest_runtest_logreport(report):
    outcome = _outcomes.setdefault(report.nodeid, "passed")
    if report.failed:
        _outcomes[report.nodeid] = "failed"
    elif report.skipped and outcome == "passed" and report.when == "call":
        _outcomes[report.nodeid] = "skipped"


_outcomes: dict[str, str] = {}


def pytest_sessionfinish(session, exitstatus):
    _mon.set_events(_TOOL, 0)
    # Lines run by child processes (spawned pools) count for the whole group.
    import gc
    import time
    from concurrent.futures import ProcessPoolExecutor

    for obj in gc.get_objects():
        if isinstance(obj, ProcessPoolExecutor):
            try:
                obj.shutdown(wait=True)
            except Exception:
                pass
    try:
        import multiprocessing

        for child in multiprocessing.active_children():
            child.join(10)
    except Exception:
        pass
    time.sleep(0.2)
    children = 0
    for name in os.listdir(_CHILD_DIR):
        try:
            with open(os.path.join(_CHILD_DIR, name)) as f:
                child = json.load(f)
        except Exception:
            continue
        children += 1
        for rel, ls in child.items():
            _lines[""].setdefault(rel, set()).update(ls)
    _outcomes["__children__"] = str(children)
    data = {
        "lines": {ctx: {f: sorted(ls) for f, ls in files.items()} for ctx, files in _lines.items()},
        "opens": {ctx: sorted(p) for ctx, p in _opens.items()},
        "outcomes": _outcomes,
    }
    with open(_OUT, "w") as f:
        json.dump(data, f)
