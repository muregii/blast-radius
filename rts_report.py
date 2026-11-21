"""pytest plugin used at run time: writes each test's outcome to $RTS_REPORT.

A test counts as failed if any of its reports failed (setup, call, teardown or
a subtest), matching the scorer's definition. Collection errors are recorded
by file so the caller can fail every test in a file that cannot import.
"""

import json
import os

_outcomes: dict[str, str] = {}
_broken_files: list[str] = []


def pytest_runtest_logreport(report):
    if report.failed:
        _outcomes[report.nodeid] = "failed"
    else:
        _outcomes.setdefault(report.nodeid, "passed")


def pytest_collectreport(report):
    if report.failed:
        _broken_files.append(report.nodeid.split("::")[0])


def pytest_sessionfinish(session, exitstatus):
    with open(os.environ["RTS_REPORT"], "w") as f:
        json.dump({"outcomes": _outcomes, "broken_files": _broken_files}, f)
