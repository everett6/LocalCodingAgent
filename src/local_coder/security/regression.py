"""Bounded local pytest verification in an existing trusted container image.

This limits test access using the container runtime; it is not a guarantee against
hostile code or a compromised runtime. Test-produced reports are evidence, not an
attestation. No host environment is forwarded to the container. Images must contain
Python and pytest already; dependencies are never downloaded.
"""
from __future__ import annotations

import asyncio
import os
import fnmatch
from pathlib import Path
import shutil
import stat
import tempfile
import uuid
import xml.etree.ElementTree as ET

_OUTPUT_LIMIT = 16_384
_REPORT_LIMIT = 1_048_576


async def _command(args: list[str], timeout: float) -> tuple[int, str]:
    """Drain all output while retaining only a bounded tail; kill on timeout."""
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env={"PATH": os.defpath},
    )
    output = bytearray()

    async def drain() -> None:
        assert proc.stdout is not None
        while chunk := await proc.stdout.read(8192):
            output.extend(chunk)
            if len(output) > _OUTPUT_LIMIT:
                del output[:-_OUTPUT_LIMIT]

    reader = asyncio.create_task(drain())
    try:
        async def finish() -> None:
            await proc.wait()
            await reader
        await asyncio.wait_for(finish(), timeout)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        raise
    return proc.returncode, output.decode("utf-8", errors="replace")


_EXCLUDED_DIRS = {".git", ".local-coder", ".venv", "venv", "node_modules", "__pycache__", ".aws", ".ssh", "secrets", ".secrets", ".codex", ".agents", ".azure", ".cache", "target", "dist", "build", "vendor"}
_SECRET_PATTERNS = (".env", ".env.*", "*.pem", "*.key", "credentials*", "*secret*", "*.p12", "*.pfx", ".netrc", ".npmrc", ".pypirc", "id_rsa", "id_ed25519")


def _secure_read_flags() -> int:
    """Require both no-follow and nonblocking reads; never substitute zero."""
    flags = [getattr(os, name, None) for name in ("O_NOFOLLOW", "O_NONBLOCK")]
    if any(type(flag) is not int or flag == 0 for flag in flags):
        raise ValueError("isolated regression requires POSIX O_NOFOLLOW and O_NONBLOCK support")
    return os.O_RDONLY | flags[0] | flags[1]


def _snapshot(root: Path, destination: Path) -> list[str]:
    """Copy bounded readable source, excluding symlinks and private state."""
    read_flags = _secure_read_flags()
    excluded = []
    count = total = visited = 0
    destination.mkdir(mode=0o755)
    destination.chmod(0o755)
    for directory, dirs, files in os.walk(root, followlinks=False):
        visited += 1 + len(dirs) + len(files)
        if visited > 10000:
            raise ValueError("source snapshot exceeds 10000 directory entries")
        base = Path(directory)
        for name in list(dirs):
            source = base / name
            if name in _EXCLUDED_DIRS or source.is_symlink() or any(fnmatch.fnmatch(name.lower(), p) for p in _SECRET_PATTERNS):
                dirs.remove(name)
                if len(excluded) < 2000:
                    excluded.append(source.relative_to(root).as_posix())
            else:
                target_dir = destination / source.relative_to(root)
                target_dir.mkdir(mode=0o755)
                target_dir.chmod(0o755)
        for name in files:
            source = base / name
            relative = source.relative_to(root)
            if source.is_symlink() or any(fnmatch.fnmatch(name.lower(), p) for p in _SECRET_PATTERNS):
                if len(excluded) < 2000:
                    excluded.append(relative.as_posix())
                continue
            fd = os.open(source, read_flags)
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("snapshot contains a nonregular file")
                count += 1
                total += info.st_size
                if count > 2000 or total > 64 * 1024 * 1024:
                    raise ValueError("source snapshot exceeds 2000 files or 64 MiB")
                data = handle.read(min(64 * 1024 * 1024 - total + info.st_size, info.st_size) + 1)
                if len(data) != info.st_size:
                    raise ValueError("source changed during snapshot")
            target = destination / relative
            target.write_bytes(data)
            target.chmod(0o644)
    return sorted(excluded)


_PLUGIN = """import pytest
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when == 'call':
        item.user_properties.append(('local_nodeid', item.nodeid))
        if call.excinfo is not None:
            kind = 'assertion' if isinstance(call.excinfo.value, AssertionError) else 'other'
            item.user_properties.append(('local_failure_kind', kind))
"""


async def run_regression(
    project_root: str | Path, test_path: str | Path, image: str,
    timeout_seconds: int = 60, backend: str = "docker",
) -> dict:
    """Run one local tests/*.py regression and return bounded structured evidence.

    ``status`` is passed, failed (assertions), error (including collection/setup),
    timeout, or unavailable. Counts are derived on the host from bounded JUnit XML.
    A passing status requires actual executed tests and pytest exit code zero.
    Existing project config, conftest files and third-party plugin autoload are
    disabled. Imports use /project/src and /project; fixtures must be in the test.
    """
    result = dict(status="error", passed=0, failed=0, errors=0, skipped=0,
                  tests=0, exit_code=None, output="", error="", backend=backend,
                  isolation="container", test_ids=[], image_id="", failure_kind="",
                  snapshot_exclusions=[])
    if backend not in {"docker", "podman"}:
        result["error"] = "backend must be docker or podman"
        return result
    if type(timeout_seconds) not in (int, float) or not 1 <= timeout_seconds <= 600:
        result["error"] = "timeout_seconds must be between 1 and 600"
        return result
    try:
        root = Path(project_root).resolve(strict=True)
        candidate = Path(test_path)
        path = (root / candidate).resolve(strict=True) if not candidate.is_absolute() else candidate.resolve(strict=True)
        relative = path.relative_to(root)
        if not root.is_dir() or not path.is_file() or relative.parts[0] != "tests" or path.suffix != ".py":
            raise ValueError("test_path must be a Python file under project tests/")
        # Docker's --mount syntax cannot represent commas in host paths safely.
        if "," in str(root):
            raise ValueError("project path cannot contain commas")
        if not image or image.startswith("-") or len(image) > 512:
            raise ValueError("an existing trusted image is required")
    except (OSError, ValueError, IndexError) as exc:
        result["error"] = str(exc)[:1000]
        return result
    try:
        read_flags = _secure_read_flags()
    except ValueError as exc:
        result.update(status="unavailable", error=str(exc))
        return result
    runtime = shutil.which(backend)
    if runtime is None:
        result.update(status="unavailable", error=f"{backend} is not installed")
        return result
    name = "local-coder-regression-" + uuid.uuid4().hex
    created = False
    try:
        code, installed = await _command([runtime, "image", "inspect", "--format", "{{.Id}}", image], 10)
        image_id = installed.strip()
        if code or not image_id.startswith("sha256:") or len(image_id) != 71:
            result.update(status="unavailable", error="trusted image is not installed or runtime is unavailable")
            return result
        result["image_id"] = image_id
        with tempfile.TemporaryDirectory(prefix="local-coder-regression-") as report_dir:
            snapshot = Path(report_dir) / "source"
            result["snapshot_exclusions"] = _snapshot(root, snapshot)
            if not (snapshot / relative).is_file():
                raise ValueError("selected test was excluded from source snapshot")
            trusted = Path(report_dir) / "trusted"
            trusted.mkdir(mode=0o755)
            trusted.chmod(0o755)
            (trusted / "regression_metadata.py").write_text(_PLUGIN)
            (trusted / "regression_metadata.py").chmod(0o644)
            report = Path(report_dir) / "result.xml"
            report.touch(mode=0o666)
            report.chmod(0o666)
            args = [runtime, "run", "--name", name, "--pull=never", "--network", "none",
                    "--read-only", "--user", "65534:65534", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges", "--memory", "512m",
                    "--cpus", "1", "--pids-limit", "128", "--ulimit", "fsize=1048576:1048576",
                    "--log-driver", "none", "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m",
                    "--mount", f"type=bind,src={snapshot},dst=/project,readonly",
                    "--mount", f"type=bind,src={trusted},dst=/trusted,readonly",
                    "--mount", f"type=bind,src={report},dst=/report.xml",
                    "--workdir", "/tmp", "--env", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
                    "--env", "PYTEST_ADDOPTS=", "--env", "PYTEST_PLUGINS=",
                    "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "PYTHONPATH=/trusted:/project/src:/project",
                    "--entrypoint", "python", image_id, "-m", "pytest", "-c", "/dev/null", "--rootdir=/project",
                    "--noconftest", "-p", "regression_metadata", "-p", "no:cacheprovider", "--junitxml=/report.xml",
                    "--", "/project/" + relative.as_posix()]
            created = True
            code, output = await _command(args, timeout_seconds)
            result.update(exit_code=code, output=output)
            report = Path(report_dir) / "result.xml"
            # Open without following a container-created symlink; bound the read
            # even if an untrusted writer changes the file after fstat.
            fd = os.open(report, read_flags)
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > _REPORT_LIMIT:
                    result["error"] = "missing or oversized pytest report"
                    return result
                data = handle.read(_REPORT_LIMIT + 1)
            if len(data) > _REPORT_LIMIT:
                result["error"] = "oversized pytest report"
                return result
            if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
                result["error"] = "unsupported XML declaration"
                return result
            tree = ET.fromstring(data)
            cases = list(tree.iter("testcase"))
            result["tests"] = len(cases)
            failure_kinds = []
            for case in cases:
                key = ("errors" if case.find("error") is not None else
                       "failed" if case.find("failure") is not None else
                       "skipped" if case.find("skipped") is not None else "passed")
                result[key] += 1
                properties = {p.get("name"): p.get("value", "") for p in case.findall("properties/property")}
                if key == "failed":
                    failure_kinds.append(properties.get("local_failure_kind", "other"))
                if key in {"passed", "failed"}:
                    identifier = properties.get("local_nodeid") or case.get("classname", "") + "::" + case.get("name", "")
                    if len(identifier) > 1024 or len(result["test_ids"]) >= 1000 or identifier in result["test_ids"]:
                        result["error"] = "invalid, duplicate, or excessive test identifiers"
                        return result
                    result["test_ids"].append(identifier)
            result["test_ids"].sort()
            result["failure_kind"] = ("assertion" if failure_kinds and all(k == "assertion" for k in failure_kinds) else "other" if failure_kinds else "")
            if result["errors"] or code not in (0, 1):
                result["error"] = "pytest collection, setup, or infrastructure error"
            elif not result["passed"] + result["failed"]:
                result["error"] = "no tests executed"
            elif code == 0 and result["failed"] == 0:
                result["status"] = "passed"
            elif code == 1 and result["failed"] and result["failure_kind"] == "assertion":
                result["status"] = "failed"
            else:
                result["error"] = "pytest exit status and report disagree"
    except asyncio.TimeoutError:
        result.update(status="timeout", error="container command timed out")
    except (OSError, ET.ParseError, ValueError) as exc:
        result["error"] = str(exc)[:1000]
    finally:
        if created:
            # Runs on timeout and cancellation as well as normal completion.
            try:
                await asyncio.shield(_command([runtime, "rm", "--force", name], 10))
            except (OSError, asyncio.TimeoutError):
                result.update(status="error", error="container cleanup failed")
    return result
