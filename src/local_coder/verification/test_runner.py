"""Detect a project's test framework, run it, and parse failures compactly.

The goal is a write-test-fix loop that costs the model very few tokens: rather
than handing it a full test log, each run is reduced to one line of counts and
a short list of failures (test name, file:line, one-line message). Frameworks
that can emit a machine-readable report (pytest JUnit XML, jest/vitest/mocha
JSON, ``go test -json``) are parsed from that report; the others (unittest,
cargo) are parsed from their plain-text output. When nothing can be parsed --
a collection error, a crash, a config problem -- the tail of the raw output is
returned instead so the model still sees what went wrong.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import shutil
import signal
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

FRAMEWORKS = ("pytest", "unittest", "jest", "vitest", "mocha", "go", "cargo", "npm", "make")

DEFAULT_TIMEOUT_S = 600
MAX_TIMEOUT_S = 1800
# Raw output is only kept for the fallback tail; cap what we hold in memory.
_MAX_RAW_CHARS = 200_000
_FALLBACK_TAIL_LINES = 40
_FALLBACK_TAIL_CHARS = 3000

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_CODE_FRAME_RE = re.compile(r"^>?\s*\d*\s*\|")


@dataclass
class TestFailure:
    __test__ = False  # not a pytest test class

    name: str
    message: str
    file: str | None = None
    line: int | None = None
    kind: str = "FAILED"  # FAILED (assertion) or ERROR (setup, crash, build)

    @property
    def location(self) -> str | None:
        if not self.file:
            return None
        return f"{self.file}:{self.line}" if self.line else self.file


@dataclass
class TestReport:
    __test__ = False

    framework: str
    command: list[str]
    exit_code: int | None = None
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    duration_s: float = 0.0
    failures: list[TestFailure] = field(default_factory=list)
    timed_out: bool = False
    parsed: bool = False  # structured results were found
    raw_output: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.errors + self.skipped

    @property
    def no_tests(self) -> bool:
        """The run worked but found nothing to run."""
        return self.parsed and self.total == 0 and not self.failures and not self.timed_out

    @property
    def ok(self) -> bool:
        if self.timed_out or self.failed or self.errors:
            return False
        # A structured report with no failures but a nonzero exit (e.g. a
        # coverage threshold, or pytest's "no tests collected") is not a pass.
        return self.exit_code == 0 or (self.parsed and self.exit_code is None)


@dataclass
class _Plan:
    framework: str
    argv: list[str]
    parse: Callable[[TestReport, str, Path | None, Path], None]
    report_path: Path | None = None


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _package_json(root: Path) -> dict:
    try:
        data = json.loads(_read(root / "package.json") or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _js_framework(root: Path) -> str | None:
    pkg = _package_json(root)
    if not pkg:
        return None
    deps: dict = {}
    for key in ("dependencies", "devDependencies"):
        if isinstance(pkg.get(key), dict):
            deps.update(pkg[key])
    test_script = str((pkg.get("scripts") or {}).get("test", ""))
    for name in ("vitest", "jest", "mocha"):
        if name in deps or re.search(rf"\b{name}\b", test_script):
            return name
    if test_script and "no test specified" not in test_script:
        return "npm"
    return None


def _has_pytest_config(root: Path) -> bool:
    if (root / "pytest.ini").exists() or (root / "conftest.py").exists():
        return True
    if (root / "tests" / "conftest.py").exists():
        return True
    if "[tool.pytest" in _read(root / "pyproject.toml"):
        return True
    return any(
        marker in _read(root / name)
        for name, marker in (("setup.cfg", "[tool:pytest]"), ("tox.ini", "[pytest]"))
    )


def _is_python_project(root: Path) -> bool:
    markers = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")
    if any((root / m).exists() for m in markers):
        return True
    return any(root.glob("test_*.py")) or any(root.glob("tests/test_*.py"))


def _python_for(root: Path) -> str:
    """Prefer the project's own virtualenv so its dependencies are importable."""
    for venv in (".venv", "venv", "env"):
        for rel in ("bin/python", "Scripts/python.exe"):
            candidate = root / venv / rel
            if candidate.exists():
                return str(candidate)
    return sys.executable or shutil.which("python3") or "python3"


def _pytest_available(root: Path, python: str) -> bool:
    if python == sys.executable:
        return importlib.util.find_spec("pytest") is not None
    # A project venv: look for the installed package rather than spawning it.
    venv = Path(python).parent.parent
    return any(venv.glob("lib/python*/site-packages/pytest")) or any(
        venv.glob("Lib/site-packages/pytest")
    )


def detect_framework(project_root: str | os.PathLike[str]) -> str | None:
    """Best guess at the test framework for a project, or None."""
    root = Path(project_root)
    if _has_pytest_config(root):
        return "pytest"
    if (root / "Cargo.toml").exists():
        return "cargo"
    if (root / "go.mod").exists():
        return "go"
    js = _js_framework(root)
    if js and js != "npm":
        return js
    if _is_python_project(root):
        python = _python_for(root)
        mentions_pytest = any(
            "pytest" in _read(root / name)
            for name in ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
                         "requirements-dev.txt", "requirements_dev.txt")
        )
        if mentions_pytest or _pytest_available(root, python):
            return "pytest"
        return "unittest"
    if js:
        return js
    if re.search(r"(?m)^test\s*:", _read(root / "Makefile")):
        return "make"
    return None


# --------------------------------------------------------------------------
# Shared parsing helpers
# --------------------------------------------------------------------------

def _clean(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def _one_line(text: str, limit: int = 240) -> str:
    """Collapse the first few meaningful lines of a message into one line."""
    lines = [ln.strip() for ln in _clean(text).splitlines()]
    # Drop stack frames and source code frames ("> 12 | foo()", "|    ^").
    lines = [ln for ln in lines
             if ln and not ln.startswith("at ") and not _CODE_FRAME_RE.match(ln)][:3]
    out = " | ".join(lines)
    return out if len(out) <= limit else out[: limit - 3] + "..."


def _relative(root: Path, raw: str) -> str | None:
    """Workspace-relative path for a frame, or None if outside the project or
    inside dependencies (site-packages, node_modules, the stdlib)."""
    raw = raw.removeprefix("file://")
    if raw.startswith("<") or raw.startswith("node:"):
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    try:
        rel = Path(os.path.normpath(path)).relative_to(root)
    except ValueError:
        return None
    parts = set(rel.parts)
    if parts & {"node_modules", "site-packages", ".venv", "venv", ".cargo"}:
        return None
    return rel.as_posix()


_PY_FRAME = re.compile(r"^(?P<file>[^\s:][^:\n]*\.py):(?P<line>\d+):", re.M)
_PY_TRACE_FRAME = re.compile(r'File "(?P<file>[^"]+)", line (?P<line>\d+)')
_JS_FRAME = re.compile(r"(?P<file>(?:file://)?[^\s()]+\.[cm]?[jt]sx?):(?P<line>\d+):\d+")


def _last_frame(root: Path, text: str, pattern: re.Pattern) -> tuple[str | None, int | None]:
    found: tuple[str | None, int | None] = (None, None)
    for m in pattern.finditer(text):
        rel = _relative(root, m.group("file"))
        if rel:
            found = (rel, int(m.group("line")))
    return found


def _first_frame(root: Path, text: str, pattern: re.Pattern) -> tuple[str | None, int | None]:
    for m in pattern.finditer(text):
        rel = _relative(root, m.group("file"))
        if rel:
            return rel, int(m.group("line"))
    return None, None


# --------------------------------------------------------------------------
# Parsers: each fills in a TestReport from a report file and/or raw output
# --------------------------------------------------------------------------

def _parse_pytest(report: TestReport, output: str, report_path: Path | None, root: Path) -> None:
    if not report_path or not report_path.exists():
        return
    try:
        tree = ET.parse(report_path)
    except ET.ParseError:
        return
    report.parsed = True
    for case in tree.iter("testcase"):
        file = case.get("file") or ""
        classname = case.get("classname") or ""
        name = case.get("name") or "?"
        # classname is "pkg.module.Class"; the module part mirrors the file.
        module = file[:-3].replace("/", ".") if file.endswith(".py") else ""
        cls = classname[len(module) + 1:] if module and classname.startswith(module + ".") else ""
        node_id = "::".join(p for p in (file or classname, cls, name) if p)
        if case.find("skipped") is not None:
            report.skipped += 1
            continue
        problem = case.find("failure")
        kind = "FAILED"
        if problem is None:
            problem = case.find("error")
            kind = "ERROR"
        if problem is None:
            report.passed += 1
            continue
        if kind == "FAILED":
            report.failed += 1
        else:
            report.errors += 1
        detail = problem.text or ""
        loc_file, loc_line = _last_frame(root, detail, _PY_FRAME)
        if not loc_file and file:
            line = case.get("line")
            loc_file, loc_line = file, (int(line) + 1 if line and line.isdigit() else None)
        report.failures.append(TestFailure(
            name=node_id,
            message=_one_line(problem.get("message") or detail),
            file=loc_file, line=loc_line, kind=kind,
        ))
    suite = tree.find(".//testsuite")
    if suite is not None:
        try:
            report.duration_s = float(suite.get("time") or 0)
        except ValueError:
            pass


_UNITTEST_HEAD = re.compile(r"^(FAIL|ERROR): (\S+) \(([^)]+)\)", re.M)
_UNITTEST_RAN = re.compile(r"^Ran (\d+) tests? in ([\d.]+)s", re.M)
_UNITTEST_TAIL = re.compile(r"^(?:FAILED|OK)\s*(?:\((.*)\))?\s*$", re.M)


def _parse_unittest(report: TestReport, output: str, report_path: Path | None, root: Path) -> None:
    ran = _UNITTEST_RAN.search(output)
    if not ran:
        return
    report.parsed = True
    report.duration_s = float(ran.group(2))
    counts: dict[str, int] = {}
    tail = _UNITTEST_TAIL.search(output, ran.end())
    if tail and tail.group(1):
        for part in tail.group(1).split(","):
            key, _, val = part.strip().partition("=")
            if val.isdigit():
                counts[key] = int(val)
    report.failed = counts.get("failures", 0)
    report.errors = counts.get("errors", 0)
    report.skipped = counts.get("skipped", 0)
    report.passed = max(
        int(ran.group(1)) - report.failed - report.errors - report.skipped
        - counts.get("expected failures", 0), 0,
    )
    heads = list(_UNITTEST_HEAD.finditer(output))
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else ran.start()
        block = output[m.end():end]
        body = [ln for ln in block.splitlines() if ln.strip() and not set(ln.strip()) <= set("-=")]
        # The exception line is the last unindented line of the traceback.
        message = next((ln for ln in reversed(body) if not ln.startswith((" ", "\t"))), "")
        file, line = _last_frame(root, block, _PY_TRACE_FRAME)
        report.failures.append(TestFailure(
            name=m.group(3), message=_one_line(message),
            file=file, line=line, kind="FAILED" if m.group(1) == "FAIL" else "ERROR",
        ))


def _parse_jest_like(report: TestReport, output: str, report_path: Path | None, root: Path) -> None:
    """jest and vitest share the same JSON result shape."""
    if not report_path or not report_path.exists():
        return
    try:
        data = json.loads(_read(report_path))
    except json.JSONDecodeError:
        return
    report.parsed = True
    report.passed = int(data.get("numPassedTests", 0))
    report.failed = int(data.get("numFailedTests", 0))
    report.skipped = int(data.get("numPendingTests", 0)) + int(data.get("numTodoTests", 0))
    start = data.get("startTime")
    for suite in data.get("testResults", []):
        suite_file = _relative(root, suite.get("name", "")) or suite.get("name", "?")
        failed_here = False
        for case in suite.get("assertionResults", []):
            if case.get("status") != "failed":
                continue
            failed_here = True
            text = "\n".join(case.get("failureMessages") or [])
            file, line = _first_frame(root, text, _JS_FRAME)
            loc = case.get("location") or {}
            if not file and loc.get("line"):
                file, line = suite_file, int(loc["line"])
            report.failures.append(TestFailure(
                name=f"{suite_file} > {case.get('fullName') or case.get('title')}",
                message=_one_line(text), file=file or suite_file, line=line,
            ))
        if suite.get("status") == "failed" and not failed_here:
            # The file itself failed to load (syntax error, bad import...).
            report.errors += 1
            message = re.sub(r"^\s*●.*$", "", _clean(suite.get("message", "")), flags=re.M)
            file, line = _first_frame(root, message, _JS_FRAME)
            report.failures.append(TestFailure(
                name=suite_file, message=_one_line(message) or "test file failed to run",
                file=file or suite_file, line=line, kind="ERROR",
            ))
        if start and suite.get("endTime"):
            report.duration_s = max(report.duration_s, (suite["endTime"] - start) / 1000)


def _parse_mocha(report: TestReport, output: str, report_path: Path | None, root: Path) -> None:
    if not report_path or not report_path.exists():
        return
    try:
        data = json.loads(_read(report_path))
    except json.JSONDecodeError:
        return
    report.parsed = True
    stats = data.get("stats", {})
    report.passed = int(stats.get("passes", 0))
    report.failed = int(stats.get("failures", 0))
    report.skipped = int(stats.get("pending", 0))
    report.duration_s = float(stats.get("duration", 0)) / 1000
    for case in data.get("failures", []):
        err = case.get("err") or {}
        stack = err.get("stack") or ""
        file, line = _first_frame(root, stack, _JS_FRAME)
        message = err.get("message") or stack
        if err.get("name") and err["name"] not in message:
            message = f"{err['name']}: {message}"
        report.failures.append(TestFailure(
            name=case.get("fullTitle") or case.get("title") or "?",
            message=_one_line(message),
            file=file or _relative(root, case.get("file", "")), line=line,
        ))


_GO_OUT_LOC = re.compile(r"^\s+(?P<file>[\w./-]+\.go):(?P<line>\d+): ?(?P<msg>.*)$")
_GO_ABS_LOC = re.compile(r"(?P<file>/[^\s:]+\.go):(?P<line>\d+)")
_GO_BUILD_LOC = re.compile(r"^(?P<file>[^\s:]+\.go):(?P<line>\d+):\d+: (?P<msg>.*)$")


def _go_module(root: Path) -> str:
    m = re.search(r"(?m)^module\s+(\S+)", _read(root / "go.mod"))
    return m.group(1) if m else ""


def _parse_go(report: TestReport, output: str, report_path: Path | None, root: Path) -> None:
    module = _go_module(root)

    def pkg_dir(pkg: str) -> str:
        if module and (pkg == module or pkg.startswith(module + "/")):
            return pkg[len(module):].strip("/")
        return ""

    test_output: dict[tuple[str, str], list[str]] = {}
    pkg_output: dict[str, list[str]] = {}
    build_output: dict[str, list[str]] = {}
    results: dict[tuple[str, str], str] = {}
    failed_pkgs: dict[str, dict] = {}
    for raw in output.splitlines():
        if not raw.startswith("{"):
            continue
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue
        report.parsed = True
        action = ev.get("Action")
        pkg = ev.get("Package") or ev.get("ImportPath", "")
        test = ev.get("Test")
        if action == "build-output":
            build_output.setdefault(pkg.split(" ")[0], []).append(ev.get("Output", ""))
        elif action == "output":
            target = test_output.setdefault((pkg, test), []) if test else pkg_output.setdefault(pkg, [])
            target.append(ev.get("Output", ""))
        elif action in ("pass", "fail", "skip"):
            if test:
                results[(pkg, test)] = action
            elif action == "fail":
                failed_pkgs[pkg] = ev
            if not test and ev.get("Elapsed"):
                report.duration_s += float(ev["Elapsed"])

    failed_tests = [k for k, v in results.items() if v == "fail"]
    for (pkg, test), action in results.items():
        if action == "pass":
            report.passed += 1
        elif action == "skip":
            report.skipped += 1
    for pkg, test in failed_tests:
        # A parent test fails whenever a subtest does; report only the leaf.
        if any(p == pkg and t.startswith(test + "/") for p, t in failed_tests):
            continue
        report.failed += 1
        lines = [ln.rstrip("\n") for ln in test_output.get((pkg, test), [])]
        lines = [ln for ln in lines if not ln.startswith(("=== ", "--- ", "    --- "))]
        file = line = None
        message = ""
        for ln in lines:
            m = _GO_OUT_LOC.match(ln)
            if m:
                d = pkg_dir(pkg)
                file = f"{d}/{m.group('file')}" if d else m.group("file")
                line, message = int(m.group("line")), m.group("msg")
                break
        if not file:  # panics carry absolute paths in the goroutine trace
            file, line = _first_frame(root, "\n".join(lines), _GO_ABS_LOC)
            message = next((ln for ln in lines if ln.strip().startswith("panic:")), "")
        report.failures.append(TestFailure(
            name=f"{pkg}.{test}" if pkg else test,
            message=_one_line(message or "\n".join(lines)) or "test failed",
            file=file, line=line,
        ))
    for pkg, ev in failed_pkgs.items():
        if any(p == pkg for p, _ in failed_tests):
            continue
        report.errors += 1
        if ev.get("FailedBuild"):
            lines = [ln.rstrip("\n") for ln in build_output.get(pkg, [])]
            file = line = None
            message = ""
            for ln in lines:
                m = _GO_BUILD_LOC.match(ln)
                if m:
                    file = _relative(root, m.group("file"))
                    line, message = int(m.group("line")), m.group("msg")
                    break
            report.failures.append(TestFailure(
                name=f"{pkg} [build failed]", message=message or _one_line("\n".join(lines)),
                file=file, line=line, kind="ERROR",
            ))
        else:
            lines = [ln.rstrip("\n") for ln in pkg_output.get(pkg, [])]
            file, line = _first_frame(root, "\n".join(lines), _GO_ABS_LOC)
            useful = [ln for ln in lines if ln.strip() and not ln.startswith(("FAIL", "ok "))]
            report.failures.append(TestFailure(
                name=pkg, message=_one_line("\n".join(useful[:3])) or "package failed",
                file=file, line=line, kind="ERROR",
            ))


_CARGO_RESULT = re.compile(
    r"^test result: \w+\. (\d+) passed; (\d+) failed; (\d+) ignored; \d+ measured; "
    r"\d+ filtered out(?:; finished in ([\d.]+)s)?", re.M,
)
_CARGO_FAILED_LINE = re.compile(r"^test (\S+) \.\.\. FAILED$", re.M)
_CARGO_PANIC = re.compile(r"panicked at (?P<file>[^\s:]+):(?P<line>\d+):\d+:\n(?P<msg>(?:.+\n?){1,3})")
_CARGO_COMPILE_ERR = re.compile(r"^error(?:\[\w+\])?: (?P<msg>.+)\n\s*--> (?P<file>[^\s:]+):(?P<line>\d+)", re.M)


def _parse_cargo(report: TestReport, output: str, report_path: Path | None, root: Path) -> None:
    for m in _CARGO_RESULT.finditer(output):
        report.parsed = True
        report.passed += int(m.group(1))
        report.failed += int(m.group(2))
        report.skipped += int(m.group(3))
        report.duration_s += float(m.group(4) or 0)
    for m in _CARGO_FAILED_LINE.finditer(output):
        name = m.group(1)
        block = re.search(
            rf"^---- {re.escape(name)} stdout ----\n(.*?)(?=^---- |^failures:|\Z)",
            output, re.M | re.S,
        )
        text = block.group(1) if block else ""
        text = re.sub(r"(?m)^note: run with `RUST_BACKTRACE.*$", "", text)
        panic = _CARGO_PANIC.search(text)
        report.failures.append(TestFailure(
            name=name,
            message=_one_line(panic.group("msg") if panic else text) or "test failed",
            file=panic.group("file") if panic else None,
            line=int(panic.group("line")) if panic else None,
        ))
    if not report.parsed:
        for m in _CARGO_COMPILE_ERR.finditer(output):
            report.parsed = True
            report.errors += 1
            report.failures.append(TestFailure(
                name="[build failed]", message=_one_line(m.group("msg")),
                file=m.group("file"), line=int(m.group("line")), kind="ERROR",
            ))


def _parse_nothing(report: TestReport, output: str, report_path: Path | None, root: Path) -> None:
    """npm/make scripts: the exit code is all we can rely on."""


# --------------------------------------------------------------------------
# Command construction
# --------------------------------------------------------------------------

def _js_bin(root: Path, name: str) -> list[str]:
    local = root / "node_modules" / ".bin" / name
    if local.exists():
        return [str(local)]
    return ["npx", "--no-install", name]


def _check_arg(value: str | None, label: str) -> str | None:
    if value is None or value == "":
        return None
    if value.startswith("-"):
        raise ValueError(f"{label} must not start with '-': {value!r}")
    return value


def build_plan(
    framework: str,
    root: Path,
    report_dir: Path,
    target: str | None = None,
    name_filter: str | None = None,
    rerun_failed: bool = False,
) -> tuple[_Plan, list[str]]:
    """Command line and parser for a framework. Returns (plan, notes)."""
    notes: list[str] = []
    target = _check_arg(target, "target")
    name_filter = _check_arg(name_filter, "filter")
    if target and framework not in ("go", "cargo"):
        from local_coder.workspace import Workspace
        Workspace(root).resolve(target)  # reject paths outside the project
    report_path = report_dir / "report"

    if rerun_failed and framework not in ("pytest", "jest"):
        notes.append(f"rerun_failed is not supported for {framework}; ran the full selection.")

    if framework == "pytest":
        report_path = report_path.with_suffix(".xml")
        argv = [_python_for(root), "-m", "pytest", "-q", "--tb=short", "--color=no",
                "-o", "junit_family=xunit1", f"--junitxml={report_path}"]
        if rerun_failed:
            argv.append("--lf")
        if name_filter:
            argv += ["-k", name_filter]
        if target:
            argv.append(target)
        return _Plan(framework, argv, _parse_pytest, report_path), notes

    if framework == "unittest":
        argv = [_python_for(root), "-m", "unittest"]
        if not target:
            # "discover" has to come first; its options follow it.
            argv.append("discover")
            if (root / "tests").is_dir() and not (root / "tests" / "__init__.py").exists():
                argv += ["-s", "tests"]
        if name_filter:
            argv += ["-k", name_filter]
        if target:
            argv.append(target)
        return _Plan(framework, argv, _parse_unittest), notes

    if framework == "jest":
        report_path = report_path.with_suffix(".json")
        argv = _js_bin(root, "jest") + ["--ci", "--json", f"--outputFile={report_path}"]
        if rerun_failed:
            argv.append("--onlyFailures")
        if name_filter:
            argv += ["--testNamePattern", name_filter]
        if target:
            argv.append(target)
        return _Plan(framework, argv, _parse_jest_like, report_path), notes

    if framework == "vitest":
        report_path = report_path.with_suffix(".json")
        argv = _js_bin(root, "vitest") + ["run", "--reporter=json", f"--outputFile={report_path}"]
        if name_filter:
            argv += ["-t", name_filter]
        if target:
            argv.append(target)
        return _Plan(framework, argv, _parse_jest_like, report_path), notes

    if framework == "mocha":
        report_path = report_path.with_suffix(".json")
        argv = _js_bin(root, "mocha") + ["--reporter", "json",
                                         "--reporter-option", f"output={report_path}"]
        if name_filter:
            argv += ["--grep", name_filter]
        if target:
            argv.append(target)
        return _Plan(framework, argv, _parse_mocha, report_path), notes

    if framework == "go":
        argv = ["go", "test", "-json"]
        if name_filter:
            argv += ["-run", name_filter]
        argv.append(target or "./...")
        return _Plan(framework, argv, _parse_go), notes

    if framework == "cargo":
        argv = ["cargo", "test", "--color", "never"]
        if target:
            is_test_file = (root / "tests" / f"{target}.rs").exists()
            argv += ["--test", target] if is_test_file else ["-p", target]
        if name_filter:
            argv.append(name_filter)
        return _Plan(framework, argv, _parse_cargo), notes

    if framework == "npm":
        argv = ["npm", "test", "--silent"]
        if target:
            argv += ["--", target]
        if name_filter:
            notes.append("filter is not supported for a plain npm test script; ignored.")
        return _Plan(framework, argv, _parse_nothing), notes

    if framework == "make":
        if target or name_filter:
            notes.append("target/filter are not supported for make test; ignored.")
        return _Plan(framework, ["make", "test"], _parse_nothing), notes

    raise ValueError(f"Unknown test framework {framework!r}; expected one of {', '.join(FRAMEWORKS)}")


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------

def _test_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "CI": "1", "NO_COLOR": "1", "FORCE_COLOR": "0", "PY_COLORS": "0",
        "RUST_BACKTRACE": "0", "CARGO_TERM_COLOR": "never",
        # An agent can edit a file twice within one second without changing its
        # size; a cached .pyc would then hide the second edit.
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return env


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError):
        pass


async def run_tests(
    project_root: str | os.PathLike[str],
    framework: str = "auto",
    target: str | None = None,
    name_filter: str | None = None,
    rerun_failed: bool = False,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> TestReport:
    """Run the project's tests and return a parsed report."""
    root = Path(project_root).resolve()
    if framework in ("", "auto", None):
        detected = detect_framework(root)
        if detected is None:
            raise ValueError(
                "Could not detect a test framework. Pass framework explicitly "
                f"({', '.join(FRAMEWORKS)})."
            )
        framework = detected
    timeout = max(1.0, min(float(timeout or DEFAULT_TIMEOUT_S), MAX_TIMEOUT_S))

    with tempfile.TemporaryDirectory(prefix="local-coder-tests-") as tmp:
        plan, notes = build_plan(framework, root, Path(tmp), target, name_filter, rerun_failed)
        report = TestReport(framework=framework, command=plan.argv, notes=notes)
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *plan.argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
                cwd=str(root),
                env=_test_env(),
                start_new_session=(os.name == "posix"),
            )
        except FileNotFoundError:
            report.notes.append(f"{plan.argv[0]} was not found; is {framework} installed?")
            return report
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            _kill_tree(proc)
            await proc.wait()
            report.timed_out = True
            report.duration_s = time.monotonic() - started
            report.notes.append(f"Timed out after {int(timeout)}s.")
            return report
        output = _clean(stdout.decode("utf-8", errors="replace"))
        report.exit_code = proc.returncode
        report.raw_output = output[-_MAX_RAW_CHARS:]
        plan.parse(report, output, plan.report_path, root)
        for failure in report.failures:
            failure.message = failure.message.replace(f"{root}{os.sep}", "")
        if not report.duration_s:
            report.duration_s = time.monotonic() - started
        return report


# --------------------------------------------------------------------------
# Formatting for model context
# --------------------------------------------------------------------------

def _counts(report: TestReport) -> str:
    parts = []
    for n, label in ((report.failed, "failed"), (report.errors, "error" + ("s" if report.errors != 1 else "")),
                     (report.passed, "passed"), (report.skipped, "skipped")):
        if n:
            parts.append(f"{n} {label}")
    return ", ".join(parts) or "no tests ran"


def format_report(report: TestReport, max_failures: int = 10) -> str:
    """Compact, model-friendly summary of a test run.

    Failure lines use the ``FAILED <name> - <message>`` shape (plus an
    ``  at <file:line>`` line) so verification.failures.parse_failures can
    re-read them.
    """
    status = "passed" if report.ok else "did not pass"
    if report.timed_out:
        status = "timed out"
    head = f"{report.framework}: {status}"
    if report.parsed or report.total:
        head += f" ({_counts(report)} in {report.duration_s:.1f}s)"
    elif report.exit_code is not None:
        head += f" (exit code {report.exit_code})"
    lines = [head]
    lines += [f"note: {n}" for n in report.notes]

    for failure in report.failures[:max_failures]:
        lines.append(f"{failure.kind} {failure.name} - {failure.message}")
        if failure.location:
            lines.append(f"  at {failure.location}")
    hidden = len(report.failures) - max_failures
    if hidden > 0:
        lines.append(f"... {hidden} more failure(s) not shown; narrow with target or filter.")

    # Nothing structured explains the failure: show the tail of the log.
    if not report.ok and not report.failures and report.raw_output.strip():
        tail = report.raw_output.strip().splitlines()[-_FALLBACK_TAIL_LINES:]
        text = "\n".join(tail)[-_FALLBACK_TAIL_CHARS:]
        shown = [a for a in report.command if "local-coder-tests-" not in a]
        if shown:
            lines.append("$ " + " ".join([Path(shown[0]).name, *shown[1:]]))
        lines.append(text)
    return "\n".join(lines)
