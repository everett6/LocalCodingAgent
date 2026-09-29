"""Tests for test-framework detection, running, and compact failure parsing.

Parser fixtures are trimmed copies of real output from pytest 9, Python 3.12
unittest, jest 30, vitest 3, mocha 12, go 1.25 and cargo 1.9x.
"""
import asyncio
import json
import shutil
import textwrap
from pathlib import Path

import pytest

from local_coder.orchestrator.coordinator import Coordinator
from local_coder.tools.testing import RunTestsTool
from local_coder.types import ProjectConfig, TaskStatus
from local_coder.verification.failures import parse_failures, summarize_failures
from local_coder.verification.test_runner import (
    TestFailure,
    TestReport,
    _parse_cargo,
    _parse_go,
    _parse_jest_like,
    _parse_mocha,
    _parse_pytest,
    _parse_unittest,
    build_plan,
    detect_framework,
    format_report,
)


def run(coro):
    return asyncio.run(coro)


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

@pytest.mark.parametrize("files, expected", [
    ({"pytest.ini": "[pytest]\n"}, "pytest"),
    ({"pyproject.toml": "[tool.pytest.ini_options]\n"}, "pytest"),
    ({"requirements.txt": "pytest\n", "tests/test_a.py": ""}, "pytest"),
    ({"Cargo.toml": "[package]\nname='x'\n"}, "cargo"),
    ({"go.mod": "module example.com/x\n"}, "go"),
    ({"package.json": json.dumps({"devDependencies": {"jest": "^30"}})}, "jest"),
    ({"package.json": json.dumps({"devDependencies": {"vitest": "^3"}})}, "vitest"),
    ({"package.json": json.dumps({"scripts": {"test": "mocha test/"}})}, "mocha"),
    ({"package.json": json.dumps({"scripts": {"test": "node run-tests.js"}})}, "npm"),
    ({"package.json": json.dumps({"scripts": {"test": "echo \"Error: no test specified\" && exit 1"}})}, None),
    ({"Makefile": "build:\n\tcc x.c\ntest:\n\t./check\n"}, "make"),
    ({"README.md": "hi"}, None),
])
def test_detect_framework(tmp_path, files, expected):
    for rel, text in files.items():
        write(tmp_path, rel, text)
    assert detect_framework(tmp_path) == expected


def test_detect_prefers_pytest_config_over_frontend_package_json(tmp_path):
    write(tmp_path, "package.json", json.dumps({"devDependencies": {"jest": "^30"}}))
    write(tmp_path, "conftest.py", "")
    assert detect_framework(tmp_path) == "pytest"


def test_detect_python_without_pytest_falls_back_to_unittest(tmp_path):
    write(tmp_path, "setup.py", "from setuptools import setup\n")
    # A project venv without pytest installed decides it, not our own env.
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python").write_text("")
    assert detect_framework(tmp_path) == "unittest"


# --------------------------------------------------------------------------
# Command construction and argument safety
# --------------------------------------------------------------------------

def test_build_plan_maps_target_and_filter(tmp_path):
    plan, _ = build_plan("pytest", tmp_path, tmp_path, target="tests/test_a.py", name_filter="login")
    assert plan.argv[-3:] == ["-k", "login", "tests/test_a.py"]
    assert any(a.startswith("--junitxml=") for a in plan.argv)

    plan, _ = build_plan("go", tmp_path, tmp_path, name_filter="TestLogin")
    assert plan.argv == ["go", "test", "-json", "-run", "TestLogin", "./..."]

    plan, _ = build_plan("unittest", tmp_path, tmp_path, name_filter="login")
    assert plan.argv[2:] == ["unittest", "discover", "-k", "login"]


@pytest.mark.parametrize("kwargs", [
    {"target": "--rootdir=/"},
    {"name_filter": "-p evil_plugin"},
    {"target": "../outside"},
])
def test_build_plan_rejects_option_injection_and_escapes(tmp_path, kwargs):
    with pytest.raises(ValueError):
        build_plan("pytest", tmp_path, tmp_path, **kwargs)


def test_build_plan_rejects_arbitrary_commands(tmp_path):
    # The old tool ran `framework.split()` verbatim, bypassing the shell
    # command policy for every role that can call run_tests.
    with pytest.raises(ValueError, match="Unknown test framework"):
        build_plan("rm -rf .", tmp_path, tmp_path)


def test_rerun_failed_notes_when_unsupported(tmp_path):
    _, notes = build_plan("go", tmp_path, tmp_path, rerun_failed=True)
    assert "not supported" in notes[0]


# --------------------------------------------------------------------------
# Parsers
# --------------------------------------------------------------------------

PYTEST_XML = """<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests"><testsuite name="pytest" errors="1" failures="2" skipped="1" tests="5" time="0.029"><testcase classname="tests.test_x" name="test_ok" file="tests/test_x.py" line="5" time="0.001" /><testcase classname="tests.test_x" name="test_bad" file="tests/test_x.py" line="8" time="0.001"><failure message="AssertionError: value mismatch&#10;assert 1 == 2">def test_bad():
        x = 1
&gt;       helper(x)

tests/test_x.py:11:
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _

    def helper(v):
&gt;       assert v == 2, "value mismatch"
E       AssertionError: value mismatch

tests/test_x.py:4: AssertionError</failure></testcase><testcase classname="tests.test_x.TestK" name="test_err" file="tests/test_x.py" line="13" time="0.000"><failure message="ValueError: kaboom">    def test_err(self):
&gt;       raise ValueError("kaboom")
E       ValueError: kaboom

/usr/lib/python3.12/json/__init__.py:99: in loads
tests/test_x.py:15: ValueError</failure></testcase><testcase classname="tests.test_x" name="test_fix" file="tests/test_x.py" line="20" time="0.000"><error message="failed on setup with &quot;RuntimeError: fixture broke&quot;">E       RuntimeError: fixture broke

tests/test_x.py:19: RuntimeError</error></testcase><testcase classname="tests.test_x" name="test_s" file="tests/test_x.py" line="23" time="0.000"><skipped type="pytest.skip" message="unconditional skip">tests/test_x.py:24: unconditional skip</skipped></testcase></testsuite></testsuites>"""


def test_parse_pytest_junit(tmp_path):
    xml = tmp_path / "r.xml"
    xml.write_text(PYTEST_XML)
    report = TestReport("pytest", [], exit_code=1)
    _parse_pytest(report, "", xml, tmp_path)

    assert (report.passed, report.failed, report.errors, report.skipped) == (1, 2, 1, 1)
    bad, err, fix = report.failures
    assert bad.name == "tests/test_x.py::test_bad"
    assert bad.message == "AssertionError: value mismatch | assert 1 == 2"
    # The innermost frame inside the project, not the test's def line.
    assert bad.location == "tests/test_x.py:4"
    assert err.name == "tests/test_x.py::TestK::test_err"
    assert err.location == "tests/test_x.py:15"  # stdlib frame skipped
    assert fix.kind == "ERROR" and "fixture broke" in fix.message


UNITTEST_OUT = """FE.
======================================================================
ERROR: test_err (test_u.T.test_err)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "{root}/tests/test_u.py", line 5, in test_err
    def test_err(self): raise KeyError("k")
                        ^^^^^^^^^^^^^^^^^^^
KeyError: 'k'

======================================================================
FAIL: test_bad (test_u.T.test_bad)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "{root}/tests/test_u.py", line 4, in test_bad
    def test_bad(self): self.assertEqual(1, 2)
AssertionError: 1 != 2

----------------------------------------------------------------------
Ran 4 tests in 0.001s

FAILED (failures=1, errors=1, skipped=1)
"""


def test_parse_unittest(tmp_path):
    report = TestReport("unittest", [], exit_code=1)
    _parse_unittest(report, UNITTEST_OUT.format(root=tmp_path), None, tmp_path)

    assert (report.passed, report.failed, report.errors, report.skipped) == (1, 1, 1, 1)
    err, bad = report.failures
    assert (err.kind, err.name, err.message, err.location) == (
        "ERROR", "test_u.T.test_err", "KeyError: 'k'", "tests/test_u.py:5")
    assert (bad.kind, bad.message, bad.location) == ("FAILED", "AssertionError: 1 != 2", "tests/test_u.py:4")


def test_parse_jest_json(tmp_path):
    root = str(tmp_path)
    data = {
        "numPassedTests": 1, "numFailedTests": 1, "numPendingTests": 1, "numTodoTests": 0,
        "startTime": 1000,
        "testResults": [
            {
                "name": f"{root}/sum.test.js", "status": "failed", "endTime": 1400, "message": "",
                "assertionResults": [
                    {"fullName": "ok", "status": "passed", "failureMessages": []},
                    {"fullName": "math bad", "status": "failed", "failureMessages": [
                        "Error: expect(received).toBe(expected) // Object.is equality\n\n"
                        "Expected: 3\nReceived: 2\n"
                        f"    at Object.toBe ({root}/sum.test.js:3:35)\n"
                        f"    at Promise.finally.completed ({root}/node_modules/jest-circus/build/x.js:1834:28)",
                    ]},
                ],
            },
            {
                "name": f"{root}/broken.test.js", "status": "failed", "assertionResults": [],
                "message": "  \x1b[1m● \x1b[22mTest suite failed to run\n\n"
                           "    Cannot find module './missing' from 'broken.test.js'\n\n"
                           "    > 1 | const x = require('./missing');\n"
                           "        |           ^\n\n"
                           f"      at Object.<anonymous> ({root}/broken.test.js:1:1)",
            },
        ],
    }
    path = tmp_path / "r.json"
    path.write_text(json.dumps(data))
    report = TestReport("jest", [], exit_code=1)
    _parse_jest_like(report, "", path, tmp_path)

    assert (report.passed, report.failed, report.errors, report.skipped) == (1, 1, 1, 1)
    bad, broken = report.failures
    assert bad.name == "sum.test.js > math bad"
    assert bad.message == ("Error: expect(received).toBe(expected) // Object.is equality"
                           " | Expected: 3 | Received: 2")
    assert bad.location == "sum.test.js:3"
    assert broken.kind == "ERROR"
    assert broken.message == "Cannot find module './missing' from 'broken.test.js'"
    assert broken.location == "broken.test.js:1"
    assert report.duration_s == pytest.approx(0.4)


def test_parse_mocha_json(tmp_path):
    data = {
        "stats": {"passes": 1, "failures": 1, "pending": 1, "duration": 2},
        "failures": [{
            "title": "bad", "fullTitle": "suite bad", "file": str(tmp_path / "mt/a.spec.js"),
            "err": {"name": "AssertionError",
                    "message": "Expected values to be strictly equal:\n\n1 !== 2\n",
                    "stack": "AssertionError [ERR_ASSERTION]: Expected values to be strictly equal:\n\n"
                             "1 !== 2\n\n    at Context.<anonymous> (mt/a.spec.js:4:28)\n"
                             "    at process.processImmediate (node:internal/timers:484:21)"},
        }],
    }
    path = tmp_path / "r.json"
    path.write_text(json.dumps(data))
    report = TestReport("mocha", [], exit_code=1)
    _parse_mocha(report, "", path, tmp_path)

    assert (report.passed, report.failed, report.skipped) == (1, 1, 1)
    (fail,) = report.failures
    assert fail.name == "suite bad"
    assert fail.message == "AssertionError: Expected values to be strictly equal: | 1 !== 2"
    assert fail.location == "mt/a.spec.js:4"


def _go_events(*events):
    return "\n".join(json.dumps(e) for e in events)


def test_parse_go_json_reports_leaf_subtests_with_package_paths(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/demo\n")
    pkg = "example.com/demo/calc"
    out = _go_events(
        {"Action": "pass", "Package": pkg, "Test": "TestOk"},
        {"Action": "output", "Package": pkg, "Test": "TestBad", "Output": "=== RUN   TestBad\n"},
        {"Action": "output", "Package": pkg, "Test": "TestBad", "Output": "    calc_test.go:7: expected 1, got 2\n"},
        {"Action": "output", "Package": pkg, "Test": "TestBad", "Output": "--- FAIL: TestBad (0.00s)\n"},
        {"Action": "fail", "Package": pkg, "Test": "TestBad"},
        {"Action": "output", "Package": pkg, "Test": "TestSub/inner", "Output": "    calc_test.go:10: inner broke\n"},
        {"Action": "fail", "Package": pkg, "Test": "TestSub/inner"},
        {"Action": "fail", "Package": pkg, "Test": "TestSub"},
        {"Action": "skip", "Package": pkg, "Test": "TestSkip"},
        {"Action": "fail", "Package": pkg, "Elapsed": 0.003},
    )
    report = TestReport("go", [], exit_code=1)
    _parse_go(report, out, None, tmp_path)

    assert (report.passed, report.failed, report.errors, report.skipped) == (1, 2, 0, 1)
    assert [(f.name, f.location, f.message) for f in report.failures] == [
        (f"{pkg}.TestBad", "calc/calc_test.go:7", "expected 1, got 2"),
        (f"{pkg}.TestSub/inner", "calc/calc_test.go:10", "inner broke"),
    ]


def test_parse_go_build_failure(tmp_path):
    out = _go_events(
        {"ImportPath": "example.com/bad [example.com/bad.test]", "Action": "build-output",
         "Output": "# example.com/bad [example.com/bad.test]\n"},
        {"ImportPath": "example.com/bad [example.com/bad.test]", "Action": "build-output",
         "Output": "./bad_test.go:3:28: undefined: undefinedThing\n"},
        {"ImportPath": "example.com/bad [example.com/bad.test]", "Action": "build-fail"},
        {"Action": "fail", "Package": "example.com/bad", "Elapsed": 0,
         "FailedBuild": "example.com/bad [example.com/bad.test]"},
    )
    report = TestReport("go", [], exit_code=1)
    _parse_go(report, out, None, tmp_path)

    (fail,) = report.failures
    assert (fail.kind, fail.name, fail.message, fail.location) == (
        "ERROR", "example.com/bad [build failed]", "undefined: undefinedThing", "bad_test.go:3")


CARGO_OUT = """running 4 tests
test tests::ign ... ignored
test tests::ok ... ok
test tests::panics ... FAILED
test tests::bad ... FAILED

failures:

---- tests::panics stdout ----

thread 'tests::panics' (2468) panicked at src/lib.rs:7:27:
boom here
note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace

---- tests::bad stdout ----

thread 'tests::bad' (2466) panicked at src/lib.rs:6:24:
assertion `left == right` failed
  left: 2
 right: 3


failures:
    tests::bad
    tests::panics

test result: FAILED. 1 passed; 2 failed; 1 ignored; 0 measured; 0 filtered out; finished in 0.16s
"""


def test_parse_cargo_text(tmp_path):
    report = TestReport("cargo", [], exit_code=101)
    _parse_cargo(report, CARGO_OUT, None, tmp_path)

    assert (report.passed, report.failed, report.skipped) == (1, 2, 1)
    assert [(f.name, f.location, f.message) for f in report.failures] == [
        ("tests::panics", "src/lib.rs:7", "boom here"),
        ("tests::bad", "src/lib.rs:6", "assertion `left == right` failed | left: 2 | right: 3"),
    ]


def test_parse_cargo_compile_error(tmp_path):
    out = ("error[E0425]: cannot find function `nope` in this scope\n"
           " --> src/lib.rs:3:5\n  |\n3 |     nope();\n  |     ^^^^ not found\n")
    report = TestReport("cargo", [], exit_code=101)
    _parse_cargo(report, out, None, tmp_path)

    (fail,) = report.failures
    assert (fail.kind, fail.location) == ("ERROR", "src/lib.rs:3")
    assert "cannot find function" in fail.message


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------

def test_format_report_is_compact_and_readable_by_parse_failures():
    report = TestReport("jest", [], exit_code=1, passed=40, failed=12, duration_s=3.2, parsed=True)
    report.failures = [
        TestFailure(f"auth.test.js > login {i}", f"expected {i}", "auth.test.js", 10 + i)
        for i in range(12)
    ]
    text = format_report(report, max_failures=10)
    lines = text.splitlines()

    assert lines[0] == "jest: did not pass (12 failed, 40 passed in 3.2s)"
    assert lines[1:3] == ["FAILED auth.test.js > login 0 - expected 0", "  at auth.test.js:10"]
    assert lines[-1].startswith("... 2 more failure(s) not shown")

    parsed = parse_failures(text)
    assert parsed[0].test == "auth.test.js > login 0"
    assert parsed[0].location == "auth.test.js:10"
    assert "- auth.test.js > login 1: expected 1 (auth.test.js:11)" in summarize_failures(text)


def test_format_report_falls_back_to_output_tail_when_unparsed():
    output = "\n".join(f"line {i}" for i in range(100)) + "\nImportError: no module named app"
    report = TestReport("pytest", ["/venv/bin/python", "-m", "pytest", "-q"], exit_code=2,
                        raw_output=output)
    text = format_report(report)

    assert text.startswith("pytest: did not pass (exit code 2)")
    assert "$ python -m pytest -q" in text
    assert text.endswith("ImportError: no module named app")
    assert "line 10\n" not in text  # only the tail is kept


def test_format_report_pass_is_one_line():
    report = TestReport("go", [], exit_code=0, passed=12, skipped=1, duration_s=0.4, parsed=True)
    assert format_report(report) == "go: passed (12 passed, 1 skipped in 0.4s)"


# --------------------------------------------------------------------------
# End to end through the tool (real subprocesses)
# --------------------------------------------------------------------------

@pytest.fixture
def py_project(tmp_path):
    write(tmp_path, "pytest.ini", "[pytest]\n")
    write(tmp_path, "calc.py", """\
        def add(a, b):
            return a - b
        """)
    write(tmp_path, "tests/test_calc.py", """\
        from calc import add

        def test_add():
            assert add(2, 2) == 4

        def test_zero():
            assert add(0, 0) == 0
        """)
    return tmp_path


def test_tool_runs_pytest_and_reports_failure_location(py_project):
    tool = RunTestsTool(str(py_project))
    result = run(tool.execute())

    assert result.success is False
    assert result.output.splitlines()[0].startswith("pytest: did not pass (1 failed, 1 passed")
    assert "FAILED tests/test_calc.py::test_add - assert 0 == 4" in result.output
    assert "  at tests/test_calc.py:4" in result.output

    # write-test-fix: fix the code, rerun only the failure
    (py_project / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    result = run(tool.execute(rerun_failed=True))
    assert result.success is True
    assert result.output.startswith("pytest: passed (1 passed")


def test_tool_filter_that_matches_nothing_fails(py_project):
    result = run(RunTestsTool(str(py_project)).execute(filter="does_not_exist"))
    assert result.success is False
    assert "no tests ran" in result.output


def test_tool_accepts_legacy_test_path_argument(py_project):
    result = run(RunTestsTool(str(py_project)).execute(test_path="tests/test_calc.py", filter="zero"))
    assert result.success is True


def test_tool_reports_timeout(tmp_path):
    write(tmp_path, "pytest.ini", "[pytest]\n")
    write(tmp_path, "tests/test_slow.py", """\
        import time

        def test_slow():
            time.sleep(30)
        """)
    result = run(RunTestsTool(str(tmp_path)).execute(timeout=2))
    assert result.success is False
    assert result.output.startswith("pytest: timed out")
    assert "Timed out after 2s" in result.output


def test_tool_runs_unittest(tmp_path):
    write(tmp_path, "tests/test_u.py", """\
        import unittest

        class T(unittest.TestCase):
            def test_ok(self):
                pass

            def test_bad(self):
                self.assertEqual(1, 2)
        """)
    result = run(RunTestsTool(str(tmp_path)).execute(framework="unittest"))
    assert result.success is False
    assert result.output.splitlines() == [
        "unittest: did not pass (1 failed, 1 passed in 0.0s)",
        "FAILED test_u.T.test_bad - AssertionError: 1 != 2",
        "  at tests/test_u.py:8",
    ]


def test_tool_without_detectable_framework_explains(tmp_path):
    result = run(RunTestsTool(str(tmp_path)).execute())
    assert result.success is False
    assert "Could not detect a test framework" in result.output


@pytest.mark.skipif(shutil.which("go") is None, reason="go not installed")
def test_tool_runs_go(tmp_path):
    write(tmp_path, "go.mod", "module example.com/demo\n\ngo 1.21\n")
    write(tmp_path, "demo_test.go", """\
        package demo

        import "testing"

        func TestOk(t *testing.T) {}
        func TestBad(t *testing.T) { t.Errorf("expected 1, got %d", 2) }
        """)
    result = run(RunTestsTool(str(tmp_path)).execute())
    assert result.success is False
    assert "FAILED example.com/demo.TestBad - expected 1, got 2\n  at demo_test.go:6" in result.output


# --------------------------------------------------------------------------
# Coordinator: verification runs tests without a model
# --------------------------------------------------------------------------

def test_coordinator_runs_detected_tests_without_a_model(py_project):
    config = ProjectConfig(project_root=str(py_project))
    coordinator = Coordinator(config=config, project_root=str(py_project))

    async def no_model(role):
        raise AssertionError("verification should not need a model")

    coordinator.model_manager.get_model = no_model
    response = run(coordinator._run_tests())

    assert response.status == TaskStatus.FAILED
    assert response.tests_passed is False
    assert "tests/test_calc.py:4" in response.summary
    assert "tests/test_calc.py:4" in summarize_failures(response.summary)
