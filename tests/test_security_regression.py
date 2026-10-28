"""Command-level checks of the isolated local regression runner."""
import asyncio
import sys
from pathlib import Path

import pytest

from local_coder.security import regression


IMAGE_ID = "sha256:" + "a" * 64


def setup_run(tmp_path, monkeypatch, xml, code=0, timeout=False):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_poc.py").write_text("def test_poc(): assert True\n")
    calls = []
    monkeypatch.setattr(regression.shutil, "which", lambda name: "/usr/bin/" + name)

    async def command(args, seconds):
        calls.append(args)
        if args[1:3] == ["image", "inspect"]:
            return 0, IMAGE_ID + "\n"
        if args[1] == "run":
            if timeout:
                raise asyncio.TimeoutError
            mount = next(arg for arg in args if arg.endswith("dst=/report.xml"))
            destination = mount.split("src=", 1)[1].split(",dst=", 1)[0]
            if xml is not None:
                Path(destination).write_text(xml)
            return code, "pytest output"
        return 0, ""

    monkeypatch.setattr(regression, "_command", command)
    return calls


def run(tmp_path, **kwargs):
    return asyncio.run(regression.run_regression(tmp_path, "tests/test_poc.py", "trusted:test", **kwargs))


def test_pass_and_container_hardening(tmp_path, monkeypatch):
    calls = setup_run(tmp_path, monkeypatch, '<testsuites><testsuite><testcase classname="poc" name="test_ok"/></testsuite></testsuites>')
    result = run(tmp_path)
    assert result["status"] == "passed"
    assert result["test_ids"] == ["poc::test_ok"]
    assert result["image_id"] == IMAGE_ID
    args = calls[1]
    for flag, value in [("--network", "none"), ("--user", "65534:65534"),
                        ("--cap-drop", "ALL"), ("--security-opt", "no-new-privileges"),
                        ("--pull=never", None), ("--read-only", None),
                        ("--memory", "512m"), ("--cpus", "1"), ("--pids-limit", "128")]:
        assert flag in args
        if value:
            assert args[args.index(flag) + 1] == value
    assert any("dst=/project,readonly" in arg for arg in args)
    assert "--noconftest" in args
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1" in args
    assert calls[-1][1:3] == ["rm", "--force"]


@pytest.mark.parametrize("xml,code,status", [
    ('<testsuite><testcase name="fail"><properties><property name="local_failure_kind" value="assertion"/></properties><failure/></testcase></testsuite>', 1, "failed"),
    ('<testsuite><testcase name="import"><error/></testcase></testsuite>', 2, "error"),
    ('<testsuite><testcase name="setup"><error/></testcase></testsuite>', 1, "error"),
    ('<testsuite><testcase name="skip"><skipped/></testcase></testsuite>', 0, "error"),
    ('<testsuite/>', 0, "error"),
    ('<testsuite><testcase name="ok"/></testsuite>', 1, "error"),
    ('<testsuite><testcase name="same"/><testcase name="same"/></testsuite>', 0, "error"),
    ('broken XML', 0, "error"),
    (None, 0, "error"),
    ('<!DOCTYPE x><testsuite/>', 0, "error"),
])
def test_classification(tmp_path, monkeypatch, xml, code, status):
    setup_run(tmp_path, monkeypatch, xml, code)
    assert run(tmp_path)["status"] == status


def test_timeout_cleans_up(tmp_path, monkeypatch):
    calls = setup_run(tmp_path, monkeypatch, None, timeout=True)
    assert run(tmp_path)["status"] == "timeout"
    assert calls[-1][1:3] == ["rm", "--force"]


def test_image_never_pulled(tmp_path, monkeypatch):
    calls = setup_run(tmp_path, monkeypatch, None)

    async def missing(args, timeout):
        calls.append(args)
        return 1, "No such image"

    monkeypatch.setattr(regression, "_command", missing)
    assert run(tmp_path)["status"] == "unavailable"
    assert len(calls) == 1
    assert calls[0][1:3] == ["image", "inspect"]


def test_scope_and_input_validation(tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("pass")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "escape.py").symlink_to(outside)
    for path in ("outside.py", "tests/escape.py", "tests/missing.py"):
        result = asyncio.run(regression.run_regression(tmp_path, path, "trusted:test"))
        assert result["status"] == "error"
    assert run(tmp_path, backend="shell")["status"] == "error"
    assert run(tmp_path, timeout_seconds=0)["status"] == "error"


def test_output_is_bounded():
    code, output = asyncio.run(regression._command([
        sys.executable, "-c", "print('x' * 100000)"
    ], 5))
    assert code == 0
    assert len(output) <= regression._OUTPUT_LIMIT


def test_cancellation_cleans_up(tmp_path, monkeypatch):
    calls = setup_run(tmp_path, monkeypatch, None)
    original = regression._command

    async def cancel_run(args, timeout):
        if args[1] == "run":
            calls.append(args)
            raise asyncio.CancelledError
        return await original(args, timeout)

    monkeypatch.setattr(regression, "_command", cancel_run)
    with pytest.raises(asyncio.CancelledError):
        run(tmp_path)
    assert calls[-1][1:3] == ["rm", "--force"]


def test_runtime_unavailable(tmp_path, monkeypatch):
    setup_run(tmp_path, monkeypatch, None)
    monkeypatch.setattr(regression.shutil, "which", lambda name: None)
    assert run(tmp_path)["status"] == "unavailable"


@pytest.mark.parametrize("flag", ["O_NOFOLLOW", "O_NONBLOCK"])
def test_missing_secure_file_primitives_fail_before_commands(tmp_path, monkeypatch, flag):
    calls = setup_run(tmp_path, monkeypatch, None)
    monkeypatch.delattr(regression.os, flag, raising=False)
    result = run(tmp_path)
    assert result["status"] == "unavailable"
    assert "POSIX" in result["error"]
    assert calls == []
    assert result["snapshot_exclusions"] == []


@pytest.mark.parametrize("flag", ["O_NOFOLLOW", "O_NONBLOCK"])
def test_snapshot_missing_secure_file_primitives_has_no_side_effects(tmp_path, monkeypatch, flag):
    source = tmp_path / "original"
    source.mkdir()
    (source / "app.py").write_text("pass")
    target = tmp_path / "snapshot"
    monkeypatch.delattr(regression.os, flag, raising=False)
    with pytest.raises(ValueError, match="POSIX"):
        regression._snapshot(source, target)
    assert not target.exists()


def test_report_read_is_bounded(tmp_path, monkeypatch):
    setup_run(tmp_path, monkeypatch, "x" * (regression._REPORT_LIMIT + 1))
    result = run(tmp_path)
    assert result["status"] == "error"
    assert "oversized" in result["error"]


def test_nonassertion_failure_is_error(tmp_path, monkeypatch):
    setup_run(tmp_path, monkeypatch, '<testsuite><testcase name="fail"><properties><property name="local_failure_kind" value="other"/></properties><failure/></testcase></testsuite>', 1)
    result = run(tmp_path)
    assert result["status"] == "error"
    assert result["failure_kind"] == "other"


def test_snapshot_filters_private_files_and_normalizes_modes(tmp_path):
    source = tmp_path / "original"
    source.mkdir(mode=0o700)
    (source / "src").mkdir(mode=0o700)
    (source / "src" / "app.py").write_text("pass")
    (source / "src" / "app.py").chmod(0o600)
    for directory in (".git", ".venv", "node_modules", "secrets"):
        (source / directory).mkdir()
        (source / directory / "private").write_text("sensitive")
    (source / ".env").write_text("sensitive")
    (source / "linked.py").symlink_to(source / "src" / "app.py")
    target = tmp_path / "snapshot"
    excluded = regression._snapshot(source, target)
    assert {".git", ".venv", "node_modules", "secrets", ".env", "linked.py"} <= set(excluded)
    assert (target / "src" / "app.py").read_text() == "pass"
    assert (target.stat().st_mode & 0o777) == 0o755
    assert ((target / "src" / "app.py").stat().st_mode & 0o777) == 0o644
    assert not (target / ".env").exists()


def test_snapshot_limits_fail_closed(tmp_path):
    source = tmp_path / "original"
    source.mkdir()
    for number in range(2001):
        (source / str(number)).touch()
    with pytest.raises(ValueError, match="2000 files"):
        regression._snapshot(source, tmp_path / "snapshot")


def test_snapshot_large_file_fails_closed(tmp_path):
    source = tmp_path / "original"
    source.mkdir()
    with (source / "large").open("wb") as handle:
        handle.truncate(64 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="64 MiB"):
        regression._snapshot(source, tmp_path / "snapshot")


def test_trusted_plugin_reports_real_nodeid_and_assertion(tmp_path):
    import subprocess
    (tmp_path / "regression_metadata.py").write_text(regression._PLUGIN)
    (tmp_path / "test_sample.py").write_text('def test_assertion(): assert False\ndef test_error(): raise RuntimeError("bad")\n')
    report = tmp_path / "result.xml"
    import os
    env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONPATH=str(tmp_path))
    completed = subprocess.run([sys.executable, "-m", "pytest", "-c", "/dev/null", "--rootdir=" + str(tmp_path), "--noconftest", "-p", "regression_metadata", "--junitxml=" + str(report), str(tmp_path / "test_sample.py")], env=env, capture_output=True, timeout=10)
    assert completed.returncode == 1
    tree = regression.ET.fromstring(report.read_bytes())
    properties = [{p.get("name"): p.get("value") for p in case.findall("properties/property")} for case in tree.iter("testcase")]
    assert {p["local_failure_kind"] for p in properties} == {"assertion", "other"}
    assert all("test_sample.py::test_" in p["local_nodeid"] for p in properties)
